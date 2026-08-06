"""완성 지원서의 복용약과 공고의 명시적 약물 제외조건을 대조한다.

식약처 DUR 관계는 약물 안전 근거이며 그 자체가 임상시험 제외조건은 아니다.
따라서 공고에 약물·병용금기 제외조건이 명시된 경우에만 기존 기준 결과를
결정론적으로 ``CONTRADICTED``로 바꾼다.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Sequence

from ..domain.models import CriterionResult, CriterionRule
from ..domain.states import CriterionStatus


_DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "mfds_dur_diabetes_10.json"
_MEDICATION_CONTEXT_HINTS = (
    "약물",
    "의약품",
    "성분",
    "복용",
    "투여",
    "병용",
    "금기",
    "medication",
    "medicine",
    "drug",
    "ingredient",
    "concomitant",
    "contraindicated",
)
_GENERAL_CONTRAINDICATION_HINTS = (
    "병용금기",
    "병용금지",
    "금기약물",
    "금기의약품",
    "금기성분",
    "contraindicatedmedication",
    "contraindicateddrug",
    "prohibitedmedication",
    "prohibitedconcomitant",
    "druginteraction",
)


def _normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^0-9a-z가-힣]+", "", text)


def _contains_alias(text: str, aliases: tuple[str, ...]) -> bool:
    normalized = _normalized(text)
    return any(_normalized(alias) in normalized for alias in aliases if alias)


@dataclass(frozen=True)
class DrugContraindication:
    relation_id: str
    ingredient_code: str
    ingredient_name_ko: str
    ingredient_name_en: str
    related_ingredient_code: str
    related_ingredient_name_ko: str
    related_ingredient_name_en: str
    notification_date: str
    reason: str
    status: str
    source_id: str

    @property
    def ingredient_aliases(self) -> tuple[str, ...]:
        return (self.ingredient_name_ko, self.ingredient_name_en)

    @property
    def related_aliases(self) -> tuple[str, ...]:
        return (self.related_ingredient_name_ko, self.related_ingredient_name_en)

    @property
    def display_pair(self) -> str:
        return f"{self.ingredient_name_ko}–{self.related_ingredient_name_ko}"


def load_diabetes_contraindications() -> tuple[DrugContraindication, ...]:
    """배포 패키지에 고정한 식약처 DUR 당뇨 관련 10건을 읽는다."""
    rows = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    return tuple(DrugContraindication(**row) for row in rows)


def extract_current_medications(application: dict[str, Any]) -> tuple[str, ...]:
    """지원서 배열에서 판정 가능한 약물명만 추출한다."""
    raw = (application.get("data") or {}).get("current_medications") or []
    if not isinstance(raw, list):
        return ()
    medications: list[str] = []
    for item in raw:
        value: Any = item
        if isinstance(item, dict):
            value = next(
                (
                    item.get(key)
                    for key in (
                        "ingredient_name",
                        "ingredient",
                        "medication_name",
                        "name",
                    )
                    if item.get(key)
                ),
                None,
            )
        text = str(value or "").strip()
        if text:
            medications.append(text)
    return tuple(dict.fromkeys(medications))


class MedicationSafetyEvaluator:
    """DUR 관계와 명시적 EXCLUSION 기준이 함께 있을 때만 차단한다."""

    def __init__(
        self, relations: Sequence[DrugContraindication] | None = None
    ) -> None:
        self._relations = tuple(relations or load_diabetes_contraindications())

    def apply(
        self,
        *,
        rules: Sequence[CriterionRule],
        results: Sequence[CriterionResult],
        current_medications: Sequence[str],
        application_id: str | None,
    ) -> tuple[list[CriterionResult], dict[str, Any]]:
        medications = tuple(item.strip() for item in current_medications if item.strip())
        summary: dict[str, Any] = {
            "enabled": True,
            "medication_count": len(medications),
            "known_relation_count": len(self._relations),
            "checked_exclusion_criteria": 0,
            "applied": [],
            "policy": "explicit_trial_exclusion_only",
        }
        if not medications:
            return list(results), summary

        rule_by_id = {rule.criterion_id: rule for rule in rules}
        updated: list[CriterionResult] = []
        for result in results:
            rule = rule_by_id.get(result.criterion_id)
            match = self._match(rule, medications) if rule is not None else None
            if rule is not None and rule.criterion_type == "EXCLUSION":
                summary["checked_exclusion_criteria"] += 1
            if rule is None or match is None:
                updated.append(result)
                continue

            application_source = (
                f"{application_id}:current_medications"
                if application_id
                else "current_medications"
            )
            explanation = (
                "완성 지원서의 복용약과 공고의 명시적 약물 제외조건을 "
                f"식약처 DUR 병용금기 관계로 대조했습니다. {match.display_pair}: "
                f"{match.reason}. 이 결과는 임상시험 사전 판정이며 의료진 확인이 필요합니다."
            )
            updated.append(
                replace(
                    result,
                    status=CriterionStatus.CONTRADICTED,
                    observed_value=match.display_pair,
                    confidence=max(result.confidence, 0.9),
                    explanation=explanation,
                    source_ids=tuple(
                        dict.fromkeys(
                            (*result.source_ids, application_source, match.source_id)
                        )
                    ),
                    rule_satisfied=False,
                    patient_reported=True,
                )
            )
            summary["applied"].append(
                {
                    "criterion_id": result.criterion_id,
                    "relation_id": match.relation_id,
                    "source_id": match.source_id,
                }
            )
        return updated, summary

    def _match(
        self, rule: CriterionRule, medications: tuple[str, ...]
    ) -> DrugContraindication | None:
        if rule.criterion_type != "EXCLUSION":
            return None
        criterion_text = " ".join(
            filter(
                None,
                (
                    rule.field_name,
                    rule.label,
                    rule.value_low,
                    rule.value_high,
                ),
            )
        )
        normalized_criterion = _normalized(criterion_text)
        if not any(
            _normalized(hint) in normalized_criterion
            for hint in _MEDICATION_CONTEXT_HINTS
        ):
            return None

        general_exclusion = any(
            _normalized(hint) in normalized_criterion
            for hint in _GENERAL_CONTRAINDICATION_HINTS
        )
        for relation in self._relations:
            has_ingredient = any(
                _contains_alias(item, relation.ingredient_aliases)
                for item in medications
            )
            has_related = any(
                _contains_alias(item, relation.related_aliases)
                for item in medications
            )
            criterion_has_ingredient = _contains_alias(
                criterion_text, relation.ingredient_aliases
            )
            criterion_has_related = _contains_alias(
                criterion_text, relation.related_aliases
            )
            if general_exclusion and has_ingredient and has_related:
                return relation
            if (has_ingredient and criterion_has_related) or (
                has_related and criterion_has_ingredient
            ):
                return relation
        return None


__all__ = [
    "DrugContraindication",
    "MedicationSafetyEvaluator",
    "extract_current_medications",
    "load_diabetes_contraindications",
]
