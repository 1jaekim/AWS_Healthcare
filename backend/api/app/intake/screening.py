"""완성된 지원서 JSON을 스크리닝 오케스트레이터 입력으로 변환한다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..domain.models import Observation


@dataclass(frozen=True)
class ApplicationSupplementSet:
    """지원서에서 승격된 관찰값과 제외된 필드."""

    observations: dict[str, Observation]
    skipped: tuple[dict[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": len(self.observations),
            "fields": sorted(self.observations),
            "items": [
                {
                    "field": item.field_name,
                    "value": item.value,
                    "unit": item.unit,
                    "observed_at": item.observed_at,
                    "source": item.source,
                    "source_id": item.source_id,
                }
                for item in self.observations.values()
            ],
            "skipped": list(self.skipped),
        }


class ApplicationSupplementBuilder:
    """스키마 필드를 지원서 판정 사실로 변환한다.

    `x-criterion-field`가 있으면 그 값을 기준 필드로 사용하고, 없으면 JSON
    프로퍼티 이름을 사용한다. 배열은 단일 규칙값으로 안전하게 해석할 수 없으므로
    RAG/타임라인이 확인해야 할 문맥으로 남기고 관찰값 승격에서는 제외한다.
    """

    def build(
        self,
        *,
        application: dict[str, Any],
        json_schema: dict[str, Any],
    ) -> ApplicationSupplementSet:
        observations: dict[str, Observation] = {}
        skipped: list[dict[str, str]] = []
        properties = json_schema.get("properties") or {}

        for name, value in (application.get("data") or {}).items():
            spec = properties.get(name) or {}
            field_name = str(spec.get("x-criterion-field") or name).strip()
            reason = self._reject_reason(field_name, value, spec)
            if reason:
                skipped.append({"field": name, "reason": reason})
                continue
            observations[field_name] = Observation(
                field_name=field_name,
                value=value,
                unit=(str(spec["x-unit"]) if spec.get("x-unit") else None),
                observed_at=application.get("updated_at"),
                source="APPLICATION",
                source_id=f"{application['application_id']}:{name}",
                detail=f"지원서 구조화 필드: {spec.get('title', name)}",
            )

        return ApplicationSupplementSet(
            observations=observations,
            skipped=tuple(skipped),
        )

    @staticmethod
    def _reject_reason(
        field_name: str, value: Any, spec: dict[str, Any]
    ) -> str | None:
        if not field_name:
            return "기준 필드 매핑이 없습니다."
        if value is None or (isinstance(value, str) and not value.strip()):
            return "값이 없습니다."
        if spec.get("type") == "array" or isinstance(value, (list, dict)):
            return "목록 값은 단일 기준 관찰값으로 승격하지 않습니다."
        return None


__all__ = ["ApplicationSupplementBuilder", "ApplicationSupplementSet"]
