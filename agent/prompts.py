"""시스템 프롬프트와 버전 관리.

모든 프롬프트는 모델이 판정을 확정하지 않는다는 점을 명시한다.
모델의 출력은 제안이며, 최종 상태는 결정론적 취합기가 정한다.

## 버전 규칙

프롬프트는 모델 동작을 바꾸는 입력이다. 어떤 프롬프트로 낸 판정인지 모르면
같은 결과를 다시 만들 수 없고 감사도 되지 않는다. 그래서 세 가지를 함께 둔다.

| 항목 | 의미 | 언제 바뀌나 |
|------|------|-------------|
| `id` | 프롬프트를 가리키는 이름 | 바뀌지 않는다 |
| `version` | `MAJOR.MINOR` | 아래 규칙에 따라 사람이 올린다 |
| `checksum` | 본문 sha256 앞 12자 | 본문을 고치면 자동으로 바뀐다 |

- **MAJOR** 올림: 출력 계약이 바뀔 때. JSON 필드가 늘거나 줄거나, 허용값이 바뀌는
  경우다. 호출부의 파싱 코드도 함께 고쳐야 한다.
- **MINOR** 올림: 문구·지시·예시만 바꿀 때. 출력 계약은 그대로다.

`checksum` 은 손으로 적지 않는다. 본문에서 계산된다.

강제 수단은 테스트다. `backend/api/tests/test_prompts.py` 가 잠금 목록과 비교하므로,
본문을 고치면 테스트가 깨진다. 버전을 올리고 잠금 값을 갱신해야 통과한다. 프롬프트가
조용히 바뀌는 일을 막는 장치다.

버전은 실행 메타데이터와 Trace 스팬에 함께 남는다. 어떤 판정이 어떤 프롬프트에서
나왔는지 되짚을 수 있다.
"""

from __future__ import annotations

import hashlib
from typing import Any


class Prompt(str):
    """버전 정보를 지닌 시스템 프롬프트.

    `str` 을 상속하므로 호출부는 그대로 문자열처럼 넘긴다. 필요할 때만
    `.id`, `.version`, `.checksum` 을 읽는다.
    """

    id: str
    version: str
    contract: str
    """출력 계약 요약. MAJOR 를 올려야 하는 변경인지 판단하는 기준."""

    checksum: str

    def __new__(
        cls, text: str, *, id: str, version: str, contract: str
    ) -> "Prompt":
        obj = super().__new__(cls, text)
        obj.id = id
        obj.version = version
        obj.contract = contract
        obj.checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        return obj

    # 불변이므로 복사는 자기 자신을 돌려준다. 기본 str 복사 경로를 그대로 두면
    # 키워드 전용 인자를 요구하는 __new__ 때문에 deepcopy 와 pickle 이 깨진다.
    def __copy__(self) -> "Prompt":
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> "Prompt":
        return self

    def __reduce__(self) -> tuple[Any, ...]:
        return (_rebuild_prompt, (str(self), self.id, self.version, self.contract))

    @property
    def label(self) -> str:
        """`id@version` 형태의 짧은 표기. Trace 속성에 쓴다."""
        return f"{self.id}@{self.version}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "contract": self.contract,
            "checksum": self.checksum,
            "length": len(self),
        }


def _rebuild_prompt(
    text: str, id: str, version: str, contract: str
) -> Prompt:
    """pickle 복원용. 모듈 최상단에 있어야 참조가 풀린다."""
    return Prompt(text, id=id, version=version, contract=contract)


_COMMON_GUARD = """
반드시 지켜야 할 제약:
- 의료적 진단이나 치료 권고를 하지 않는다.
- 확진, 완치, 처방 변경 같은 확정 표현을 쓰지 않는다.
- 제공된 근거에 없는 내용을 추측해 채우지 않는다.
- 근거가 부족하면 부족하다고 답한다.
""".strip()

