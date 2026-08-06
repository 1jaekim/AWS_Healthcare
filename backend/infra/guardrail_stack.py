"""Bedrock Guardrails CDK Stack.

아키텍처 v2 의 `안전장치` 자리다. 보안 원칙 첫 줄 — "직접 식별자는 LLM, RAG,
로그에 전달하지 않는다" — 를 모델 호출 경계에서 강제한다.

애플리케이션 계층에도 같은 목적의 방어가 있다(`app/safety/pii.py`,
`app/safety/pseudonyms.py`). 둘 다 두는 이유는 계층이 다르기 때문이다. 코드는
우리가 아는 패턴만 막고, Guardrail 은 모델 입출력 자체를 서비스 경계에서 막는다.
한쪽이 새도 다른 쪽이 남는다.

배포 후 출력값을 API 환경 변수로 넣어야 실제로 적용된다.

    BEDROCK_GUARDRAIL_ID=<GuardrailId 출력값>
    BEDROCK_GUARDRAIL_VERSION=<GuardrailVersion 출력값>

넣지 않으면 `app/config.py` 의 `ModelSettings.guardrail_id` 가 None 이라 모델
호출에 Guardrail 이 붙지 않는다.
"""

from aws_cdk import CfnOutput, CfnResource, RemovalPolicy, Stack
from constructs import Construct


BLOCKED_INPUT_MESSAGE = (
    "이 요청에는 직접 식별 정보가 포함되어 있어 처리할 수 없습니다. "
    "이름·주민등록번호·연락처를 제외하고 다시 입력해 주세요."
)

BLOCKED_OUTPUT_MESSAGE = (
    "직접 식별 정보가 포함될 수 있어 응답을 표시하지 않았습니다."
)


class GuardrailStack(Stack):
    """임상시험 매칭 모델 호출용 Guardrail."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.guardrail = CfnResource(
            self,
            "TrialMatchingGuardrail",
            type="AWS::Bedrock::Guardrail",
            properties={
                "Name": "trial-matching-guardrail",
                "Description": "임상시험 매칭 PII 차단 및 의료 조언 억제",
                "BlockedInputMessaging": BLOCKED_INPUT_MESSAGE,
                "BlockedOutputsMessaging": BLOCKED_OUTPUT_MESSAGE,
                # ─── 직접 식별자 ─────────────────────────────────
                # 이름·연락처·주소는 익명화(ANONYMIZE)가 아니라 차단(BLOCK)한다.
                # 익명화는 값을 가린 채 요청을 통과시키는데, 그러면 식별자가
                # 애초에 들어왔다는 사실이 조용히 넘어간다. 여기서는 들어오면
                # 안 되는 것이므로 막고 사용자에게 알린다.
                "SensitiveInformationPolicyConfig": {
                    "PiiEntitiesConfig": [
                        {"Type": "NAME", "Action": "BLOCK"},
                        {"Type": "EMAIL", "Action": "BLOCK"},
                        {"Type": "PHONE", "Action": "BLOCK"},
                        {"Type": "ADDRESS", "Action": "BLOCK"},
                        # AGE 는 넣지 않는다. 나이는 가려야 할 식별자가 아니라
                        # 판정에 쓰는 기준 필드다. `AGE: ANONYMIZE` 를 켜 두면
                        # 근거 서술이 이렇게 나온다.
                        #
                        #   "근거에 명시된 나이 {AGE}세는 {AGE}세 이상 조건을 충족함"
                        #
                        # 사람이 검토할 때 이 문장으로는 아무것도 확인할 수 없다.
                        # 배포된 A2A Reviewer 응답에서 실제로 재현됐다. 나이가
                        # 단독으로 개인을 식별하지도 않는다.
                        {"Type": "CREDIT_DEBIT_CARD_NUMBER", "Action": "BLOCK"},
                        {"Type": "INTERNATIONAL_BANK_ACCOUNT_NUMBER", "Action": "BLOCK"},
                    ],
                    # 주민등록번호. Bedrock 기본 PII 목록에 한국 형식이 없어서
                    # 정규식으로 따로 잡는다.
                    "RegexesConfig": [
                        {
                            "Name": "korean-rrn",
                            "Description": "주민등록번호 6자리-7자리",
                            "Pattern": r"\d{6}[-\s]?[1-4]\d{6}",
                            "Action": "BLOCK",
                        }
                    ],
                },
                # ─── 의료 조언 억제 ──────────────────────────────
                # 이 서비스는 사전 스크리닝이지 진료가 아니다. 모델이 처방이나
                # 치료 지시를 내놓으면 제품 경계를 벗어난다.
                "TopicPolicyConfig": {
                    "TopicsConfig": [
                        {
                            "Name": "MedicalTreatmentAdvice",
                            "Type": "DENY",
                            "Definition": (
                                "특정 환자에게 약물 용량, 처방 변경, 치료 시작·중단을 "
                                "지시하거나 진단을 확정하는 내용"
                            ),
                            "Examples": [
                                "메트포르민 용량을 1000mg으로 올리세요.",
                                "인슐린 치료를 지금 시작해야 합니다.",
                                "이 검사 결과는 당뇨병 확진입니다.",
                            ],
                        }
                    ]
                },
                "ContentPolicyConfig": {
                    "FiltersConfig": [
                        {"Type": "PROMPT_ATTACK", "InputStrength": "HIGH", "OutputStrength": "NONE"},
                        {"Type": "MISCONDUCT", "InputStrength": "MEDIUM", "OutputStrength": "MEDIUM"},
                        {"Type": "INSULTS", "InputStrength": "MEDIUM", "OutputStrength": "MEDIUM"},
                        {"Type": "HATE", "InputStrength": "HIGH", "OutputStrength": "HIGH"},
                        {"Type": "SEXUAL", "InputStrength": "HIGH", "OutputStrength": "HIGH"},
                        {"Type": "VIOLENCE", "InputStrength": "MEDIUM", "OutputStrength": "MEDIUM"},
                    ]
                },
            },
        )
        self.guardrail.apply_removal_policy(RemovalPolicy.DESTROY)

        guardrail_id = self.guardrail.get_att("GuardrailId").to_string()

        # DRAFT 는 편집 중인 상태라 배포본이 참조하면 조용히 바뀔 수 있다.
        # 고정 버전을 만들어 그것을 API 가 참조하게 한다.
        self.guardrail_version = CfnResource(
            self,
            "TrialMatchingGuardrailVersion",
            type="AWS::Bedrock::GuardrailVersion",
            properties={
                "GuardrailIdentifier": guardrail_id,
                "Description": "API 가 참조하는 고정 버전",
            },
        )
        self.guardrail_version.add_dependency(self.guardrail)

        self.guardrail_id = guardrail_id
        self.guardrail_arn = self.guardrail.get_att("GuardrailArn").to_string()
        self.version = self.guardrail_version.get_att("Version").to_string()

        CfnOutput(
            self,
            "GuardrailId",
            value=self.guardrail_id,
            description="BEDROCK_GUARDRAIL_ID",
        )
        CfnOutput(
            self,
            "GuardrailVersion",
            value=self.version,
            description="BEDROCK_GUARDRAIL_VERSION",
        )
