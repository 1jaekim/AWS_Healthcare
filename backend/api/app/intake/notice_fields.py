"""공고문 자유 텍스트 → 지원서 확장 필드 (LLM 보강 계층).

지원서 스키마는 세 층으로 쌓인다.

    1. 기본 6항목            `BASE_PROPERTIES`. 고정. 공고와 무관하게 항상 같다.
    2. 기준 파생 항목        `TrialSchemaBuilder`. 결정론적. 판정의 입력이 된다.
    3. 공고문 파생 항목      이 모듈. LLM 이 공고문을 읽고 판단해 추가한다.

3층이 필요한 이유는 구조화된 기준 JSON 이 공고문의 전부가 아니기 때문이다. 동의
가능 여부, 방문 가능 요일, 흡연력처럼 공고문에 문장으로만 적혀 있고 아직
`FIELD_CATALOG` 에 들어오지 않은 요구사항이 있다. 2층은 그것을 만들 수 없다.

대신 3층은 다음 넷을 지킨다. 규칙 없이 모델에 맡기면 같은 공고에서 실행마다 다른
질문이 나오고, 물어본 값이 어디에도 쓰이지 않는 일이 생긴다.

1. **2층을 덮지 않는다.** 이름이나 기준 매핑이 겹치는 필드는 버린다. 판정 입력을
   모델 생성 필드로 교체하지 않는다.
2. **기준에 연결되는 필드는 단위를 기준 선언값으로 강제한다.** 모델이 다른 단위를
   붙이면 판정의 `V-UNIT` 검증에서 걸린다.
3. **답할 수 없는 항목은 만들지 않는다.** 진단 후 경과일, 최근 1년 검사 횟수처럼
   기록에서 계산하는 값은 `TEMPORAL_WINDOW` 로 2층이 이미 제외한 것들이다. 이걸
   필수로 넣으면 재질문 5회를 거기에 다 쓰고 `MAX_FOLLOW_UPS_REACHED` 로 끝난다.
4. **실패하면 빈 목록을 돌려준다.** 모델이 죽거나 스텁이어도 2층만으로 챗이
   시작된다. 공고 하나 때문에 지원 자체가 막히지 않게 한다.

같은 공고·같은 기준이면 결과를 캐시에서 돌려준다. `schema_id` 가 스키마 내용의
지문이므로, 모델 출력이 한 글자만 흔들려도 새 스키마가 생겨 화면이 매번 다른
질문을 받게 된다. temperature 는 0 이지만 그것만으로는 보장되지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from agent.model import Conversation, ModelClient, ModelError

from ..domain.criteria import spec_for
from .trial_schema import ASKABLE_KINDS

MAX_NOTICE_FIELDS = 8
"""LLM 이 추가할 수 있는 필드 수 상한.

재질문은 5회뿐이다. 3층이 8개를 넘게 만들면 2층 항목이 빠진 채로 상한에 걸린다.
"""

NOTICE_FIELD_SYSTEM = """당신은 임상시험 지원서 스키마 설계자다.
공고문을 읽고, 지원자가 직접 답할 수 있는 사실만 추가 질문 항목으로 만들어라.

규칙:
- 적격 여부를 판단하지 마라. 값을 묻는 항목만 만든다.
- already_asked 에 있는 항목은 만들지 마라. 같은 내용을 다른 이름으로 만드는 것도 중복이다.
- 공고문에 근거가 없는 항목은 추측해서 만들지 마라. 만들 것이 없으면 빈 배열을 반환한다.
- 질문 설명에 기준 임계값을 넣지 마라. "HbA1c 7.0 이상인가요" 가 아니라 "가장 최근 HbA1c 값" 이다.
- 지원자가 답할 수 없는 항목은 만들지 마라. 진단 후 경과 일수, 최근 1년 검사 횟수처럼
  진료 기록에서 계산해야 하는 값이 그렇다.
- 민감정보(임신, 정신과 병력 등)는 공고문이 명시적으로 요구할 때만 만들어라.
- criterion_field 는 known_criterion_fields 에 있는 키만 쓸 수 있다. 해당하는 것이 없으면 null 이다.

JSON 객체 하나만 반환한다:
{"fields":[{"name":"snake_case","type":"string|integer|number|boolean|array","title":"한국어 이름","description":"질문 설명","enum":[],"criterion_field":"키 또는 null","unit":"단위 또는 null","notice_evidence":"근거가 된 공고문 문구"}]}"""

_SUPPORTED_TYPES = {"string", "integer", "number", "boolean", "array"}


@dataclass(frozen=True)
class NoticeFieldSet:
    """보강 결과. 실패해도 예외를 던지지 않고 이유를 담아 돌려준다."""

    fields: tuple[dict[str, Any], ...] = ()
    status: str = "skipped"
    """`generated` · `cached` · `skipped` · `failed` 중 하나."""
    reason: str | None = None
    dropped: tuple[dict[str, str], ...] = ()
    cache_key: str | None = None
    evidence: Mapping[str, str] = field(default_factory=dict)
    """필드명 → 근거가 된 공고문 문구. 검토자가 되짚을 때 쓴다."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "count": len(self.fields),
            "fields": [str(item["name"]) for item in self.fields],
            "dropped": list(self.dropped),
            "evidence": dict(self.evidence),
        }


