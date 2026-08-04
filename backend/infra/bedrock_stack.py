"""Amazon Bedrock Knowledge Bases GraphRAG infrastructure.

S3 ``rag/patients/`` documents are embedded and enriched by Bedrock. Bedrock
stores both vectors and extracted entity relationships in Neptune Analytics.
"""

from aws_cdk import CfnResource, RemovalPolicy, Stack, aws_iam as iam
from constructs import Construct


EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"
EMBEDDING_DIMENSIONS = 1024
DEFAULT_GRAPH_MODEL_ID = "amazon.nova-micro-v1:0"


class BedrockKnowledgeBaseStack(Stack):
    """Bedrock Knowledge Bases + Neptune Analytics managed GraphRAG."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        data_bucket_arn: str,
        data_key_arn: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account
        embedding_model_arn = (
            f"arn:aws:bedrock:{region}::foundation-model/{EMBEDDING_MODEL_ID}"
        )
        graph_model_id = self.node.try_get_context("graph_construction_model_id")
        graph_model_id = graph_model_id or DEFAULT_GRAPH_MODEL_ID
        graph_model_arn = (
            f"arn:aws:bedrock:{region}::foundation-model/{graph_model_id}"
        )

        # Raw L1 resources keep this stack compatible with the repository's
        # pinned CDK while using current CloudFormation GraphRAG properties.
        self.graph = CfnResource(
            self,
            "HealthcareEvidenceGraph",
            type="AWS::NeptuneGraph::Graph",
            properties={
                "GraphName": "healthcare-evidence-graphrag",
                "ProvisionedMemory": 16,
                "ReplicaCount": 0,
                "PublicConnectivity": False,
                "KmsKeyIdentifier": data_key_arn,
                "VectorSearchConfiguration": {
                    "VectorSearchDimension": EMBEDDING_DIMENSIONS,
                },
            },
        )
        self.graph.apply_removal_policy(RemovalPolicy.RETAIN)
        graph_arn = self.graph.get_att("GraphArn").to_string()

        self.kb_role = iam.Role(
            self,
            "BedrockGraphRagRole",
            assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"),
            description="Bedrock Knowledge Bases GraphRAG execution role",
        )
        self.kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:ListBucket"],
                resources=[data_bucket_arn, f"{data_bucket_arn}/rag/patients/*"],
            )
        )
        self.kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt", "kms:GenerateDataKey"],
                resources=[data_key_arn],
            )
        )
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
                        "BucketArn": data_bucket_arn,
                        "InclusionPrefixes": ["rag/patients/"],
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

        # Avoid an implicit wildcard account in any future ARN additions.
        self.account_id = account

    def get_knowledge_base_id(self) -> str:
        return self.knowledge_base.get_att("KnowledgeBaseId").to_string()

    def get_graph_arn(self) -> str:
        return self.graph.get_att("GraphArn").to_string()

    def get_data_source_id(self) -> str:
        return self.data_source.get_att("DataSourceId").to_string()
