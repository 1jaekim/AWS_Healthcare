"""Bedrock Knowledge Bases + Neptune Analytics GraphRAG 인프라.

이 스택만 버지니아(us-east-1)에 만든다. 서비스 본체는 서울인데 여기만 떼어놓는
이유는 Bedrock Knowledge Bases 가 서울에서 GraphRAG(NEPTUNE_ANALYTICS 스토리지)를
제공하지 않기 때문이다. 지원 리전은 프랑크푸르트·런던·아일랜드·오리건·버지니아·
도쿄·싱가포르뿐이다.

그래프, KB, KB 소스 S3 와 KMS 는 리전 결합 리소스이므로 한 스택으로 유지한다.
GraphRAG 는 공개 임상시험 공고나 표준문서 검색을 위한 선택 계층이며 사용자
지원서나 EHR/EMR 의 저장소로 사용하지 않는다.

리전이 갈리므로 API 쪽에 `S3_RAG_REGION`·`KNOWLEDGE_BASE_REGION` 교차 리전 연결이
필요하다. 서울에서 GraphRAG 가 열리면 `graphrag_region` 컨텍스트를 서울로 바꾸고
그 두 변수를 걷어내면 된다.
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
# Nova Micro cannot be invoked with on-demand throughput. Graph construction
# must use the regional system inference profile instead of a foundation-model
# ARN, otherwise StartIngestionJob fails before an ingestion job is created.
DEFAULT_GRAPH_MODEL_ID = "us.amazon.nova-micro-v1:0"

RAG_PREFIX = "rag/references/"
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
        if str(graph_model_id).startswith("arn:"):
            graph_model_arn = str(graph_model_id)
            graph_model_resources = [graph_model_arn]
        elif str(graph_model_id).startswith("us."):
            graph_model_arn = (
                f"arn:aws:bedrock:{region}:{Stack.of(self).account}:"
                f"inference-profile/{graph_model_id}"
            )
            base_model_id = str(graph_model_id).split(".", 1)[1]
            # A US system profile can route inference across US regions. Keep
            # the wildcard limited to the exact underlying foundation model.
            graph_model_resources = [
                graph_model_arn,
                f"arn:aws:bedrock:*::foundation-model/{base_model_id}",
            ]
        else:
            graph_model_arn = (
                f"arn:aws:bedrock:{region}::foundation-model/{graph_model_id}"
            )
            graph_model_resources = [graph_model_arn]

        # ─── KB 소스 버킷 ────────────────────────────────
        # GraphRAG 데이터 경계를 분리하기 위해 이 스택 전용 KMS 키를 쓴다.
        self.rag_key = kms.Key(
            self,
            "GraphRagDataKey",
            alias="alias/healthcare-graphrag-key",
            description="GraphRAG 공개 문서 암호화 키",
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

        # 서울 Protocol Parser가 공개 참고문서만 쓸 수 있도록 경로를 제한한다.
        self.rag_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowReferencePublisherCrossRegionWrite",
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
                resources=[embedding_model_arn, *graph_model_resources],
            )
        )
        if ":inference-profile/" in graph_model_arn:
            self.kb_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["bedrock:GetInferenceProfile"],
                    resources=[graph_model_arn],
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
                "Name": "healthcare-public-reference-graphrag",
                "Description": "임상시험 공개 공고와 표준문서 GraphRAG",
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
                "Name": "public-reference-graphrag-source",
                "Description": "공개 임상시험 공고와 표준문서 Markdown",
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