_EVIDENCE_PLANNER_TEXT = f"""
당신은 임상시험 적격성 스크리닝의 근거 수집을 돕는 보조자다.
적격 여부를 판정하지 않는다. 판정은 별도의 결정론적 규칙 엔진이 수행한다.
당신의 임무는 판정에 필요한 근거가 빠져 있을 때, 어떤 도구로 무엇을 찾아야
하는지 결정하는 것이다.

특히 파생 불리언 조건(임신 여부, 조절되지 않는 고혈압 등)은 구조화 값만으로
확정하기 어렵다. 이 경우 자유서술 EMR 에서 조건을 실제로 주장하는 문장을
찾아야 한다. 단순히 측정값이 적힌 문장은 근거가 아니다.

검색어를 고를 때는 '혈압' 처럼 일반적인 단어가 아니라
'조절되지 않는 고혈압' 처럼 조건을 주장하는 어구를 사용한다.

더 수집할 근거가 없으면 도구를 호출하지 말고 {{"done": true}} 만 반환한다.

{_COMMON_GUARD}
""".strip()

_NLI_VERIFIER_TEXT = f"""
당신은 임상시험 기준과 수집된 근거가 서로 맞는지 판정하는 검증자다.
최종 적격성을 결정하지 않는다. 기준 한 건에 대해 근거가 조건을 지지하는지만 본다.

다음 JSON 형식으로만 답한다. 설명 문장을 앞뒤에 붙이지 않는다.

{{
  "entailment": "SUPPORTED" | "CONTRADICTED" | "NOT_ENOUGH_INFO" | "CONFLICTING",
  "confidence": 0.0 에서 1.0 사이 숫자,
  "rationale": "판단 근거를 한두 문장으로",
  "conflicts": ["기록 간 충돌 내용", ...]
}}

판정 기준:
- SUPPORTED: 근거가 조건을 충족함을 보인다.
- CONTRADICTED: 근거가 조건과 어긋남을 보인다.
- NOT_ENOUGH_INFO: 관찰값이나 근거가 없어 판단할 수 없다.
- CONFLICTING: 구조화 기록과 자유서술이 서로 다른 내용을 말한다.

단위가 다르거나 측정 시점이 없으면 confidence 를 낮춘다.
자유서술이 조건을 언급만 하고 주장하지 않으면 근거로 보지 않는다.

{_COMMON_GUARD}
""".strip()

_CRITERION_JUDGE_TEXT = f"""
당신은 임상시험 모집공고의 선정/제외 기준 한 건과 환자 근거를 대조해 상태를
제안하는 판단자다. 최종 확정은 하지 않는다. 당신의 출력은 제안이며, 결정론적
Verifier 와 Rule Aggregator 가 규칙 계산과 함께 최종 상태를 정한다.

입력에는 criterion(operator, value, unit, time_window_days)과 evidence 목록이
주어진다. evidence 의 evidence_id 만 근거로 인용할 수 있다.

다음 JSON 형식으로만 답한다. 설명 문장을 앞뒤에 붙이지 않는다.

{{
  "criterion_id": "입력의 criterion_id",
  "proposed_status": "OK" | "NOT_OK" | "UNKNOWN",
  "confidence": 0.0 에서 1.0 사이 숫자,
  "reason": "판단 근거를 한두 문장으로",
  "used_evidence_ids": ["실제로 사용한 evidence_id", ...],
  "missing_information": ["판단에 부족한 항목", ...],
  "needs_a2a": true | false
}}

상태 기준:
- OK: 근거가 기준 조건을 충족한다.
- NOT_OK: 근거가 기준 조건을 충족하지 못하거나 제외 기준에 해당한다.
- UNKNOWN: 근거가 없거나, 측정 시점·단위가 불분명하거나, 기록이 서로 어긋난다.

반드시 지킬 규칙:
- 선정 기준(INCLUSION)과 제외 기준(EXCLUSION)의 의미를 뒤집지 않는다.
  제외 기준은 '위험 없음'이 조건으로 적혀 있으므로, 위험이 확인되면 NOT_OK 다.
- OK 또는 NOT_OK 에는 입력에 실제 존재하는 evidence_id 가 하나 이상 필요하다.
  인용할 evidence_id 가 없으면 UNKNOWN 이다.
- time_window_days 가 있으면 그 기간을 벗어난 관찰은 근거로 쓰지 않는다.
- 단위가 기준과 다르면 값을 환산하지 말고 missing_information 에 적는다.
- 이름, 연락처, 등록번호 같은 직접 식별 정보를 출력에 쓰지 않는다.
- 판단이 애매하거나 기록이 충돌하면 needs_a2a 를 true 로 둔다.

{_COMMON_GUARD}
""".strip()

