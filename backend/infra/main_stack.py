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

        GraphRAG는 선택 계층이므로 출력값을 문자열로 명시적으로 받는다. 이
        스택은 서울이고 GraphRAG는 버지니아라 리전이 갈리는데, 문자열로 받으면
        CloudFormation 교차 리전 참조 없이 연결할 수 있다.

        GraphRAG 스택을 먼저 배포하고 그 출력값을 CDK 컨텍스트로 넘긴다.
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
            "LOG_LEVEL": "INFO",
            "SNS_ALERT_TOPIC_ARN": observability_stack.alert_topic.topic_arn,
        }

        # ─── Sanitizer Lambda ────────────────────────────
        self.sanitizer_fn = _lambda.Function(
            self,
            "SanitizerFunction",
            function_name="healthcare-sanitizer",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/sanitizer"),
            environment={
                **lambda_common_env,
                "PATIENT_PSEUDONYM_SECRET_ARN": self.patient_pseudonym_secret.secret_arn,
            },
            timeout=Duration.minutes(5),
            memory_size=1024,
            dead_letter_queue=self.dlq,
        )
        s3_stack.grant_sanitizer_access(self.sanitizer_fn)
        self.patient_pseudonym_secret.grant_read(self.sanitizer_fn)

        # Step Functions runs in Seoul, while the Bedrock Knowledge Base is in
        # Virginia. AWS SDK integrations are region-local, so this adapter
        # creates the Bedrock Agent client for the explicit GraphRAG region.
        self.graphrag_ingestion_fn = _lambda.Function(
            self,
            "GraphRagIngestionFunction",
            function_name="healthcare-graphrag-ingestion",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/graphrag_ingestion"),
            environment={
                "KNOWLEDGE_BASE_ID": knowledge_base_id,
                "DATA_SOURCE_ID": data_source_id,
                "GRAPHRAG_REGION": graphrag_region,
                "LOG_LEVEL": "INFO",
            },
            timeout=Duration.minutes(2),
            memory_size=256,
            dead_letter_queue=self.dlq,
        )

        # ─── Protocol Parser Lambda ─────────────────────
        self.protocol_parser_fn = _lambda.Function(
            self,
            "ProtocolParserFunction",
            function_name="healthcare-protocol-parser",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=_lambda.Code.from_asset("lambdas/protocol_parser"),
            environment={
                **lambda_common_env,
                "S3_RAG_BUCKET_NAME": graphrag_bucket,
                "S3_RAG_REGION": graphrag_region,
            },
            timeout=Duration.minutes(5),
            memory_size=512,
            dead_letter_queue=self.dlq,
        )
        s3_stack.grant_protocol_parser_access(self.protocol_parser_fn)
        self.criteria_table.grant_write_data(self.protocol_parser_fn)

        # GraphRAG에는 공개 공고·표준문서만 쓴다. 환자 sanitizer에는 이 버킷
        # 권한을 주지 않아 데이터 경계를 IAM에서도 강제한다.
        if graphrag_bucket:
            self.protocol_parser_fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["s3:PutObject"],
                    resources=[
                        f"arn:aws:s3:::{graphrag_bucket}/rag/references/*"
                    ],
                )
            )
            self.protocol_parser_fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["kms:Encrypt", "kms:GenerateDataKey", "kms:Decrypt"],
                    resources=[
                        f"arn:aws:kms:{graphrag_region}:{Stack.of(self).account}:key/*"
                    ],
                )
            )

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

        # 명시된 GraphRAG 리전의 KB만 색인할 수 있다.
        knowledge_base_arn = (
            f"arn:aws:bedrock:{graphrag_region}:{Stack.of(self).account}:"
            f"knowledge-base/{knowledge_base_id or '*'}"
        )
        self.graphrag_ingestion_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:StartIngestionJob", "bedrock:GetIngestionJob"],
                resources=[knowledge_base_arn],
            )
        )

        # ─── Step Functions: EMR Pipeline ────────────────
        with open("step_functions/emr_pipeline.json", "r", encoding="utf-8") as f:
            emr_definition = f.read()

        emr_definition = emr_definition.replace(
            "${SanitizerFunctionArn}", self.sanitizer_fn.function_arn
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
        observability_stack.alert_topic.grant_publish(self.emr_state_machine)

        # ─── Step Functions: Trials Pipeline ─────────────
        with open("step_functions/trials_pipeline.json", "r", encoding="utf-8") as f:
            trials_definition = f.read()

        trials_definition = trials_definition.replace(
            "${ProtocolParserFunctionArn}", self.protocol_parser_fn.function_arn
        ).replace(
            "${GraphRagIngestionFunctionArn}",
            self.graphrag_ingestion_fn.function_arn,
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
        self.graphrag_ingestion_fn.grant_invoke(self.trials_state_machine)
        observability_stack.alert_topic.grant_publish(self.trials_state_machine)

        # ─── EventBridge: 텍스트 공고 업로드 → Trials Pipeline
        # trials/screenshots/는 감사 원본이다. Textract는 한국어 OCR을 지원하지
        # 않으므로 스크린샷은 기준 추출이나 GraphRAG 색인을 시작하지 않는다.
        trials_upload_rule = events.Rule(
            self,
            "TrialsUploadRule",
            description="임상시험 공고 업로드 감지",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={
                    "bucket": {"name": [s3_stack.data_bucket.bucket_name]},
                    "object": {"key": [{"prefix": "trials/documents/"}]},
                },
            ),
        )
        trials_upload_rule.add_target(
            events_targets.SfnStateMachine(self.trials_state_machine)
        )
