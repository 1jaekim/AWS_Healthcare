"""
메인 CDK Stack - 전체 AWS 리소스 프로비저닝
─────────────────────────────────────────────
모든 Lambda, Step Functions, DynamoDB, Neptune, VPC 등을
하나로 조합하는 최상위 스택

의존 관계:
  S3DataStack → BedrockKnowledgeBaseStack
             → MainStack (Lambdas, StepFunctions, Neptune, DynamoDB)
             → ObservabilityStack
"""
import json
import os

from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_events as events,
    aws_events_targets as events_targets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_lambda_event_sources as lambda_events,
    aws_neptune_alpha as neptune,
    aws_sqs as sqs,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as tasks,
)
from constructs import Construct


class MainStack(Stack):
    """Healthcare 파이프라인 메인 스택"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        s3_stack,
        bedrock_stack,
        observability_stack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ─── VPC (Neptune용) ─────────────────────────────
        self.vpc = ec2.Vpc(
            self,
            "HealthcareVpc",
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
            ],
        )

        # ─── Neptune 클러스터 ────────────────────────────
        neptune_security_group = ec2.SecurityGroup(
            self,
            "NeptuneSG",
            vpc=self.vpc,
            description="Neptune 클러스터 보안 그룹",
            allow_all_outbound=False,
        )

        self.neptune_cluster = neptune.DatabaseCluster(
            self,
            "HealthcareNeptune",
            vpc=self.vpc,
            instance_type=neptune.InstanceType.R5_LARGE,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[neptune_security_group],
            removal_policy=RemovalPolicy.RETAIN,
            iam_authentication=True,
        )

        # Neptune Loader IAM Role
        self.neptune_loader_role = iam.Role(
            self,
            "NeptuneLoaderRole",
            assumed_by=iam.ServicePrincipal("rds.amazonaws.com"),
            description="Neptune Bulk Loader S3 접근 역할",
        )
        s3_stack.grant_neptune_loader_access(self.neptune_loader_role)

        # ─── DynamoDB Criteria Store ─────────────────────
        self.criteria_table = dynamodb.Table(
            self,
            "CriteriaStore",
            table_name="CriteriaStore",
            partition_key=dynamodb.Attribute(
                name="trial_id", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="source_key", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            point_in_time_recovery=True,
        )

        # GSI: status별 조회 (검토 대기건 조회용)
        self.criteria_table.add_global_secondary_index(
            index_name="status-index",
            partition_key=dynamodb.Attribute(
                name="status", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="created_at", type=dynamodb.AttributeType.NUMBER
            ),
        )

        # ─── DLQ (공통) ──────────────────────────────────
        self.dlq = sqs.Queue(
            self,
            "PipelineDLQ",
            queue_name="healthcare-pipeline-dlq",
            retention_period=Duration.days(14),
            encryption=sqs.QueueEncryption.KMS_MANAGED,
        )

        # ─── Lambda 공통 설정 ────────────────────────────
        lambda_common_env = {
            "S3_BUCKET_NAME": s3_stack.data_bucket.bucket_name,
            "S3_PREFIX_RAW": "raw/",
            "S3_PREFIX_RAG": "rag/",
            "S3_PREFIX_GRAPH": "graph/",
            "S3_PREFIX_TRIALS": "trials/",
            "NEPTUNE_ENDPOINT": self.neptune_cluster.cluster_endpoint.hostname,
            "NEPTUNE_PORT": "8182",
            "NEPTUNE_LOADER_ROLE_ARN": self.neptune_loader_role.role_arn,
            "DYNAMODB_CRITERIA_TABLE": self.criteria_table.table_name,
            "BEDROCK_MODEL_ID": "anthropic.claude-3-sonnet-20240229-v1:0",
            "KNOWLEDGE_BASE_ID": bedrock_stack.get_knowledge_base_id(),
            "OPENSEARCH_ENDPOINT": bedrock_stack.get_collection_endpoint(),
            "LOG_LEVEL": "INFO",
            "SNS_ALERT_TOPIC_ARN": observability_stack.alert_topic.topic_arn,
        }

        # Lambda 보안 그룹 (Neptune 접근용)
        lambda_sg = ec2.SecurityGroup(
            self,
            "LambdaSG",
            vpc=self.vpc,
            description="Lambda 함수 보안 그룹",
            allow_all_outbound=True,
        )
        neptune_security_group.add_ingress_rule(
            lambda_sg, ec2.Port.tcp(8182), "Lambda → Neptune"
        )

        # ─── Sanitizer Lambda ────────────────────────────
        self.sanitizer_fn = _lambda.Function(
            self,
            "SanitizerFunction",
            function_name="healthcare-sanitizer",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/sanitizer"),
            environment=lambda_common_env,
            timeout=Duration.minutes(5),
            memory_size=1024,
            dead_letter_queue=self.dlq,
        )
        s3_stack.grant_sanitizer_access(self.sanitizer_fn)

        # ─── Graph ETL Lambda ────────────────────────────
        self.graph_etl_fn = _lambda.Function(
            self,
            "GraphETLFunction",
            function_name="healthcare-graph-etl",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/graph_etl"),
            environment=lambda_common_env,
            timeout=Duration.minutes(5),
            memory_size=1024,
            dead_letter_queue=self.dlq,
        )
        s3_stack.grant_graph_etl_access(self.graph_etl_fn)

        # ─── Neptune Upsert Lambda (VPC 내부) ────────────
        self.neptune_upsert_fn = _lambda.Function(
            self,
            "NeptuneUpsertFunction",
            function_name="healthcare-neptune-upsert",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/neptune_upsert"),
            environment=lambda_common_env,
            timeout=Duration.minutes(5),
            memory_size=512,
            vpc=self.vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_groups=[lambda_sg],
            dead_letter_queue=self.dlq,
        )
        s3_stack.grant_upsert_lambda_access(self.neptune_upsert_fn)

        # S3 graph/ 이벤트 → Neptune Upsert Lambda
        s3_stack.add_graph_event_notification(self.neptune_upsert_fn)

        # ─── Protocol Parser Lambda ─────────────────────
        self.protocol_parser_fn = _lambda.Function(
            self,
            "ProtocolParserFunction",
            function_name="healthcare-protocol-parser",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/protocol_parser"),
            environment=lambda_common_env,
            timeout=Duration.minutes(5),
            memory_size=512,
            dead_letter_queue=self.dlq,
        )
        s3_stack.grant_protocol_parser_access(self.protocol_parser_fn)
        self.criteria_table.grant_write_data(self.protocol_parser_fn)

        # Bedrock 호출 권한
        self.protocol_parser_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=["*"],
            )
        )
        # Textract 권한
        self.protocol_parser_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["textract:DetectDocumentText"],
                resources=["*"],
            )
        )

        # ─── OpenSearch Query Lambda ────────────────────
        self.opensearch_query_fn = _lambda.Function(
            self,
            "OpenSearchQueryFunction",
            function_name="healthcare-opensearch-query",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/opensearch_query"),
            environment=lambda_common_env,
            timeout=Duration.seconds(30),
            memory_size=256,
        )
        # Bedrock KB Retrieve 권한
        self.opensearch_query_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:Retrieve"],
                resources=["*"],
            )
        )

        # ─── Step Functions: EMR Pipeline ────────────────
        with open("step_functions/emr_pipeline.json", "r") as f:
            emr_definition = f.read()

        emr_definition = emr_definition.replace(
            "${SanitizerFunctionArn}", self.sanitizer_fn.function_arn
        ).replace(
            "${GraphETLFunctionArn}", self.graph_etl_fn.function_arn
        ).replace(
            "${NeptuneUpsertFunctionArn}", self.neptune_upsert_fn.function_arn
        ).replace(
            "${AlertTopicArn}", observability_stack.alert_topic.topic_arn
        )

        self.emr_state_machine = sfn.StateMachine(
            self,
            "EmrPipeline",
            state_machine_name="healthcare-emr-pipeline",
            definition_body=sfn.DefinitionBody.from_string(emr_definition),
            timeout=Duration.hours(1),
        )

        # Step Functions에 Lambda 호출 권한 부여
        self.sanitizer_fn.grant_invoke(self.emr_state_machine)
        self.graph_etl_fn.grant_invoke(self.emr_state_machine)
        self.neptune_upsert_fn.grant_invoke(self.emr_state_machine)
        observability_stack.alert_topic.grant_publish(self.emr_state_machine)

        # ─── Step Functions: Trials Pipeline ─────────────
        with open("step_functions/trials_pipeline.json", "r") as f:
            trials_definition = f.read()

        trials_definition = trials_definition.replace(
            "${ProtocolParserFunctionArn}", self.protocol_parser_fn.function_arn
        ).replace(
            "${AlertTopicArn}", observability_stack.alert_topic.topic_arn
        )

        self.trials_state_machine = sfn.StateMachine(
            self,
            "TrialsPipeline",
            state_machine_name="healthcare-trials-pipeline",
            definition_body=sfn.DefinitionBody.from_string(trials_definition),
            timeout=Duration.minutes(30),
        )

        self.protocol_parser_fn.grant_invoke(self.trials_state_machine)
        observability_stack.alert_topic.grant_publish(self.trials_state_machine)

        # ─── EventBridge: trials/ 업로드 → Trials Pipeline
        s3_stack.add_trials_event_target(
            events_targets.SfnStateMachine(self.trials_state_machine)
        )

        # ─── 관측성 연결 ─────────────────────────────────
        lambda_map = {
            "Sanitizer": self.sanitizer_fn,
            "GraphETL": self.graph_etl_fn,
            "NeptuneUpsert": self.neptune_upsert_fn,
            "ProtocolParser": self.protocol_parser_fn,
            "OpenSearchQuery": self.opensearch_query_fn,
        }

        for name, fn in lambda_map.items():
            observability_stack.create_lambda_log_group(name, fn)
            observability_stack.add_lambda_alarms(name, fn)

        observability_stack.add_step_functions_alarms("EmrPipeline", self.emr_state_machine)
        observability_stack.add_step_functions_alarms("TrialsPipeline", self.trials_state_machine)
        observability_stack.add_pipeline_dashboard_widgets(lambda_map)
