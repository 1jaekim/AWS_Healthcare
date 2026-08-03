"""
S3 버킷 구조 및 이벤트 설정 CDK Stack
─────────────────────────────────────────
버킷 구조:
  raw/    - 원본 데이터 보존 (암호화·접근통제)
  rag/    - 비식별 EMR (Bedrock Knowledge Bases 소스)
  graph/  - Neptune CSV (nodes/, edges/)
  trials/ - 임상시험 공고 원문 (PDF)

보안:
  - KMS 암호화 (SSE-KMS)
  - 퍼블릭 액세스 차단
  - 버전 관리 활성화
  - 수명주기 정책 (raw/ 90일 → Glacier)

이벤트:
  - graph/ 업로드 → Neptune Upsert Lambda 트리거
  - trials/ 업로드 → EventBridge 전달 (공고 구조화 파이프라인)
"""
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as _lambda,
    aws_s3 as s3,
    aws_s3_notifications as s3n,
)
from constructs import Construct


class S3DataStack(Stack):
    """헬스케어 데이터 레이크 S3 버킷 스택"""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ─── KMS 키 생성 ─────────────────────────────────
        self.data_key = kms.Key(
            self,
            "HealthcareDataKey",
            alias="alias/healthcare-data-key",
            description="헬스케어 데이터 암호화 키",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ─── 메인 데이터 버킷 ────────────────────────────
        self.data_bucket = s3.Bucket(
            self,
            "HealthcareDataBucket",
            bucket_name=None,  # CDK가 자동 생성
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.data_key,
            versioned=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.RETAIN,
            enforce_ssl=True,
            # 수명주기 정책
            lifecycle_rules=[
                # raw/ 데이터 90일 후 Glacier로 이동
                s3.LifecycleRule(
                    id="raw-to-glacier",
                    prefix="raw/",
                    transitions=[
                        s3.Transition(
                            storage_class=s3.StorageClass.GLACIER,
                            transition_after=Duration.days(90),
                        )
                    ],
                ),
                # 불완전 멀티파트 업로드 7일 후 정리
                s3.LifecycleRule(
                    id="abort-incomplete-uploads",
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                ),
            ],
            # CORS (필요시 프론트엔드 연동)
            cors=[
                s3.CorsRule(
                    allowed_methods=[s3.HttpMethods.GET],
                    allowed_origins=["*"],
                    allowed_headers=["*"],
                    max_age=3600,
                )
            ],
        )

        # ─── EventBridge 연동 활성화 ────────────────────
        self.data_bucket.enable_event_bridge_notification()

        # ─── S3 이벤트 규칙: trials/ 업로드 → EventBridge ─
        self.trials_upload_rule = events.Rule(
            self,
            "TrialsUploadRule",
            description="임상시험 공고 업로드 감지",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={
                    "bucket": {"name": [self.data_bucket.bucket_name]},
                    "object": {"key": [{"prefix": "trials/"}]},
                },
            ),
        )

        # ─── 버킷 정책: 접근 통제 ──────────────────────
        # raw/ 경로는 sanitizer Lambda만 읽기 허용
        # rag/ 경로는 Bedrock Knowledge Bases 읽기 허용
        # graph/ 경로는 Neptune Bulk Loader 읽기 허용

    def grant_sanitizer_access(self, lambda_function: _lambda.Function) -> None:
        """Sanitizer Lambda에 raw/ 읽기 + rag/ 쓰기 권한 부여"""
        self.data_bucket.grant_read(lambda_function, "raw/*")
        self.data_bucket.grant_write(lambda_function, "rag/*")
        self.data_key.grant_encrypt_decrypt(lambda_function)

    def grant_graph_etl_access(self, lambda_function: _lambda.Function) -> None:
        """Graph ETL Lambda에 raw/ 읽기 + graph/ 쓰기 권한 부여"""
        self.data_bucket.grant_read(lambda_function, "raw/*")
        self.data_bucket.grant_write(lambda_function, "graph/*")
        self.data_key.grant_encrypt_decrypt(lambda_function)

    def grant_neptune_loader_access(self, role: iam.IRole) -> None:
        """Neptune Bulk Loader에 graph/ 읽기 권한 부여"""
        self.data_bucket.grant_read(role, "graph/*")
        self.data_key.grant_decrypt(role)

    def grant_upsert_lambda_access(self, lambda_function: _lambda.Function) -> None:
        """Neptune Upsert Lambda에 graph/ 읽기 권한 부여"""
        self.data_bucket.grant_read(lambda_function, "graph/*")
        self.data_key.grant_decrypt(lambda_function)

    def grant_protocol_parser_access(self, lambda_function: _lambda.Function) -> None:
        """Protocol Parser Lambda에 trials/ 읽기 권한 부여"""
        self.data_bucket.grant_read(lambda_function, "trials/*")
        self.data_key.grant_decrypt(lambda_function)

    def add_graph_event_notification(self, lambda_function: _lambda.Function) -> None:
        """graph/ 경로 업로드 시 Neptune Upsert Lambda 트리거"""
        self.data_bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.LambdaDestination(lambda_function),
            s3.NotificationKeyFilter(prefix="graph/", suffix=".csv"),
        )

    def add_trials_event_target(self, target: targets.SfnStateMachine) -> None:
        """trials/ 업로드 이벤트에 Step Functions 타겟 연결"""
        self.trials_upload_rule.add_target(target)
