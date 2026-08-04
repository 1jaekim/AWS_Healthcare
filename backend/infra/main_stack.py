"""
메인 CDK Stack - 전체 AWS 리소스 프로비저닝
─────────────────────────────────────────────
Lambda, Step Functions, DynamoDB와 Bedrock GraphRAG를
하나로 조합하는 최상위 스택

의존 관계:
  S3DataStack → BedrockKnowledgeBaseStack
             → MainStack (Lambdas, StepFunctions, DynamoDB)
             → ObservabilityStack
"""
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as events_targets,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_secretsmanager as secretsmanager,
    aws_sqs as sqs,
    aws_stepfunctions as sfn,
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
        self.patient_pseudonym_secret = secretsmanager.Secret(
            self,
            "PatientPseudonymSecret",
            description="환자/임상 이벤트 비식별 HMAC 키",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                exclude_punctuation=True,
                password_length=64,
            ),
        )

        lambda_common_env = {
            "S3_BUCKET_NAME": s3_stack.data_bucket.bucket_name,
            "S3_PREFIX_RAW": "raw/",
            "S3_PREFIX_RAG": "rag/",
            "S3_PREFIX_TRIALS": "trials/",
            "DYNAMODB_CRITERIA_TABLE": self.criteria_table.table_name,
            "BEDROCK_MODEL_ID": "anthropic.claude-3-sonnet-20240229-v1:0",
            "KNOWLEDGE_BASE_ID": bedrock_stack.get_knowledge_base_id(),
            "LOG_LEVEL": "INFO",
            "SNS_ALERT_TOPIC_ARN": observability_stack.alert_topic.topic_arn,
            "PATIENT_PSEUDONYM_SECRET_ARN": self.patient_pseudonym_secret.secret_arn,
        }

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
        self.patient_pseudonym_secret.grant_read(self.sanitizer_fn)

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

        knowledge_base_arn = (
            f"arn:aws:bedrock:{Stack.of(self).region}:{Stack.of(self).account}:"
            f"knowledge-base/{bedrock_stack.get_knowledge_base_id()}"
        )

        # ─── Step Functions: EMR Pipeline ────────────────
        with open("step_functions/emr_pipeline.json", "r", encoding="utf-8") as f:
            emr_definition = f.read()

        emr_definition = emr_definition.replace(
            "${SanitizerFunctionArn}", self.sanitizer_fn.function_arn
        ).replace(
            "${KnowledgeBaseId}", bedrock_stack.get_knowledge_base_id()
        ).replace(
            "${DataSourceId}", bedrock_stack.get_data_source_id()
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
        self.emr_state_machine.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:StartIngestionJob", "bedrock:GetIngestionJob"],
                resources=[knowledge_base_arn],
            )
        )
        observability_stack.alert_topic.grant_publish(self.emr_state_machine)

        # ─── Step Functions: Trials Pipeline ─────────────
        with open("step_functions/trials_pipeline.json", "r", encoding="utf-8") as f:
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
        trials_upload_rule = events.Rule(
            self,
            "TrialsUploadRule",
            description="임상시험 공고 업로드 감지",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={
                    "bucket": {"name": [s3_stack.data_bucket.bucket_name]},
                    "object": {"key": [{"prefix": "trials/"}]},
                },
            ),
        )
        trials_upload_rule.add_target(
            events_targets.SfnStateMachine(self.trials_state_machine)
        )