class NoticeFieldAugmentor:
    """공고문에서 기준 파생 항목 밖의 확인 항목을 만든다."""

    def __init__(
        self,
        *,
        model: ModelClient,
        store: Any | None = None,
        max_fields: int = MAX_NOTICE_FIELDS,
    ) -> None:
        self._model = model
        self._store = store
        self._max_fields = max_fields

    def augment(
        self,
        *,
        trial_id: str,
        notice_text: str,
        reserved_names: set[str],
        covered_criterion_fields: set[str],
        criterion_units: Mapping[str, str | None],
    ) -> NoticeFieldSet:
        """공고문에서 3층 필드를 만든다.

        `reserved_names` 는 기본 필드와 기준 파생 필드 이름 전체다.
        `covered_criterion_fields` 는 2층이 이미 묻고 있는 기준 필드 키다.
        `criterion_units` 는 이 공고 기준이 선언한 단위로, 모델이 붙인 단위를
        덮어쓰는 데 쓴다.
        """
        text = notice_text.strip()
        if not text:
            return NoticeFieldSet(status="skipped", reason="공고문이 비어 있습니다.")

        if getattr(self._model, "mode", "unknown") == "stub":
            return NoticeFieldSet(
                status="skipped",
                reason="모델이 스텁 모드입니다. 기준 파생 항목만 사용합니다.",
            )

        cache_key = self._cache_key(trial_id, text, criterion_units)
        cached = self._cached(cache_key)
        if cached is not None:
            return cached

        try:
            raw_fields = self._call_model(trial_id, text, reserved_names, criterion_units)
        except ModelError as exc:
            # 공고 하나의 보강 실패가 지원 흐름을 막지 않는다.
            return NoticeFieldSet(
                status="failed",
                reason=f"공고문 필드 생성에 실패했습니다: {exc}",
                cache_key=cache_key,
            )
        except (ValueError, TypeError) as exc:
            return NoticeFieldSet(
                status="failed",
                reason=f"모델 응답을 읽을 수 없습니다: {exc}",
                cache_key=cache_key,
            )

        result = self._sanitize(
            raw_fields,
            reserved_names=reserved_names,
            covered_criterion_fields=covered_criterion_fields,
            criterion_units=criterion_units,
            cache_key=cache_key,
        )
        self._remember(cache_key, result)
        return result

    # -- 모델 호출 ---------------------------------------------------------

    def _call_model(
        self,
        trial_id: str,
        notice_text: str,
        reserved_names: set[str],
        criterion_units: Mapping[str, str | None],
    ) -> list[dict[str, Any]]:
        conversation = Conversation()
        conversation.user_text(
            json.dumps(
                {
                    "task": "notice_extra_fields",
                    "trial_id": trial_id,
                    "notice": notice_text,
                    "already_asked": sorted(reserved_names),
                    "known_criterion_fields": sorted(criterion_units),
                    "max_fields": self._max_fields,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        response = self._model.converse(
            conversation=conversation, system=NOTICE_FIELD_SYSTEM
        )
        payload = response.json_payload() or {}
        raw = payload.get("fields")
        if not isinstance(raw, list):
            raise ValueError("모델이 fields 배열을 반환하지 않았습니다.")
        return [item for item in raw if isinstance(item, dict)]

    # -- 검증 -------------------------------------------------------------

    def _sanitize(
        self,
        raw_fields: Sequence[dict[str, Any]],
        *,
        reserved_names: set[str],
        covered_criterion_fields: set[str],
        criterion_units: Mapping[str, str | None],
        cache_key: str,
    ) -> NoticeFieldSet:
        """모델 출력에서 쓸 수 있는 필드만 남긴다.

        버린 필드는 이유와 함께 남긴다. 조용히 사라지면 왜 그 항목을 안 묻는지
        나중에 알 수 없다.
        """
        kept: list[dict[str, Any]] = []
        dropped: list[dict[str, str]] = []
        evidence: dict[str, str] = {}
        seen: set[str] = set()
        # 2층이 이미 확보한 기준 매핑. 모델이 같은 기준을 다시 묻는 것을 막는다.
        claimed = set(covered_criterion_fields)

        for item in raw_fields:
            name = self._slug(item.get("name"))
            if not name:
                dropped.append({"field": str(item.get("name", "")), "reason": "이름이 없습니다."})
                continue
            if name in reserved_names or name in seen:
                dropped.append({"field": name, "reason": "이미 묻는 항목입니다."})
                continue

            field_type = str(item.get("type") or "string")
            if field_type not in _SUPPORTED_TYPES:
                dropped.append(
                    {"field": name, "reason": f"지원하지 않는 타입입니다: {field_type}"}
                )
                continue

            criterion_field = self._slug(item.get("criterion_field"))
            if criterion_field and criterion_field not in criterion_units:
                # 이 공고 기준에 없는 키를 매핑하면 승격 단계에서 엉뚱한 필드로
                # 들어간다. 매핑만 떼고 항목 자체는 살린다.
                dropped.append(
                    {
                        "field": name,
                        "reason": f"기준에 없는 매핑을 제거했습니다: {criterion_field}",
                    }
                )
                criterion_field = ""
            if criterion_field and criterion_field in claimed:
                dropped.append(
                    {"field": name, "reason": "기준 파생 항목이 이미 묻고 있습니다."}
                )
                continue
            if criterion_field and criterion_field in reserved_names:
                # `age` 처럼 기본 필드이면서 기준 필드 키이기도 한 경우다. 2층은
                # 예약 필드라 건너뛰므로 covered 에 없지만, 기본 항목이 이미 같은
                # 이름으로 관찰값을 올린다. 매핑을 허용하면 같은 것을 두 번 묻고
                # 승격 단계에서 한쪽이 다른 쪽을 덮는다.
                dropped.append(
                    {
                        "field": name,
                        "reason": f"기본 항목이 이미 묻고 있습니다: {criterion_field}",
                    }
                )
                continue
            if criterion_field and spec_for(criterion_field).kind not in ASKABLE_KINDS:
                # 2층이 물을 수 없다고 판단한 종류다. TEMPORAL_WINDOW(진단 후 경과일,
                # 최근 1년 검사 횟수)는 기록에서 계산하는 값이고, NARRATIVE 는
                # 구조화 필드가 없어 자유서술 검색으로 확인한다. 모델이 이쪽으로
                # 매핑했다면 지원자가 답할 수 없는 값을 묻고 있다는 뜻이라 항목째
                # 버린다. 남겨두면 재질문 5회를 여기에 다 쓴다.
                dropped.append(
                    {
                        "field": name,
                        "reason": (
                            f"지원자가 답할 수 없는 기준 종류입니다: {criterion_field}"
                        ),
                    }
                )
                continue

            if len(kept) >= self._max_fields:
                dropped.append({"field": name, "reason": "필드 수 상한을 넘었습니다."})
                continue

            spec: dict[str, Any] = {
                "name": name,
                "type": field_type,
                "title": str(item.get("title") or name).strip() or name,
                "description": str(
                    item.get("description") or item.get("title") or name
                ).strip(),
                "x_source": "notice_llm",
            }
            choices = item.get("enum")
            if isinstance(choices, list) and choices:
                spec["enum"] = [str(choice) for choice in choices]
            if criterion_field:
                spec["criterion_field"] = criterion_field
                claimed.add(criterion_field)
                # 단위는 기준이 선언한 값이 우선이다. 모델이 붙인 단위를 쓰면
                # 판정의 단위 검증에서 걸린다.
                spec["unit"] = criterion_units.get(criterion_field) or None
            else:
                unit = str(item.get("unit") or "").strip()
                spec["unit"] = unit or None

            note = str(item.get("notice_evidence") or "").strip()
            if note:
                evidence[name] = note[:300]

            seen.add(name)
            kept.append(spec)

        return NoticeFieldSet(
            fields=tuple(kept),
            status="generated",
            reason=None if kept else "공고문에서 추가할 항목을 찾지 못했습니다.",
            dropped=tuple(dropped),
            cache_key=cache_key,
            evidence=evidence,
        )

    # -- 캐시 -------------------------------------------------------------

    @staticmethod
    def _cache_key(
        trial_id: str, notice_text: str, criterion_units: Mapping[str, str | None]
    ) -> str:
        digest = hashlib.sha256(
            json.dumps(
                {
                    "trial_id": trial_id,
                    "notice": notice_text,
                    "criteria": sorted(criterion_units.items()),
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:16]
        return f"NOTICE-FIELDS-{trial_id}-{digest}"

    def _cached(self, cache_key: str) -> NoticeFieldSet | None:
        if self._store is None:
            return None
        getter = getattr(self._store, "get_notice_fields", None)
        if getter is None:
            return None
        payload = getter(cache_key)
        if not payload:
            return None
        return NoticeFieldSet(
            fields=tuple(payload.get("fields") or ()),
            status="cached",
            reason=payload.get("reason"),
            dropped=tuple(payload.get("dropped") or ()),
            cache_key=cache_key,
            evidence=dict(payload.get("evidence") or {}),
        )

    def _remember(self, cache_key: str, result: NoticeFieldSet) -> None:
        if self._store is None:
            return
        setter = getattr(self._store, "save_notice_fields", None)
        if setter is None:
            return
        setter(
            cache_key,
            {
                "fields": [dict(item) for item in result.fields],
                "reason": result.reason,
                "dropped": [dict(item) for item in result.dropped],
                "evidence": dict(result.evidence),
            },
        )

    @staticmethod
    def _slug(value: Any) -> str:
        return re.sub(r"[^a-z0-9_]+", "_", str(value or "").lower()).strip("_")


__all__ = [
    "MAX_NOTICE_FIELDS",
    "NOTICE_FIELD_SYSTEM",
    "NoticeFieldAugmentor",
    "NoticeFieldSet",
]
