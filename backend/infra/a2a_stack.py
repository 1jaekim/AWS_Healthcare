"""Two independent A2A Lambda runtimes for bounded clinical deliberation."""

from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
)
from constructs import Construct


ASSET_DIR = Path(__file__).resolve().parents[1] / "build" / "a2a_lambda"


class A2AStack(Stack):
    """Reviewer and challenger with separate functions, roles, and IAM URLs."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        guardrail_id: str = "",
        guardrail_version: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if not ASSET_DIR.exists():
            raise FileNotFoundError(
                f"A2A Lambda asset is missing: {ASSET_DIR}\n"
                "Run backend/scripts/build_a2a_asset.sh first."
            )

        self.reviewer = self._agent(
            "EvidenceReviewer",
            function_name="healthcare-a2a-evidence-reviewer",
            role="evidence_reviewer",
            guardrail_id=guardrail_id,
            guardrail_version=guardrail_version,
        )
        self.challenger = self._agent(
            "ChallengeReviewer",
            function_name="healthcare-a2a-challenge-reviewer",
            role="challenge_reviewer",
            guardrail_id=guardrail_id,
            guardrail_version=guardrail_version,
        )
        self.reviewer_url = self.reviewer.add_function_url(
            auth_type=_lambda.FunctionUrlAuthType.AWS_IAM
        )
        self.challenger_url = self.challenger.add_function_url(
            auth_type=_lambda.FunctionUrlAuthType.AWS_IAM
        )

        CfnOutput(self, "ReviewerA2AUrl", value=self.reviewer_url.url)
        CfnOutput(self, "ChallengerA2AUrl", value=self.challenger_url.url)
        CfnOutput(
            self,
            "ReviewerFunctionName",
            value=self.reviewer.function_name,
        )
        CfnOutput(
            self,
            "ChallengerFunctionName",
            value=self.challenger.function_name,
        )

    def grant_invoke(self, grantee: iam.IGrantable) -> None:
        """Grant both permissions required for new IAM Function URLs."""
        self.reviewer_url.grant_invoke_url(grantee)
        self.challenger_url.grant_invoke_url(grantee)
        iam.Grant.add_to_principal(
            scope=self,
            grantee=grantee,
            actions=["lambda:InvokeFunction"],
            resource_arns=[self.reviewer.function_arn, self.challenger.function_arn],
            conditions={"Bool": {"lambda:InvokedViaFunctionUrl": "true"}},
        )

    def _agent(
        self,
        construct_id: str,
        *,
        function_name: str,
        role: str,
        guardrail_id: str,
        guardrail_version: str,
    ) -> _lambda.Function:
        function = _lambda.Function(
            self,
            construct_id,
            function_name=function_name,
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.X86_64,
            handler="a2a_agents.lambda_handler.handler",
            code=_lambda.Code.from_asset(str(ASSET_DIR)),
            environment={
                "A2A_AGENT_ROLE": role,
                "BEDROCK_MODEL_ID": (
                    "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
                ),
                "BEDROCK_MAX_TOKENS": "1536",
                "BEDROCK_GUARDRAIL_ID": guardrail_id,
                "BEDROCK_GUARDRAIL_VERSION": guardrail_version,
                "LOG_LEVEL": "INFO",
            },
            memory_size=1024,
            timeout=Duration.minutes(2),
            log_group=logs.LogGroup(
                self,
                f"{construct_id}LogGroup",
                log_group_name=f"/aws/lambda/{function_name}",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )
        function.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Converse",
                    "bedrock:ConverseStream",
                ],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:*:{Stack.of(self).account}:inference-profile/*",
                ],
            )
        )
        if guardrail_id:
            function.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["bedrock:ApplyGuardrail"],
                    resources=[
                        f"arn:aws:bedrock:{Stack.of(self).region}:"
                        f"{Stack.of(self).account}:guardrail/{guardrail_id}"
                    ],
                )
            )
        return function