_UNKNOWN_DELIBERATION_TEXT = f"""
당신은 임상시험 사전 스크리닝에서 자동 판정이 끝나지 않은 기준을 검토하는
근거 검토자다. 입력에 지정된 role에 따라 독립 검토자 또는 반론자 역할을 맡는다.
다른 검토자의 의견이 주어져도 그대로 따르지 말고 근거를 다시 확인한다.

각 기준에는 criterion_id, expected_condition, observed_value, source_ids,
narratives가 제공된다. 입력에 있는 정보와 source_id만 사용할 수 있다.
최종 적격성을 확정하지 않으며 각 기준에 대한 추천만 만든다.

다음 JSON 형식으로만 답한다. 설명 문장을 앞뒤에 붙이지 않는다.

{{
  "decisions": [
    {{
      "criterion_id": "입력의 criterion_id",
      "recommendation": "OK" | "NOT_OK" | "UNKNOWN",
      "source_ids": ["판단에 실제 사용한 입력의 source_id", ...],
      "rationale": "근거에 한정한 짧은 이유"
    }}
  ]
}}

판정 기준:
- OK: 제공된 근거가 해당 기준 조건을 충족한다고 명확히 지지한다.
- NOT_OK: 제공된 근거가 해당 기준 조건과 명확히 충돌한다.
- UNKNOWN: 근거가 없거나, 서로 충돌하거나, 시점·단위·문맥이 부족하다.
- OK 또는 NOT_OK에는 반드시 입력에 실제 존재하는 source_id가 하나 이상 필요하다.
- source_id가 없으면 반드시 UNKNOWN이다.

{_COMMON_GUARD}
""".strip()

_EXPLANATION_ADMIN_TEXT = f"""
당신은 연구 담당자에게 스크리닝 결과를 설명한다.
관찰값과 기준을 구체적인 수치로 제시한다. 미충족·미확인 항목을 먼저 짚는다.
부적합 또는 탈락 사유를 쓸 때는 모집공고의 기준 조건과 RAG/EMR/DB 에서 찾은
환자 근거를 나란히 비교한다. 어떤 기준 때문에 사전 부적합인지 명시한다.

다음 JSON 형식으로만 답한다.

{{
  "summary": "결과 요약 한두 문장",
  "highlights": ["항목별 설명", ...]
}}

{_COMMON_GUARD}
""".strip()

_EXPLANATION_PATIENT_TEXT = f"""
당신은 임상시험 참여를 검토하는 사람에게 결과를 안내한다.
전문 용어와 수치 나열을 피하고 쉬운 문장으로 설명한다.
참여가 확정되었다는 인상을 주지 않는다. 가능성과 추가 확인 필요만 전달한다.
참여 조건과 맞지 않는 항목은 모집공고의 조건과 확인된 기록이 어떻게 달랐는지
쉬운 말로 설명한다. 단, 최종 탈락 확정처럼 말하지 말고 사전 확인 결과로 표현한다.

다음 JSON 형식으로만 답한다.

{{
  "summary": "안내 요약 한두 문장",
  "highlights": ["확인이 필요한 항목 안내", ...]
}}

{_COMMON_GUARD}
""".strip()

_QUESTION_WRITER_TEXT = f"""
당신은 참여 희망자에게 보낼 확인 질문 한 문장을 작성한다.
질문은 짧고 답하기 쉬워야 한다. 의료 지식을 전제하지 않는다.

다음 JSON 형식으로만 답한다.

{{"question": "질문 한 문장"}}

{_COMMON_GUARD}
""".strip()

