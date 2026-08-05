"""공고 기준 → 지원서 스키마 요청 (결정론적).

아키텍처 v2 는 공고의 선정·제외 기준 JSON 을 판정 원본으로 둔다. 지원서 스키마도
같은 원본에서 파생되어야 한다. 공고문을 LLM 에 넣어 필드를 상상하게 하면, 같은
공고에서 실행마다 다른 질문이 나오고 물어본 값이 어떤 기준에도 연결되지 않는 일이
생긴다.

그래서 이 모듈은 기준 목록을 그대로 읽어 필드를 만든다.

    기준(field, operator, value, unit)
      → FIELD_CATALOG 의 라벨·단위·종류
      → 지원서 JSON Schema 확장 필드 (x-criterion-field, x-unit 포함)

`x-criterion-field` 가 붙은 값은 `ApplicationSupplementBuilder` 가 그대로 관찰값으로
승격하므로, 물어본 값이 반드시 어떤 기준의 입력이 된다. Bedrock 없이도 동작하고,
같은 기준이면 같은 스키마가 나온다.

기준의 임계값은 질문 문구에 넣지 않는다. "HbA1c 7.0 이상이신가요?" 처럼 물으면
답이 조건 쪽으로 끌려간다. 값만 묻고 판정은 뒤 계층이 한다. 공고 요약
(`notice_text`)에는 조건을 적는다 — 그건 원래 공개된 공고 내용이다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from ..domain.criteria import spec_for
from ..domain.states import CriterionKind
from .service import RESERVED_FIELDS

_TYPE_BY_KIND: dict[CriterionKind, str] = {
    CriterionKind.NUMERIC_POINT: "number",
    CriterionKind.TEMPORAL_WINDOW: "integer",
    CriterionKind.CATEGORICAL: "string",
    CriterionKind.DERIVED_BOOLEAN: "boolean",
    CriterionKind.NARRATIVE: "string",
}

_MEASURED_KINDS = (CriterionKind.NUMERIC_POINT, CriterionKind.TEMPORAL_WINDOW)

ASKABLE_KINDS = (
    CriterionKind.NUMERIC_POINT,
    CriterionKind.CATEGORICAL,
    CriterionKind.DERIVED_BOOLEAN,
)
"""지원자에게 직접 물을 수 있는 조건 종류.

기준이 있다고 다 묻지는 않는다.

  - `TEMPORAL_WINDOW` (제2형 당뇨 진단 기간, 최근 365일 HbA1c 측정 횟수, 안정 치료
    기간) 는 사람이 답할 값이 아니라 기록에서 계산하는 값이다. Timeline Tool 이
    센다. 진단 시기 자체는 기본 필드 `diagnosed_conditions` 가 이미 받는다.
  - `NARRATIVE` 는 구조화 필드가 없어 자유서술 검색으로 확인한다.

