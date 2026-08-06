"""
관측성 CDK Stack (CloudWatch)
────────────────────────────────
- CloudWatch Logs: 모든 Lambda 로그 그룹 (보존 30일)
- CloudWatch Metrics: 커스텀 메트릭 (파이프라인 처리 건수/실패율)
- CloudWatch Alarms: 실패율 임계값 알람
- Bedrock 토큰·비용 추적: 커스텀 메트릭
- SNS 알림: 검토 대기건, 파이프라인 실패
- CloudWatch Dashboard: 전체 파이프라인 현황
"""
from aws_cdk import (
    Duration,
    Stack,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
)
from constructs import Construct


class ObservabilityStack(Stack):
    """관측성 인프라 스택"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        alert_email: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ─── SNS 알림 토픽 ───────────────────────────────
        self.alert_topic = sns.Topic(
            self,
            "PipelineAlertTopic",
            display_name="Healthcare Pipeline Alerts",
            topic_name="healthcare-pipeline-alerts",
        )

        if alert_email:
            self.alert_topic.add_subscription(
                subs.EmailSubscription(alert_email)
            )

        # ─── CloudWatch Dashboard ────────────────────────
        self.dashboard = cw.Dashboard(
            self,
            "HealthcarePipelineDashboard",
            # CloudWatch dashboard names are account-global, not region-scoped.
            dashboard_name=f"Healthcare-Pipeline-Overview-{self.region}",
            period_override=cw.PeriodOverride.AUTO,
        )

    def create_lambda_log_group(self, function_name: str, lambda_fn: _lambda.Function) -> logs.LogGroup:
        """Lambda 로그 그룹 생성 (30일 보존)"""
        return logs.LogGroup(
            self,
            f"{function_name}LogGroup",
            log_group_name=f"/aws/lambda/{lambda_fn.function_name}",
            retention=logs.RetentionDays.ONE_MONTH,
        )

    def add_lambda_alarms(self, function_name: str, lambda_fn: _lambda.Function) -> None:
        """Lambda별 에러/쓰로틀 알람 생성"""
        # 에러 알람 (5분간 3회 이상)
        error_alarm = cw.Alarm(
            self,
            f"{function_name}ErrorAlarm",
            alarm_name=f"Healthcare-{function_name}-Errors",
            metric=lambda_fn.metric_errors(
                period=Duration.minutes(5),
                statistic="Sum",
            ),
            threshold=3,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            alarm_description=f"{function_name} Lambda 에러 발생 (5분간 3회 이상)",
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        error_alarm.add_alarm_action(cw_actions.SnsAction(self.alert_topic))

        # 쓰로틀 알람
        throttle_alarm = cw.Alarm(
            self,
            f"{function_name}ThrottleAlarm",
            alarm_name=f"Healthcare-{function_name}-Throttles",
            metric=lambda_fn.metric_throttles(
                period=Duration.minutes(5),
                statistic="Sum",
            ),
            threshold=5,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            alarm_description=f"{function_name} Lambda 쓰로틀 발생",
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        throttle_alarm.add_alarm_action(cw_actions.SnsAction(self.alert_topic))

        # 실행 시간 알람 (타임아웃 80% 초과)
        duration_alarm = cw.Alarm(
            self,
            f"{function_name}DurationAlarm",
            alarm_name=f"Healthcare-{function_name}-Duration",
            metric=lambda_fn.metric_duration(
                period=Duration.minutes(5),
                statistic="p95",
            ),
            threshold=240000,  # 240초 (4분)
            evaluation_periods=2,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            alarm_description=f"{function_name} Lambda 실행시간 과다 (p95 > 240s)",
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        duration_alarm.add_alarm_action(cw_actions.SnsAction(self.alert_topic))

    def add_step_functions_alarms(self, sfn_name: str, state_machine) -> None:
        """Step Functions 실패 알람"""
        failed_alarm = cw.Alarm(
            self,
            f"{sfn_name}FailedAlarm",
            alarm_name=f"Healthcare-{sfn_name}-Failed",
            metric=cw.Metric(
                namespace="AWS/States",
                metric_name="ExecutionsFailed",
                dimensions_map={"StateMachineArn": state_machine.state_machine_arn},
                period=Duration.minutes(5),
                statistic="Sum",
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            alarm_description=f"{sfn_name} Step Functions 실행 실패",
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        failed_alarm.add_alarm_action(cw_actions.SnsAction(self.alert_topic))

    def add_bedrock_metrics(self) -> None:
        """Bedrock 호출 메트릭 위젯 추가"""
        bedrock_widget = cw.GraphWidget(
            title="Bedrock 호출 메트릭",
            left=[
                cw.Metric(
                    namespace="AWS/Bedrock",
                    metric_name="Invocations",
                    period=Duration.hours(1),
                    statistic="Sum",
                    label="총 호출 수",
                ),
                cw.Metric(
                    namespace="AWS/Bedrock",
                    metric_name="InvocationErrors",
                    period=Duration.hours(1),
                    statistic="Sum",
                    label="호출 에러",
                ),
            ],
            right=[
                cw.Metric(
                    namespace="AWS/Bedrock",
                    metric_name="InputTokenCount",
                    period=Duration.hours(1),
                    statistic="Sum",
                    label="입력 토큰",
                ),
                cw.Metric(
                    namespace="AWS/Bedrock",
                    metric_name="OutputTokenCount",
                    period=Duration.hours(1),
                    statistic="Sum",
                    label="출력 토큰",
                ),
            ],
            width=24,
            height=6,
        )
        self.dashboard.add_widgets(bedrock_widget)

    def add_pipeline_dashboard_widgets(self, lambdas: dict[str, _lambda.Function]) -> None:
        """대시보드에 파이프라인 위젯 추가"""
        # Lambda 호출/에러 그래프
        invocations_widget = cw.GraphWidget(
            title="Lambda 호출 수",
            left=[
                fn.metric_invocations(period=Duration.hours(1), statistic="Sum", label=name)
                for name, fn in lambdas.items()
            ],
            width=12,
            height=6,
        )

        errors_widget = cw.GraphWidget(
            title="Lambda 에러 수",
            left=[
                fn.metric_errors(period=Duration.hours(1), statistic="Sum", label=name)
                for name, fn in lambdas.items()
            ],
            width=12,
            height=6,
        )

        self.dashboard.add_widgets(invocations_widget, errors_widget)

        # Lambda 실행 시간 그래프
        duration_widget = cw.GraphWidget(
            title="Lambda 실행 시간 (p95)",
            left=[
                fn.metric_duration(period=Duration.hours(1), statistic="p95", label=name)
                for name, fn in lambdas.items()
            ],
            width=12,
            height=6,
        )

        # DLQ 메트릭
        dlq_widget = cw.TextWidget(
            markdown="## DLQ 모니터링\nDLQ 메시지 수는 SQS 콘솔에서 확인하세요.\n실패한 메시지는 자동으로 DLQ에 전달됩니다.",
            width=12,
            height=6,
        )

        self.dashboard.add_widgets(duration_widget, dlq_widget)

        # Bedrock 메트릭 추가
        self.add_bedrock_metrics()

    def add_custom_metric_filter(self, log_group: logs.LogGroup, filter_name: str, pattern: str, metric_name: str) -> None:
        """로그에서 커스텀 메트릭 추출 (처리 건수 등)"""
        logs.MetricFilter(
            self,
            filter_name,
            log_group=log_group,
            filter_pattern=logs.FilterPattern.literal(pattern),
            metric_namespace="Healthcare/Pipeline",
            metric_name=metric_name,
            metric_value="1",
        )