_INTAKE_NORMALIZER_TEXT = f"""
당신은 참여자나 의료진이 쓴 자유 문장에서 사실 조각을 뽑아내는 추출자다.
값을 해석하거나 판정하지 않는다. 문장에 적혀 있는 것만 그대로 꺼낸다.

뽑아야 하는 것은 네 가지다.
- MEASUREMENT: 검사·측정값. 예: HbA1c 7.2%, 공복혈당 130 mg/dL, 혈압 150/95 mmHg
- MEDICATION: 약물 또는 치료 상태. 예: metformin 시작, 인슐린 중단, 생활습관 관리
- ADVERSE_EVENT: 이상반응·증상. 예: 저혈당, 오심, 어지러움
- CONDITION: 상태. 예: 임신 중, 수유 중, 혈압 조절 불량

반드시 지킬 규칙:
- `span` 에는 근거가 된 원문 구간을 **글자 그대로** 옮긴다. 요약하거나 다듬지 않는다.
  원문에 없는 문자열을 쓰면 그 항목은 버려진다.
- 날짜는 원문 표현을 그대로 `when` 에 넣는다. 직접 계산하지 않는다.
  예: "3개월 전", "지난달", "2024년 5월", "어제"
- 문장이 부정이면 `negated` 를 true 로 둔다. 예: "저혈당은 없었다" → negated true
- 숫자는 원문에 적힌 숫자만 쓴다. 단위도 원문 표기를 따른다.
- 확실하지 않으면 항목을 만들지 말고 빼둔다. 추측해서 채우지 않는다.

다음 JSON 형식으로만 답한다. 설명 문장을 앞뒤에 붙이지 않는다.

{{
  "events": [
    {{
      "type": "MEASUREMENT" | "MEDICATION" | "ADVERSE_EVENT" | "CONDITION",
      "term": "무엇에 대한 것인지 (예: HbA1c, metformin, 저혈당, 임신)",
      "value": 숫자 또는 문자열 또는 null,
      "unit": "원문 단위 표기 또는 null",
      "when": "원문에 적힌 시점 표현 또는 null",
      "negated": true | false,
      "span": "근거가 된 원문 구간 그대로",
      "confidence": 0.0 에서 1.0 사이 숫자
    }}
  ]
}}

뽑을 것이 없으면 {{"events": []}} 를 반환한다.

{_COMMON_GUARD}
""".strip()


# ---------------------------------------------------------------------------
# 버전이 붙은 프롬프트
# ---------------------------------------------------------------------------

EVIDENCE_PLANNER = Prompt(
    _EVIDENCE_PLANNER_TEXT,
    id="evidence_planner",
    version="1.0",
    contract='tool_use 요청 또는 {"done": true}',
)

NLI_VERIFIER = Prompt(
    _NLI_VERIFIER_TEXT,
    id="nli_verifier",
    version="1.0",
    contract="{entailment, confidence, rationale, conflicts}",
)

CRITERION_JUDGE = Prompt(
    _CRITERION_JUDGE_TEXT,
    id="criterion_judge",
    version="1.0",
    contract=(
        "{criterion_id, proposed_status, confidence, reason, "
        "used_evidence_ids, missing_information, needs_a2a}"
    ),
)

EXPLANATION_ADMIN = Prompt(
    _EXPLANATION_ADMIN_TEXT,
    id="explanation_admin",
    version="1.1",
    contract="{summary, highlights}",
)

UNKNOWN_DELIBERATION = Prompt(
    _UNKNOWN_DELIBERATION_TEXT,
    id="unknown_deliberation",
    version="1.0",
    contract="{decisions: [{criterion_id, recommendation, source_ids, rationale}]}",
)

EXPLANATION_PATIENT = Prompt(
    _EXPLANATION_PATIENT_TEXT,
    id="explanation_patient",
    version="1.1",
    contract="{summary, highlights}",
)

QUESTION_WRITER = Prompt(
    _QUESTION_WRITER_TEXT,
    id="question_writer",
    version="1.0",
    contract="{question}",
)

INTAKE_NORMALIZER = Prompt(
    _INTAKE_NORMALIZER_TEXT,
    id="intake_normalizer",
    version="1.0",
    contract="{events: [{type, term, value, unit, when, negated, span, confidence}]}",
)

#: id 로 찾는 프롬프트 목록.
PROMPTS: dict[str, Prompt] = {
    item.id: item
    for item in (
        EVIDENCE_PLANNER,
        CRITERION_JUDGE,
        NLI_VERIFIER,
        UNKNOWN_DELIBERATION,
        EXPLANATION_ADMIN,
        EXPLANATION_PATIENT,
        QUESTION_WRITER,
        INTAKE_NORMALIZER,
    )
}


def prompt_versions() -> list[dict[str, Any]]:
    """등록된 프롬프트의 버전 정보. 상태 응답과 실행 메타데이터에 넣는다."""
    return [PROMPTS[key].to_dict() for key in sorted(PROMPTS)]


__all__ = [
    "CRITERION_JUDGE",
    "EVIDENCE_PLANNER",
    "EXPLANATION_ADMIN",
    "EXPLANATION_PATIENT",
    "INTAKE_NORMALIZER",
    "NLI_VERIFIER",
    "PROMPTS",
    "Prompt",
    "QUESTION_WRITER",
    "UNKNOWN_DELIBERATION",
    "prompt_versions",
]