물어봐야 답이 안 나오는 항목을 필수로 넣으면 재질문 5회를 그것으로 다 쓰고
`MAX_FOLLOW_UPS_REACHED` 로 끝난다. 조건 자체는 공고 요약에 그대로 남는다.
"""

EXCLUSION_NOTE = "이 공고의 제외 조건 확인에 사용합니다."

_KOREAN_PURPOSE = {
    "endocrine & metabolism": "내분비·대사",
    "endocrine&metabolism": "내분비·대사",
    "oncology": "종양",
    "hematology": "혈액질환",
    "covid-19": "코로나19",
}


def _korean(value: Any) -> str:
    text = str(value or "").strip()
    return _KOREAN_PURPOSE.get(text.casefold(), text)


@dataclass(frozen=True)
class TrialSchemaRequest:
    """`IntakeService.generate_schema` 에 넘길 입력.

    `additional_fields` 는 빈 목록일 수 있다. 그때도 `None` 을 넘기지 않는 것이
    중요하다. `None` 은 "공고문에서 LLM 이 필드를 만들어라" 라는 뜻이라서,
    기준이 없는 공고에서 의도치 않게 모델을 부르게 된다.
    """

    notice_text: str
    additional_fields: list[dict[str, Any]]


def _condition_text(criterion: dict[str, Any]) -> str:
    """기준 한 건을 사람이 읽는 조건 문구로. 공고 요약에만 쓴다."""
    low = criterion.get("value_low")
    high = criterion.get("value_high")
    operator = str(criterion.get("operator") or "").strip()
    unit = str(criterion.get("unit") or "").strip()

    if low not in (None, "") and high not in (None, ""):
        body = f"{low} ~ {high}"
    elif low not in (None, ""):
        body = f"{operator} {low}".strip()
    elif high not in (None, ""):
        body = f"{operator} {high}".strip()
    else:
        body = operator or "충족 여부 확인"
    return f"{body} {unit}".strip()


def _question(label: str, kind: CriterionKind, unit: str | None, exclusion: bool) -> str:
    """지원자에게 보여줄 질문 설명. 임계값은 넣지 않는다."""
    if kind in _MEASURED_KINDS:
        base = f"가장 최근에 확인된 {label} 값"
        if unit:
            base += f" (단위: {unit})"
    elif kind is CriterionKind.DERIVED_BOOLEAN:
        base = f"{label}에 해당하는지 여부"
    else:
        base = f"현재 {label}"
    return f"{base}. {EXCLUSION_NOTE}" if exclusion else base


class TrialSchemaBuilder:
    """공고 한 건의 기준으로 지원서 스키마 요청을 만든다."""

    def build(
        self,
        *,
        trial: dict[str, Any],
        criteria: Sequence[dict[str, Any]],
    ) -> TrialSchemaRequest:
        return TrialSchemaRequest(
            notice_text=self._notice_text(trial, criteria),
            additional_fields=self._fields(criteria),
        )

    @staticmethod
    def _notice_text(
        trial: dict[str, Any], criteria: Sequence[dict[str, Any]]
    ) -> str:
        """공고 요약. 스키마 지문(fingerprint)에 들어가므로 순서가 안정적이어야 한다."""
        lines = [
            str(trial.get("trial_name") or trial.get("trial_id") or ""),
            "",
            f"분야: {_korean(trial.get('purpose')) or '미분류'}",
            _korean(trial.get("description")),
        ]

        inclusion = [item for item in criteria if item.get("criterion_type") == "INCLUSION"]
        exclusion = [item for item in criteria if item.get("criterion_type") == "EXCLUSION"]

        for title, group in (("참여 조건", inclusion), ("제외 조건", exclusion)):
            if not group:
                continue
            lines.extend(["", f"[{title}]"])
            for item in group:
                spec = spec_for(str(item.get("field") or ""))
                lines.append(f"- {spec.label}: {_condition_text(item)}")

        return "\n".join(line for line in lines if line is not None).strip()

    @staticmethod
    def _fields(criteria: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """기준 필드를 지원서 확장 필드로. 필드 단위로 한 번만 묻는다."""
        fields: list[dict[str, Any]] = []
        seen: set[str] = set()

        for criterion in criteria:
            field_name = str(criterion.get("field") or "").strip()
            if not field_name or field_name in seen:
                continue
            # 기본 스키마가 이미 묻는 항목(만 나이 등)은 중복해서 묻지 않는다.
            # IntakeService 도 예약 필드 재정의를 거부한다.
            if field_name in RESERVED_FIELDS:
                continue
            seen.add(field_name)

            spec = spec_for(field_name)
            kind = spec.kind
            if kind not in ASKABLE_KINDS:
                continue
            # 단위는 기준이 선언한 값을 그대로 쓴다. 판정 계층이 기준 단위와
            # 관찰값 단위를 비교하므로, 여기서 다른 단위를 붙이면 검증에서 걸린다.
            unit = str(criterion.get("unit") or "").strip() or (
                spec.unit if kind in _MEASURED_KINDS else ""
            )
            exclusion = criterion.get("criterion_type") == "EXCLUSION"

            fields.append(
                {
                    "name": field_name,
                    "type": _TYPE_BY_KIND.get(kind, "string"),
                    "title": spec.label,
                    "description": _question(spec.label, kind, unit, exclusion),
                    "criterion_field": field_name,
                    "unit": unit or None,
                }
            )

        return fields


__all__ = ["TrialSchemaBuilder", "TrialSchemaRequest"]
