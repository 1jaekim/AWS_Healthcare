"""Bedrock Knowledge Bases GraphRAG 인프라 (us-west-2 전용).

왜 이 스택만 리전이 다른가
─────────────────────────
나머지 전부는 서울(ap-northeast-2)에 있다. GraphRAG 만 미국 서부에 두는 이유는
선택이 아니라 제약이다. Neptune Analytics 자체는 2026년 1월부터 서울에서 쓸 수
있지만, Bedrock Knowledge Bases 가 서울에서 `NEPTUNE_ANALYTICS` 스토리지 타입을
아직 받지 않는다. 서울에 그대로 배포하면 그래프는 만들어지고 KB 생성만
실패한다(빈 그래프가 시간당 과금되며 남는다).

    KnowledgeBase storage type NEPTUNE_ANALYTICS is not supported.
    (Service: BedrockAgent, Status Code: 400)

그래서 GraphRAG 계층 — 그래프, KB, KB 가 읽을 S3 — 만 통째로 us-west-2 에 둔다.
셋을 한 스택에 묶은 것은 KB 의 데이터소스 버킷이 KB 와 같은 리전이어야 하기
때문이다. 하나만 남겨두면 조용히 깨진다.

이 리전 분리가 만드는 것
────────────────────────
- Sanitizer Lambda(서울)는 비식별 문서를 이 스택의 버킷으로 교차 리전 PUT 한다.
  `S3_RAG_BUCKET_NAME` 과 `S3_RAG_REGION` 이 그 연결이다.
- API(서울)는 이 리전의 KB 를 Retrieve 한다. `KNOWLEDGE_BASE_REGION` 이 그
  연결이다. 이 값을 빠뜨리면 API 가 서울에서 KB 를 찾다가 못 찾는다.
- 개인정보가 리전을 넘는다. 넘어가는 것은 비식별화를 마친 문서뿐이다.
  직접 식별자는 `lambdas/sanitizer/graphrag_documents.py` 에서 이미 떨어져 나가고
  서울의 raw/ 에만 남는다. 이 경계가 무너지면 리전 분리가 곧 개인정보 국외
  이전이 되므로, sanitizer 를 고칠 때 이 순서를 바꾸면 안 된다.

서울에서 KB 가 열리면 이 스택을 서울로 되돌리고 아래 두 환경 변수를 지우면 된다.
"""

from aws_cdk import (
    CfnOutput,
    CfnResource,
    RemovalPolicy,
    Stack,
    aws_iam as iam,
    aws_kms as kms,
    aws_s3 as s3,
)
from constructs import Construct


EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"
EMBEDDING_DIMENSIONS = 1024
DEFAULT_GRAPH_MODEL_ID = "amazon.nova-micro-v1:0"

RAG_PREFIX = "rag/patients/"
"""KB 가 읽는 유일한 경로. 이 밖의 객체는 색인되지 않는다."""


