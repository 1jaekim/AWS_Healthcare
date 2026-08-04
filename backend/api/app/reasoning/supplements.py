"""Intake 이벤트를 관찰값으로 승격한다.

참여자가 제출한 자유 문장은 Intake 가 이벤트로 정규화한다. 그 이벤트 중 기준 필드로
연결된 것만 관찰값(`Observation`)으로 올려 재판정 입력으로 쓴다.

승격 조건을 좁게 잡는다. 참여자 진술로 판정을 확정하지 않기 위한 장치다.

- 기준 필드(`field`)로 연결된 이벤트만 올린다. 체중처럼 카탈로그에 없는 값은 기록만 남는다.
- `needs_review` 가 붙은 이벤트는 올리지 않는다. 단위가 어긋나거나 범위를 벗어난 값이다.
- 값이 없는 이벤트는 올리지 않는다.
- 같은 필드가 여러 번 나오면 측정 시점이 늦은 것, 그다음 확신도가 높은 것을 쓴다.

올라간 관찰값은 출처가 `PATIENT_REPORTED` 다. Verifier 가 이 출처를 보고 상태를
확정하지 않고 검토로 올린다. 즉 답변은 `UNKNOWN` 을 `REVIEW_REQUIRED` 로 바꿀 수 있고,
`EVIDENCE_FOUND` 로 바꾸지는 못한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from ..domain.models import Observation

_PROMOTABLE_TYPES = frozenset({"MEASUREMENT", "CONDITION"})
"""약물·이상반응은 기준 필드로 직접 연결되지 않으므로 승격 대상이 아니다."""


@dataclass(frozen=True)
class Supplement:
    """승격된 관찰값 한 건과 그 출처."""

    observation: Observation
    answer_id: str
    criterion_id: str
    span: str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.observation.field_name,
            "value": self.observation.value,
            "unit": self.observation.unit,
            "observed_at": self.observation.observed_at,
            "source": self.observation.source,
            "source_id": self.observation.source_id,
            "answer_id": self.answer_id,
            "criterion_id": self.criterion_id,
            "span": self.span,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class SupplementSet:
    """필드별 승격 결과와 제외 내역."""

    items: dict[str, Supplement]
    skipped: tuple[dict[str, Any], ...] = ()

    @property
    def observations(self) -> dict[str, Observation]:
        return {name: item.observation for name, item in self.items.items()}

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": len(self.items),
            "fields": sorted(self.items),
            "items": [item.to_dict() for item in self.items.values()],
            "skipped": list(self.skipped),
        }


class SupplementBuilder:
    """답변에 딸린 Intake 이벤트를 관찰값으로 바꾼다."""

    def from_answers(self, answers: Iterable[Any]) -> SupplementSet:
        """답변 목록에서 승격 가능한 관찰값을 모은다."""
        promoted: dict[str, Supplement] = {}
        skipped: list[dict[str, Any]] = []

        for answer in answers:
            for event in getattr(answer, "events", None) or []:
                reason = self._reject_reason(event)
                if reason is not None:
                    skipped.append(
                        {
                            "answer_id": answer.answer_id,
                            "term": event.get("term"),
                            "field": event.get("field"),
                            "reason": reason,
                        }
                    )
                    continue

                candidate = self._to_supplement(answer, event)
                current = promoted.get(candidate.observation.field_name)
                if current is not None and not self._prefer(candidate, current):
                    skipped.append(
                        {
                            "answer_id": answer.answer_id,
                            "term": event.get("term"),
                            "field": event.get("field"),
                            "reason": "같은 필드에 더 최신인 답변이 있습니다.",
                        }
                    )
                    continue
                promoted[candidate.observation.field_name] = candidate

        return SupplementSet(items=promoted, skipped=tuple(skipped))

    # -- 내부 ------------------------------------------------------------

    @staticmethod
    def _reject_reason(event: dict[str, Any]) -> str | None:
        if event.get("event_type") not in _PROMOTABLE_TYPES:
            return "기준 필드로 연결되는 유형이 아닙니다."
        if not event.get("field"):
            return "등록된 기준 필드가 아닙니다."
        if event.get("value") is None:
            return "값이 없습니다."
        if event.get("needs_review"):
            return "검토가 필요한 이벤트는 승격하지 않습니다."
        return None

    @staticmethod
    def _to_supplement(answer: Any, event: dict[str, Any]) -> Supplement:
        field_name = str(event["field"])
        return Supplement(
            observation=Observation(
                field_name=field_name,
                value=event.get("value"),
                unit=event.get("unit"),
                observed_at=event.get("occurred_at"),
                source="PATIENT_REPORTED",
                source_id=answer.answer_id,
                detail=str(event.get("source_span") or "")[:200],
            ),
            answer_id=answer.answer_id,
            criterion_id=answer.criterion_id,
            span=str(event.get("source_span") or ""),
            confidence=float(event.get("confidence") or 0.0),
        )

    @staticmethod
    def _prefer(candidate: Supplement, current: Supplement) -> bool:
        """측정 시점이 늦은 값을 쓰고, 시점이 같으면 확신도로 가른다."""
        new_at = candidate.observation.observed_at or ""
        old_at = current.observation.observed_at or ""
        if new_at != old_at:
            return new_at > old_at
        return candidate.confidence > current.confidence


__all__ = ["Supplement", "SupplementBuilder", "SupplementSet"]
