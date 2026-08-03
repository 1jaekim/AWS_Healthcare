"""
Bedrock Knowledge Bases + OpenSearch Serverless CDK Stack
──────────────────────────────────────────────────────────
- OpenSearch Serverless 컬렉션 (Vector + Keyword Index)
- Bedrock Knowledge Base (S3 rag/ → 임베딩 → OpenSearch)
- Titan Embed Text v2 모델로 EMR 임베딩 생성
- 검색 API를 위한 쿼리 Lambda

아키텍처:
  S3 rag/canonical_notes.jsonl
    → Bedrock Knowledge Base (자동 동기화)
    → Amazon Titan Embed Text v2 (임베딩)
    → OpenSearch Serverless (Vector + Keyword Index)
    → OpenSearch 쿼리 API (Lambda)
"""
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_bedrock as bedrock,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_opensearchserverless as oss,
)
from constructs import Construct
import json


class BedrockKnowledgeBaseStack(Stack):
    """Bedrock Knowledge Bases + OpenSearch Serverless 스택"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        data_bucket_arn: str,
        data_key_arn: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.collection_name = "healthcare-emr-vectors"
        self.index_name = "emr-vectors"

        # ─── OpenSearch Serverless 네트워크 정책 ──────────
        network_policy = oss.CfnAccessPolicy(
            self,
            "NetworkPolicy",
            name="healthcare-network-policy",
            type="network",
            policy=json.dumps([
                {
                    "Rules": [
                        {
                            "ResourceType": "collection",
                            "Resource": [f"collection/{self.collection_name}"],
                        },
                        {
                            "ResourceType": "dashboard",
                            "Resource": [f"collection/{self.collection_name}"],
                        },
                    ],
                    "AllowFromPublic": True,
                }
            ]),
        )

        # ─── OpenSearch Serverless 암호화 정책 ───────────
        encryption_policy = oss.CfnSecurityPolicy(
            self,
            "EncryptionPolicy",
            name="healthcare-encryption-policy",
            type="encryption",
            policy=json.dumps({
                "Rules": [
                    {
                        "ResourceType": "collection",
                        "Resource": [f"collection/{self.collection_name}"],
                    }
                ],
                "AWSOwnedKey": True,
            }),
        )

        # ─── OpenSearch Serverless 컬렉션 ────────────────
        self.collection = oss.CfnCollection(
            self,
            "EmrVectorCollection",
            name=self.collection_name,
            type="VECTORSEARCH",
            description="EMR 비식별 데이터 벡터 검색 컬렉션",
        )
        self.collection.add_dependency(encryption_policy)
        self.collection.add_dependency(network_policy)

        # ─── Bedrock Knowledge Base IAM Role ─────────────
        self.kb_role = iam.Role(
            self,
            "BedrockKBRole",
            assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"),
            description="Bedrock Knowledge Base 실행 역할",
        )

        # S3 rag/ 경로 읽기 권한
        self.kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:ListBucket"],
                resources=[
                    data_bucket_arn,
                    f"{data_bucket_arn}/rag/*",
                ],
            )
        )

        # KMS 복호화 권한
        self.kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt", "kms:GenerateDataKey"],
                resources=[data_key_arn],
            )
        )

        # Bedrock 모델 호출 권한 (임베딩)
        self.kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[
                    f"arn:aws:bedrock:{Stack.of(self).region}::foundation-model/amazon.titan-embed-text-v2:0"
                ],
            )
        )

        # OpenSearch Serverless 접근 권한
        self.kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=["aoss:APIAccessAll"],
                resources=[
                    f"arn:aws:aoss:{Stack.of(self).region}:{Stack.of(self).account}:collection/*"
                ],
            )
        )

        # ─── OpenSearch Serverless 데이터 접근 정책 ──────
        oss.CfnAccessPolicy(
            self,
            "DataAccessPolicy",
            name="healthcare-data-access",
            type="data",
            policy=json.dumps([
                {
                    "Rules": [
                        {
                            "ResourceType": "index",
                            "Resource": [f"index/{self.collection_name}/*"],
                            "Permission": [
                                "aoss:CreateIndex",
                                "aoss:UpdateIndex",
                                "aoss:DescribeIndex",
                                "aoss:ReadDocument",
                                "aoss:WriteDocument",
                            ],
                        },
                        {
                            "ResourceType": "collection",
                            "Resource": [f"collection/{self.collection_name}"],
                            "Permission": [
                                "aoss:CreateCollectionItems",
                                "aoss:DescribeCollectionItems",
                                "aoss:UpdateCollectionItems",
                            ],
                        },
                    ],
                    "Principal": [self.kb_role.role_arn],
                }
            ]),
        )

        # ─── Bedrock Knowledge Base ──────────────────────
        self.knowledge_base = bedrock.CfnKnowledgeBase(
            self,
            "EmrKnowledgeBase",
            name="healthcare-emr-kb",
            description="비식별 EMR 데이터 기반 Knowledge Base",
            role_arn=self.kb_role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn=f"arn:aws:bedrock:{Stack.of(self).region}::foundation-model/amazon.titan-embed-text-v2:0",
                ),
            ),
            storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="OPENSEARCH_SERVERLESS",
                opensearch_serverless_configuration=bedrock.CfnKnowledgeBase.OpenSearchServerlessConfigurationProperty(
                    collection_arn=self.collection.attr_arn,
                    vector_index_name=self.index_name,
                    field_mapping=bedrock.CfnKnowledgeBase.OpenSearchServerlessFieldMappingProperty(
                        vector_field="embedding",
                        text_field="text",
                        metadata_field="metadata",
                    ),
                ),
            ),
        )

        # ─── Bedrock Data Source (S3 rag/) ───────────────
        self.data_source = bedrock.CfnDataSource(
            self,
            "EmrDataSource",
            name="emr-rag-source",
            knowledge_base_id=self.knowledge_base.attr_knowledge_base_id,
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="S3",
                s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                    bucket_arn=data_bucket_arn,
                    inclusion_prefixes=["rag/"],
                ),
            ),
        )

    def get_collection_endpoint(self) -> str:
        """OpenSearch Serverless 컬렉션 엔드포인트"""
        return self.collection.attr_collection_endpoint

    def get_knowledge_base_id(self) -> str:
        """Knowledge Base ID"""
        return self.knowledge_base.attr_knowledge_base_id
