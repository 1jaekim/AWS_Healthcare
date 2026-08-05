"""Rule Evaluator: 수치·단위·기간을 결정론적으로 계산한다.

FM 을 사용하지 않는다. 동일 입력에 항상 동일 결과를 내야 하므로 순수 함수로 구성한다.
저장소에 직접 접근하지 않고, 다른 Tool 이 조회한 관찰값만 입력으로 받는다.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..domain.models import CriterionRule, Observation, RuleOutcome
from .base import BaseTool, ToolContext

_TRUE_TOKENS = {"true", "1", "yes", "y"}
_FALSE_TOKENS = {"false", "0", "no", "n"}


def _as_bool(token: str | None) -> bool | None:
    if token is None:
        return None
    lowered = token.strip().lower()
    if lowered in _TRUE_TOKENS:
        return True
    if lowered in _FALSE_TOKENS:
        return False
    return None


def _as_float(token: str | None) -> float | None:
    if token is None or token.strip() == "":
        return None
    try:
        return float(token)
    except ValueError:
        return None


def _format(value: float | str | bool | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


class RuleEvaluator(BaseTool):
    """기준 규칙과 관찰값을 대조해 충족 여부를 계산한다."""

    name = "rule_evaluator"
    permissions = ()  # 저장소 접근 없음. 계산 전용.

    def invoke(self, context: ToolContext, /, **kwargs: Any) -> RuleOutcome:
        """단일 기준에 대한 계산 결과를 반환한다."""
        rule: CriterionRule = kwargs["rule"]
        observation: Observation | None = kwargs.get("observation")

        expected = rule.expected_repr()
        if observation is None or observation.value is None:
            return RuleOutcome(
                criterion_id=rule.criterion_id,
                satisfied=None,
                observed_repr=None,
                expected_repr=expected,
                explanation=f"{rule.label} 관찰값을 확인할 수 없습니다.",
            )

        observed_repr = _format(observation.value)
        satisfied, detail = self._apply(rule, observation.value)
        return RuleOutcome(
            criterion_id=rule.criterion_id,
            satisfied=satisfied,
            observed_repr=observed_repr,
            expected_repr=expected,
            explanation=detail,
        )

    def _apply(
        self, rule: CriterionRule, value: float | str | bool
    ) -> tuple[bool | None, str]:
        """연산자별 판정. 판정 불가 시 None 을 반환한다."""
        operator = rule.operator
        unit = f" {rule.unit}" if rule.unit and rule.unit not in {"boolean", "category", "count"} else ""

        if operator == "between":
            low = _as_float(rule.value_low)
            high = _as_float(rule.value_high)
            numeric = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
            if low is None or high is None or numeric is None:
                return None, f"{rule.label} 범위 비교에 필요한 값이 부족합니다."
            ok = low <= numeric <= high
            return ok, (
                f"{rule.label} {numeric:g}{unit} 은 기준 범위 "
                f"{low:g}-{high:g}{unit} 에 {'해당' if ok else '미해당'}합니다."
            )

        if operator == "=":
            return self._equals(rule, value)

        if operator in {"in", "not_in"}:
            expected = self._choices(rule.value_low)
            observed = str(value).strip().casefold()
            matched = any(
                observed == choice.casefold() or observed in choice.casefold()
                for choice in expected
            )
            ok = matched if operator == "in" else not matched
            return ok, (
                f"{rule.label} 관찰값 '{value}' 은 허용 범위 "
                f"{expected}에 {'해당' if matched else '미해당'}합니다."
            )

        if operator == "has":
            expected = str(rule.value_low or "").strip().casefold()
            observed = str(value).strip().casefold()
            ok = bool(expected and (expected in observed or observed in expected))
            return ok, (
                f"{rule.label} 관찰값에서 요구 내용을 "
                f"{'확인했습니다' if ok else '확인하지 못했습니다'}."
            )

        numeric = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        threshold = _as_float(rule.value_low)
        if numeric is None or threshold is None:
            return None, f"{rule.label} 수치 비교에 필요한 값이 부족합니다."

        comparisons = {
            ">=": (numeric >= threshold, "이상"),
            ">": (numeric > threshold, "초과"),
            "<=": (numeric <= threshold, "이하"),
            "<": (numeric < threshold, "미만"),
        }
        if operator not in comparisons:
            return None, f"지원하지 않는 연산자입니다: {operator}"
        ok, phrase = comparisons[operator]
        return ok, (
            f"{rule.label} {numeric:g}{unit} 은 기준 {threshold:g}{unit} {phrase} "
            f"조건을 {'충족' if ok else '미충족'}합니다."
        )

    def _equals(
        self, rule: CriterionRule, value: float | str | bool
    ) -> tuple[bool | None, str]:
        """불리언·범주 일치 비교."""
        expected_bool = _as_bool(rule.value_low)
        if expected_bool is not None:
            if not isinstance(value, bool):
                return None, f"{rule.label} 불리언 값을 확인할 수 없습니다."
            ok = value is expected_bool
            observed = "해당" if value else "비해당"
            return ok, (
                f"{rule.label} 관찰 결과는 '{observed}' 이며 기준 "
                f"'{'해당' if expected_bool else '비해당'}' 과 "
                f"{'일치' if ok else '불일치'}합니다."
            )

        expected_text = (rule.value_low or "").strip()
        observed_text = str(value).strip()
        ok = self._category_matches(expected_text, observed_text)
        return ok, (
            f"{rule.label} 관찰값 '{observed_text}' 은 기준 '{expected_text}' 과 "
            f"{'일치' if ok else '불일치'}합니다."
        )

    @staticmethod
    def _category_matches(expected: str, observed: str) -> bool:
        """범주 코드와 한국어 표기를 함께 대조한다."""
        if not expected:
            return False
        aliases: dict[str, tuple[str, ...]] = {
            "impaired_glucose": ("내당능장애", "impaired glucose", "impaired_glucose"),
            "type2_diabetes": ("제2형 당뇨병", "type 2 diabetes", "type2_diabetes"),
            "normal": ("정상", "normal"),
        }
        candidates = aliases.get(expected.lower(), (expected,))
        lowered = observed.lower()
        return any(item.lower() in lowered or lowered in item.lower() for item in candidates)

    @staticmethod
    def _choices(value: str | None) -> list[str]:
        raw = str(value or "").strip()
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, list):
            return [str(item).strip() for item in decoded if str(item).strip()]
        return [item.strip() for item in re.split(r"[,|/]", raw) if item.strip()]