class GraphRagStack(Stack):
    """Neptune Analytics + Bedrock Knowledge Base + 전용 소스 버킷."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        writer_account_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        embedding_model_arn = (
            f"arn:aws:bedrock:{region}::foundation-model/{EMBEDDING_MODEL_ID}"
        )
        graph_model_id = (
            self.node.try_get_context("graph_construction_model_id")
            or DEFAULT_GRAPH_MODEL_ID
        )
        graph_model_arn = (
            f"arn:aws:bedrock:{region}::foundation-model/{graph_model_id}"
        )

        # ─── KB 소스 버킷 ────────────────────────────────
        # 서울 데이터 레이크의 KMS 키는 리전이 달라 여기서 못 쓴다. 이 리전에
        # 별도 키를 만든다. 키가 리전마다 따로인 것은 KMS 의 성질이지 설계 실수가
        # 아니다.
        self.rag_key = kms.Key(
            self,
            "GraphRagDataKey",
            alias="alias/healthcare-graphrag-key",
            description="GraphRAG 비식별 문서 암호화 키 (us-west-2)",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.rag_bucket = s3.Bucket(
            self,
            "GraphRagSourceBucket",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.rag_key,
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # 서울의 Sanitizer Lambda 가 교차 리전으로 써야 한다. 역할 ARN 이 아직
        # 없으므로(순환 참조가 된다) 계정 단위로 열고, 조건으로 경로를 묶는다.
        # 계정 안의 아무나가 아니라 이 경로에만 쓸 수 있다.
        self.rag_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowSanitizerCrossRegionWrite",
                principals=[iam.AccountPrincipal(writer_account_id)],
                actions=["s3:PutObject", "s3:PutObjectAcl"],
                resources=[f"{self.rag_bucket.bucket_arn}/{RAG_PREFIX}*"],
            )
        )
        self.rag_key.grant_encrypt_decrypt(iam.AccountPrincipal(writer_account_id))

        # ─── Neptune Analytics 그래프 ────────────────────
        # ProvisionedMemory 16 은 최소 단위(16 m-NCU)다. 그래프는 중지할 수 없고
        # 삭제만 가능하므로, 데모가 끝나면 스택째 지우는 것이 유일한 과금 중단
        # 방법이다. RETAIN 을 걸지 않은 이유가 이것이다 — 롤백이나 삭제 때 빈
        # 그래프가 남아 계속 청구되는 사고를 이미 한 번 겪었다.
        self.graph = CfnResource(
            self,
            "HealthcareEvidenceGraph",
            type="AWS::NeptuneGraph::Graph",
            properties={
                "GraphName": "healthcare-evidence-graphrag",
                "ProvisionedMemory": 16,
                "ReplicaCount": 0,
                "PublicConnectivity": False,
                "DeletionProtection": False,
                "VectorSearchConfiguration": {
                    "VectorSearchDimension": EMBEDDING_DIMENSIONS,
                },
            },
        )
        graph_arn = self.graph.get_att("GraphArn").to_string()

        # ─── KB 실행 역할 ────────────────────────────────
        self.kb_role = iam.Role(
            self,
            "BedrockGraphRagRole",
            assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"),
            description="Bedrock Knowledge Bases GraphRAG execution role",
        )
        self.kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:ListBucket"],
                resources=[
                    self.rag_bucket.bucket_arn,
                    f"{self.rag_bucket.bucket_arn}/*",
                ],
            )
        )
        self.rag_key.grant_encrypt_decrypt(self.kb_role)
        self.kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[embedding_model_arn, graph_model_arn],
            )
        )
        self.kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "neptune-graph:GetGraph",
                    "neptune-graph:ReadDataViaQuery",
                    "neptune-graph:WriteDataViaQuery",
                    "neptune-graph:DeleteDataViaQuery",
                ],
                resources=[graph_arn],
            )
        )

        # ─── Knowledge Base ──────────────────────────────
        self.knowledge_base = CfnResource(
            self,
            "PatientEvidenceKnowledgeBase",
            type="AWS::Bedrock::KnowledgeBase",
            properties={
                "Name": "healthcare-patient-evidence-graphrag",
                "Description": "비식별 환자 근거와 표준문서 GraphRAG",
                "RoleArn": self.kb_role.role_arn,
                "KnowledgeBaseConfiguration": {
                    "Type": "VECTOR",
                    "VectorKnowledgeBaseConfiguration": {
                        "EmbeddingModelArn": embedding_model_arn,
                        "EmbeddingModelConfiguration": {
                            "BedrockEmbeddingModelConfiguration": {
                                "Dimensions": EMBEDDING_DIMENSIONS,
                                "EmbeddingDataType": "FLOAT32",
                            }
                        },
                    },
                },
                "StorageConfiguration": {
                    "Type": "NEPTUNE_ANALYTICS",
                    "NeptuneAnalyticsConfiguration": {
                        "GraphArn": graph_arn,
                        "FieldMapping": {
                            "MetadataField": "metadata",
                            "TextField": "text",
                        },
                    },
                },
            },
        )
        self.knowledge_base.add_dependency(self.graph)
        self.knowledge_base.node.add_dependency(self.kb_role)

        self.data_source = CfnResource(
            self,
            "PatientEvidenceDataSource",
            type="AWS::Bedrock::DataSource",
            properties={
                "Name": "patient-evidence-graphrag-source",
                "Description": "비식별 환자별 Markdown 근거",
                "KnowledgeBaseId": self.knowledge_base.get_att(
                    "KnowledgeBaseId"
                ).to_string(),
                "DataDeletionPolicy": "DELETE",
                "DataSourceConfiguration": {
                    "Type": "S3",
                    "S3Configuration": {
                        "BucketArn": self.rag_bucket.bucket_arn,
                        "InclusionPrefixes": [RAG_PREFIX],
                    },
                },
                "VectorIngestionConfiguration": {
                    "ContextEnrichmentConfiguration": {
                        "Type": "BEDROCK_FOUNDATION_MODEL",
                        "BedrockFoundationModelConfiguration": {
                            "ModelArn": graph_model_arn,
                            "EnrichmentStrategyConfiguration": {
                                "Method": "CHUNK_ENTITY_EXTRACTION"
                            },
                        },
                    }
                },
            },
        )
        self.data_source.add_dependency(self.knowledge_base)

        # ─── 출력 ────────────────────────────────────────
        # 리전이 다르면 CloudFormation Export 로 스택을 잇지 못한다. 서울 스택들은
        # 이 값을 CDK 컨텍스트로 받는다:
        #
        #   cdk deploy HealthcareMainStack \
        #     -c knowledge_base_id=<KnowledgeBaseId> \
        #     -c data_source_id=<DataSourceId> \
        #     -c graphrag_bucket=<GraphRagBucketName>
        CfnOutput(
            self,
            "KnowledgeBaseId",
            value=self.knowledge_base.get_att("KnowledgeBaseId").to_string(),
            description="KNOWLEDGE_BASE_ID / -c knowledge_base_id",
        )
        CfnOutput(
            self,
            "DataSourceId",
            value=self.data_source.get_att("DataSourceId").to_string(),
            description="-c data_source_id",
        )
        CfnOutput(
            self,
            "GraphRagBucketName",
            value=self.rag_bucket.bucket_name,
            description="S3_RAG_BUCKET_NAME / -c graphrag_bucket",
        )
        CfnOutput(
            self,
            "GraphRagRegion",
            value=region,
            description="KNOWLEDGE_BASE_REGION / S3_RAG_REGION",
        )
        CfnOutput(
            self,
            "GraphArn",
            value=graph_arn,
            description="Neptune Analytics 그래프 ARN (과금 대상)",
        )
