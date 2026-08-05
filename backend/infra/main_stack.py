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
        observability_stack,
        graphrag_region: str,
        knowledge_base_id: str = "",
        data_source_id: str = "",
        graphrag_bucket: str = "",
        **kwargs,
    ) -> None:
        """GraphRAG 값들은 스택 참조가 아니라 문자열로 받는다.

        GraphRAG 는 us-west-2 에 있고 이 스택은 서울에 있다. CloudFormation 의
        Export/ImportValue 는 같은 리전 안에서만 동작하므로 스택 객체를 넘겨
        `get_att` 를 쓰던 방식은 리전이 갈린 순간 성립하지 않는다.

        대신 GraphRAG 스택을 먼저 배포하고 그 출력값을 CDK 컨텍스트로 넘긴다.
        빈 문자열이면 GraphRAG 없이 배포된다 — Lambda 는 KB 를 못 찾고 API 는
        로컬 키워드 검색으로 내려앉지만, 파이프라인 자체는 뜬다.
        """
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

        # 지원서 스키마와 작성 세션. API Lambda는 요청마다 다른 실행 환경을
        # 사용할 수 있으므로 메모리에 두면 다음 요청에서 404가 난다.
        self.intake_table = dynamodb.Table(
            self,
            "ApplicationStore",
            table_name="ApplicationStore",
            partition_key=dynamodb.Attribute(
                name="record_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",
            removal_policy=RemovalPolicy.RETAIN,
            point_in_time_recovery=True,
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
            "BEDROCK_MODEL_ID": "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "KNOWLEDGE_BASE_ID": knowledge_base_id,
            # 비식별 문서만 GraphRAG 리전으로 건너간다. 비워두면 서울 버킷에
            # 그대로 쓰므로 KB 가 읽지 못한다 — 리전이 갈린 뒤로는 이 두 값이
            # 파이프라인과 KB 를 잇는 유일한 끈이다.
            "S3_RAG_BUCKET_NAME": graphrag_bucket,
            "S3_RAG_REGION": graphrag_region,
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

        # GraphRAG 버킷은 다른 리전에 있어서 CDK 의 grant_write 로 못 잇는다
        # (버킷 객체가 이 앱의 이 리전 스택에 없다). 이름으로 ARN 을 짜서 직접
        # 붙인다. 대상이 `rag/patients/` 하나뿐이라 와일드카드가 넓지 않다.
        if graphrag_bucket:
            self.sanitizer_fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["s3:PutObject"],
                    resources=[f"arn:aws:s3:::{graphrag_bucket}/rag/patients/*"],
                )
            )
            # 교차 리전 KMS. 키 ARN 을 여기서 알 수 없어 리전으로만 좁힌다.
            # 이 계정의 us-west-2 키에 한정되고, S3 가 대신 호출하는 경로다.
            self.sanitizer_fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["kms:Encrypt", "kms:GenerateDataKey", "kms:Decrypt"],
                    resources=[
                        f"arn:aws:kms:{graphrag_region}:{Stack.of(self).account}:key/*"
                    ],
                )
            )

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

        # KB 는 us-west-2 에 있다. 이 스택의 리전을 쓰면 존재하지 않는 ARN 이
        # 만들어지고, Step Functions 의 색인 시작이 권한 오류로 죽는다.
        knowledge_base_arn = (
            f"arn:aws:bedrock:{graphrag_region}:{Stack.of(self).account}:"
            f"knowledge-base/{knowledge_base_id or '*'}"
        )

        # ─── Step Functions: EMR Pipeline ────────────────
        with open("step_functions/emr_pipeline.json", "r", encoding="utf-8") as f:
            emr_definition = f.read()

        emr_definition = emr_definition.replace(
            "${SanitizerFunctionArn}", self.sanitizer_fn.function_arn
        ).replace(
            "${KnowledgeBaseId}", knowledge_base_id
        ).replace(
            "${DataSourceId}", data_source_id
        ).replace(
            "${GraphRagRegion}", graphrag_region
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
