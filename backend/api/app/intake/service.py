"""공고 기반 JSON Schema 생성과 반복형 자연어 지원서 수집."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from agent.model import Conversation, ModelClient, ModelError
from .store import IntakeStore

SUPPORTED_TYPES = {"string", "integer", "number", "boolean", "array"}
MAX_DYNAMIC_FIELDS = 64
MAX_FOLLOW_UPS = 5
FIELD_SOURCES = {"base", "trial_notice", "notice_llm"}
"""`x-source` 로 허용되는 값.

  - `base`         고정 기본 항목
  - `trial_notice` 공고의 기준 JSON 에서 결정론적으로 파생된 항목. 판정 입력이 된다
  - `notice_llm`   공고문 자유 텍스트에서 LLM 이 추가한 항목
"""
BASE_PROPERTIES: dict[str, dict[str, Any]] = {
    "age": {
        "type": "integer",
        "title": "만 나이",
        "description": "지원 시점의 만 나이",
        "minimum": 0,
        "maximum": 130,
        "x-source": "base",
    },
    "sex": {
        "type": "string",
        "title": "성별",
        "description": "공고의 참여 조건 확인에 사용하는 성별",
        "enum": ["male", "female", "other", "prefer_not_to_say"],
        "x-source": "base",
    },
    "diagnosed_conditions": {
        "type": "array",
        "title": "현재 진단받은 질환",
        "description": "현재 진단받아 관리 중인 질환 목록이며, 없으면 빈 목록",
        "items": {"type": "string"},
        "x-source": "base",
    },
    "current_medications": {
        "type": "array",
        "title": "현재 복용 중인 약",
        "description": "현재 복용 중인 약 이름 목록이며, 없으면 빈 목록",
        "items": {"type": "string"},
        "x-source": "base",
    },
    "allergies": {
        "type": "array",
        "title": "알레르기",
        "description": "알고 있는 약물 또는 기타 알레르기 목록이며, 없으면 빈 목록",
        "items": {"type": "string"},
        "x-source": "base",
    },
    "prior_trial_participation": {
        "type": "boolean",
        "title": "과거 임상시험 참여 여부",
        "description": "과거 다른 임상시험에 참여한 적이 있는지 여부",
        "x-source": "base",
    },
}
RESERVED_FIELDS = set(BASE_PROPERTIES)

OTHER_PERSON_WORDS = "|".join(
    (
        "아이",
        "아기",
        "자녀",
        "아들",
        "딸",
        "조카",
        "손자",
        "손녀",
        "어머니",
        "아버지",
        "엄마",
        "아빠",
        "부모",
        "누나",
        "오빠",
        "언니",
        "형",
        "동생",
        "배우자",
        "남편",
        "아내",
        "친구",
        "반려견",
        "반려묘",
        "반려동물",
        "강아지",
        "고양이",
    )
)
"""나이 표기 옆에 붙으면 본인이 아니라는 신호. 지원자 나이 추출에서 제외한다."""
NONE_ANSWERS = {
    "없음",
    "없어요",
    "없습니다",
    "해당 없음",
    "해당없음",
    "복용하지 않음",
    "진단받은 질환 없음",
    "알레르기 없음",
    "none",
    "no",
    "n/a",
}

SCHEMA_SYSTEM = """당신은 임상시험 지원서 스키마 설계자다.
공고문에서 지원자가 직접 답해야 하는 사실만 추출하라. 적격 여부를 판단하지 마라.
기본 필드(age, sex, diagnosed_conditions, current_medications, allergies, prior_trial_participation)는 이미 있으므로 만들거나 수정하지 마라. 민감정보는 공고상 반드시 필요할 때만 추가하라.
JSON 객체 하나만 반환한다: {"fields":[{"name":"snake_case","type":"string|integer|number|boolean|array","title":"한국어 이름","description":"질문 설명","enum":[],"criterion_field":"기준 필드 키 또는 null","unit":"단위 또는 null"}]}
알 수 없는 조건을 추측하지 말고, 중복 필드를 만들지 마라."""

EXTRACTION_SYSTEM = """당신은 임상시험 지원서 정보 추출기다.
제공된 스키마에 정의된 필드만 자연어에서 추출하라. 명시되지 않은 값은 추측하지 말고 null로 둔다.
이전 값과 새 답변이 충돌하면 명확한 최신 답변만 사용한다. 적격 여부나 의학적 판단을 생성하지 마라.
반드시 submit_intake_values 도구를 호출해 확인된 값만 values에 제출하라."""


class ApplicationSchemaNotFound(LookupError):
    pass


class ApplicationNotFound(LookupError):
    pass


class InvalidGeneratedSchema(ValueError):
    pass


class IntakeExtractionError(RuntimeError):
    pass


class FollowUpLimitReached(RuntimeError):
    pass


class ApplicationNotComplete(RuntimeError):
    pass


class IntakeService:
    def __init__(self, *, store: IntakeStore, model: ModelClient) -> None:
        self._store = store
        self._model = model

    def generate_schema(
        self,
        *,
        trial_id: str,
        notice_text: str,
        additional_fields: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """기초 필드와 공고별 필드를 합쳐 고정 버전의 JSON Schema를 만든다."""
        proposed = list(additional_fields or [])
        if notice_text.strip() and additional_fields is None:
            proposed = self._generate_fields_with_model(trial_id, notice_text)
        if len(proposed) > MAX_DYNAMIC_FIELDS:
            raise InvalidGeneratedSchema(
                f"공고별 필드는 최대 {MAX_DYNAMIC_FIELDS}개까지 추가할 수 있습니다."
            )

        properties = {name: dict(spec) for name, spec in BASE_PROPERTIES.items()}
        required = list(BASE_PROPERTIES)
        for raw in proposed:
            name, spec = self._normalize_field(raw)
            if name in properties:
                raise InvalidGeneratedSchema(f"중복된 필드입니다: {name}")
            properties[name] = spec
            # 스키마에 포함된 항목은 모두 수집 대상이다. 선택 정보는 애초에
            # 공고 스키마에 추가하지 않아 COMPLETE의 의미를 단순하게 유지한다.
            required.append(name)

        json_schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "title": f"{trial_id} 임상시험 지원서",
            "additionalProperties": False,
            "properties": properties,
            "required": required,
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                {"trial_id": trial_id, "notice_text": notice_text, "schema": json_schema},
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:16]
        schema_id = f"APP-SCHEMA-{trial_id}-{fingerprint}"
        record = {
            "schema_id": schema_id,
            "trial_id": trial_id,
            "version": fingerprint,
            "base_schema_version": self.base_schema()["version"],
            "notice_text": notice_text,
            "json_schema": json_schema,
            "mode": getattr(self._model, "mode", "unknown"),
            "created_at": datetime.now(UTC).isoformat(),
        }
        self._store.save_schema(record)
        return record

    def start_application(
        self,
        *,
        schema_id: str,
        application_text: str,
        owner_sub: str = "",
        account_profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """첫 지원서를 만든다.

        `account_profile` 은 계정에서 파생한 기본값(나이·성별)이다. 가입 때 이미
        받은 값을 다시 묻지 않으려고 초기값으로 깔아둔다. 지원자가 답변에
        명시하면 그 값이 덮어쓴다 — 계정 정보가 낡았을 수 있고, 정정할 길을
        막으면 안 된다. 두 값이 다르면 `profile_conflicts` 에 남겨 사람이 볼 수
        있게 한다.
        """
        schema = self._require_schema(schema_id)
        seeded = self._validate_values(
            schema["json_schema"], dict(account_profile or {})
        )
        extracted = self._extract(schema["json_schema"], application_text, seeded)
        conflicts = [
            {
                "field": name,
                "account_value": seeded[name],
                "applicant_value": extracted[name],
            }
            for name in sorted(set(seeded) & set(extracted))
            if seeded[name] != extracted[name]
        ]
        values = {**seeded, **extracted}
        now = datetime.now(UTC).isoformat()
        record = {
            "application_id": f"APP-{uuid4().hex}",
            "schema_id": schema_id,
            "trial_id": schema["trial_id"],
            "owner_sub": owner_sub,
            "data": values,
            "account_profile_fields": sorted(seeded),
            "profile_conflicts": conflicts,
            "iteration": 1,
            "follow_up_count": 0,
            "status": "PROCESSING",
            "created_at": now,
            "updated_at": now,
        }
        self._store.save_application(record)
        return self._process(record, schema)

    def get_schema(self, schema_id: str) -> dict[str, Any]:
        return self._require_schema(schema_id)

    @staticmethod
    def base_schema() -> dict[str, Any]:
        json_schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "title": "임상시험 공통 지원서",
            "additionalProperties": False,
            "properties": {name: dict(spec) for name, spec in BASE_PROPERTIES.items()},
            "required": list(BASE_PROPERTIES),
        }
        version = hashlib.sha256(
            json.dumps(json_schema, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        return {"version": version, "json_schema": json_schema}

    def add_response(
        self, application_id: str, response_text: str, *, owner_sub: str | None = None
    ) -> dict[str, Any]:
        record = self._require_application(application_id, owner_sub=owner_sub)
        if record.get("status") == "MAX_FOLLOW_UPS_REACHED":
            raise FollowUpLimitReached(application_id)
        schema = self._require_schema(record["schema_id"])
        extracted = self._extract(
            schema["json_schema"], response_text, dict(record["data"])
        )
        record["data"] = self._merge(
            record["data"], extracted, schema["json_schema"]
        )
        record["iteration"] += 1
        record["updated_at"] = datetime.now(UTC).isoformat()
        return self._process(record, schema)

    def get_application(
        self, application_id: str, *, owner_sub: str | None = None
    ) -> dict[str, Any]:
        record = self._require_application(application_id, owner_sub=owner_sub)
        return self._response(record, self._require_schema(record["schema_id"]))

    def completed_application(
        self, application_id: str, *, owner_sub: str | None = None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """다음 파이프라인으로 넘길 완성 레코드와 고정 스키마를 반환한다."""
        record = self._require_application(application_id, owner_sub=owner_sub)
        if record.get("status") != "COMPLETE":
            raise ApplicationNotComplete(application_id)
        schema = self._require_schema(record["schema_id"])
        return record, schema

    def _generate_fields_with_model(
        self, trial_id: str, notice_text: str
    ) -> list[dict[str, Any]]:
        if getattr(self._model, "mode", "unknown") == "stub":
            raise InvalidGeneratedSchema(
                "로컬 스텁은 공고에서 필드를 생성하지 않습니다. "
                "BEDROCK_ENABLED=true로 실행하거나 additional_fields를 전달해 주세요."
            )
        conversation = Conversation()
        conversation.user_text(
            json.dumps(
                {"task": "intake_schema", "trial_id": trial_id, "notice": notice_text},
                ensure_ascii=False,
            )
        )
        try:
            response = self._model.converse(
                conversation=conversation, system=SCHEMA_SYSTEM
            )
        except ModelError as exc:
            raise InvalidGeneratedSchema(f"공고 스키마 생성에 실패했습니다: {exc}") from exc
        payload = response.json_payload()
        fields = (payload or {}).get("fields")
        if not isinstance(fields, list):
            raise InvalidGeneratedSchema("모델이 fields 배열을 반환하지 않았습니다.")
        return [item for item in fields if isinstance(item, dict)]

    def _extract(
        self,
        json_schema: dict[str, Any],
        text: str,
        current: dict[str, Any],
    ) -> dict[str, Any]:
        conversation = Conversation()
        conversation.user_text(
            json.dumps(
                {
                    "task": "intake_extract",
                    "schema": json_schema,
                    "current_values": current,
                    "applicant_text": text,
                },
                ensure_ascii=False,
            )
        )
        try:
            tool_properties = {
                name: {
                    key: value
                    for key, value in spec.items()
                    if key
                    in {"type", "description", "enum", "items", "minimum", "maximum"}
                }
                for name, spec in json_schema.get("properties", {}).items()
            }
            extraction_tool = {
                "toolSpec": {
                    "name": "submit_intake_values",
                    "description": "지원자의 자연어 답변에서 확인된 지원서 필드를 제출합니다.",
                    "inputSchema": {
                        "json": {
                            "type": "object",
                            "properties": {
                                "values": {
                                    "type": "object",
                                    "properties": tool_properties,
                                    "additionalProperties": False,
                                }
                            },
                            "required": ["values"],
                            "additionalProperties": False,
                        }
                    },
                }
            }
            response = self._model.converse(
                conversation=conversation,
                system=EXTRACTION_SYSTEM,
                tools=[extraction_tool],
            )
        except ModelError as exc:
            raise IntakeExtractionError(f"지원서 정보 추출에 실패했습니다: {exc}") from exc
        payload = response.json_payload() or {}
        for use in response.tool_uses:
            if use.name == "submit_intake_values":
                payload = use.arguments
                break
        raw_values = payload.get("values")
        if not isinstance(raw_values, dict):
            raw_values = payload.get("data")
        if not isinstance(raw_values, dict):
            # Claude가 지시와 달리 values 래퍼 없이 스키마 필드를 바로 반환하는
            # 경우도 안전하게 수용한다. 스키마 밖 키는 아래 검증에서 제거된다.
            properties = json_schema.get("properties", {})
            direct = {key: value for key, value in payload.items() if key in properties}
            raw_values = direct if direct else None
        deterministic = self._extract_common_values(text, json_schema)
        if not isinstance(raw_values, dict):
            if deterministic:
                return deterministic
            if current:
                # 이번 답변에서 새로 얻은 것이 없을 뿐이다. 계정에서 채운 값이나
                # 앞선 답변이 이미 있으면 진행할 수 있다. 여기서 막으면
                # "없습니다" 같은 정상 답변이 거부된다.
                return {}
            raise IntakeExtractionError(
                "답변을 구조화하지 못했습니다. 문장을 조금 더 구체적으로 적어주세요."
            )
        # 나이·BMI·성별·과거 참여 여부처럼 명확한 공통 항목은 규칙 추출을
        # 우선한다. 모델이 누락하거나 반대로 읽어도 사용자가 쓴 사실을 보존한다.
        return {
            **self._validate_values(json_schema, raw_values),
            **deterministic,
        }

    @staticmethod
    def _self_reported_age(text: str) -> int | None:
        """지원자 본인의 나이만 뽑는다. 확신할 수 없으면 None.

        후보가 하나면 그대로 쓴다. 여럿이면 본인 지칭 단서가 붙은 것만 고른다.
        단서가 없거나 둘 이상이면 판단을 포기한다. 가족·자녀·반려동물 나이를
        본인 나이로 확정하는 것보다 비워 두는 편이 낫다.
        """
        matches = list(re.finditer(r"(?:만\s*)?(\d{1,3})\s*(?:세|살)", text))
        if not matches:
            return None

        # 1단계: 제3자 나이를 후보에서 뺀다. "5세 아이", "어머니가 70세" 처럼
        # 가족·자녀·반려동물 나이가 본인 나이로 확정되는 것을 막는다.
        others_after = re.compile(rf"^\s*(?:{OTHER_PERSON_WORDS})")
        others_before = re.compile(rf"(?:{OTHER_PERSON_WORDS})\S{{0,3}}\s*$")
        candidates = [
            m
            for m in matches
            if not others_after.match(text[m.end() :])
            and not others_before.search(text[max(0, m.start() - 14) : m.start()])
        ]
        if not candidates:
            return None
        if len(candidates) == 1:
            return int(candidates[0].group(1))

        # 2단계: 후보가 여럿이면 본인 지칭 단서가 붙은 하나만 고른다.
        # 앞쪽 단서 "저는 41세", 뒤쪽 단서 "32세 여성" — 성별 표기는 본인을 가리킨다.
        before = re.compile(r"(?:저는|제가|제\s*나이|본인은?|나는|만)\s*$")
        after = re.compile(r"^\s*(?:여성|여자|남성|남자)")
        cued = [
            m
            for m in candidates
            if before.search(text[: m.start()]) or after.match(text[m.end() :])
        ]
        if len(cued) == 1:
            return int(cued[0].group(1))
        return None

    @staticmethod
    def _self_reported_sex(text: str) -> str | None:
        """지원자 본인의 성별만 뽑는다. 확신할 수 없으면 None.

        `남자친구`·`여자친구`·`여동생` 같은 말은 본인 성별이 아니다. 두 성별이
        같이 나오면 판단을 포기한다.
        """
        cleaned = re.sub(r"(?:남자|여자)\s*친구|남동생|여동생|남편|아내|배우자", " ", text)
        has_female = re.search(r"여성|여자", cleaned) is not None
        has_male = re.search(r"남성|남자", cleaned) is not None
        if has_female and has_male:
            return None
        if has_female:
            return "female"
        if has_male:
            return "male"
        return None

    @classmethod
    def _extract_common_values(
        cls, text: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        """한국어 짧은 답변에서 명확한 공통 필드를 결정론적으로 추출한다.

        이 결과는 모델 추출값을 덮어쓴다. 그래서 확신할 수 없으면 값을 만들지
        않는 쪽이 안전하다. 예전에는 첫 번째 `\\d+세` 를 무조건 집어서
        `5세 아이를 키우는 32세 여성` 의 나이를 5로 확정했고, 그 값이 모델의
        정답(32)을 덮어썼다. 나이가 5로 굳으면 `18세 이상` 기준에서 방향이 틀린
        부적격 판정이 나온다. 지금은 후보가 여럿이고 본인 지칭이 불분명하면
        비워 두고 모델 추출값과 계정 프로필에 맡긴다.
        """
        values: dict[str, Any] = {}
        properties = schema.get("properties", {})
        if "age" in properties:
            age = cls._self_reported_age(text)
            if age is not None:
                values["age"] = age
        bmi = re.search(r"\bBMI\s*(?:는|가|=|:)?\s*(\d{1,2}(?:\.\d+)?)", text, re.I)
        if bmi and "bmi" in properties:
            values["bmi"] = float(bmi.group(1))
        if "sex" in properties:
            sex = cls._self_reported_sex(text)
            if sex:
                values["sex"] = sex
        if "prior_trial_participation" in properties:
            if re.search(
                r"(?:임상\s*(?:시험|실험)|임상)(?:에|을|은|시험|실험)?[^.\n]{0,15}"
                r"(?:참여|해본|경험)[^.\n]{0,10}(?:없|아니)",
                text,
            ):
                values["prior_trial_participation"] = False
            elif re.search(
                r"(?:임상\s*(?:시험|실험)|임상)[^.\n]{0,15}(?:참여|해본|경험)[^.\n]{0,8}(?:있|했)",
                text,
            ):
                values["prior_trial_participation"] = True
        return cls._validate_values(schema, values)

    @classmethod
    def _normalize_field(
        cls, raw: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        name = re.sub(r"[^a-z0-9_]+", "_", str(raw.get("name", "")).lower()).strip("_")
        if not name or name in RESERVED_FIELDS:
            raise InvalidGeneratedSchema(f"유효하지 않거나 예약된 필드입니다: {name}")
        field_type = str(raw.get("type", "string"))
        if field_type not in SUPPORTED_TYPES:
            raise InvalidGeneratedSchema(f"지원하지 않는 필드 타입입니다: {field_type}")
        # 출처는 화이트리스트로만 받는다. 모델 출력이 그대로 들어오면 판정에
        # 반영되는 기준 파생 항목으로 위장할 수 있다. 프롬프트에 없는 키(`x_source`)
        # 를 쓰는 것도 같은 이유다.
        declared_source = str(raw.get("x_source") or "").strip()
        spec: dict[str, Any] = {
            "type": field_type,
            "title": str(raw.get("title") or name),
            "description": str(raw.get("description") or raw.get("title") or name),
            "x-source": (
                declared_source if declared_source in FIELD_SOURCES else "trial_notice"
            ),
        }
        choices = raw.get("enum")
        if isinstance(choices, list) and choices:
            spec["enum"] = choices
        if field_type == "array":
            spec["items"] = {"type": "string"}
        criterion_field = str(raw.get("criterion_field") or "").strip()
        if criterion_field:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", criterion_field):
                raise InvalidGeneratedSchema(
                    f"유효하지 않은 기준 필드 매핑입니다: {criterion_field}"
                )
            spec["x-criterion-field"] = criterion_field
        unit = str(raw.get("unit") or "").strip()
        if unit:
            spec["x-unit"] = unit
        return name, spec

    @classmethod
    def _validate_values(
        cls, schema: dict[str, Any], values: dict[str, Any]
    ) -> dict[str, Any]:
        validated: dict[str, Any] = {}
        for name, value in values.items():
            spec = schema.get("properties", {}).get(name)
            if spec is None or cls._is_empty(value):
                continue
            coerced = cls._coerce(value, spec)
            if coerced is not None and cls._in_bounds(coerced, spec):
                validated[name] = coerced
        return validated

    @staticmethod
    def _coerce(value: Any, spec: dict[str, Any]) -> Any | None:
        expected = spec.get("type")
        try:
            if expected == "string":
                result: Any = str(value).strip()
            elif expected == "integer" and not isinstance(value, bool):
                result = int(value)
            elif expected == "number" and not isinstance(value, bool):
                result = float(value)
            elif expected == "boolean":
                if isinstance(value, bool):
                    result = value
                elif str(value).strip().lower() in {"true", "yes", "y", "예", "네"}:
                    result = True
                elif str(value).strip().lower() in {"false", "no", "n", "아니오", "아니요"}:
                    result = False
                else:
                    return None
            elif expected == "array":
                result = value if isinstance(value, list) else [value]
                normalized = [str(item).strip() for item in result if str(item).strip()]
                if normalized and all(
                    IntakeService._is_none_answer(item) for item in normalized
                ):
                    result = []
                else:
                    result = [
                        item
                        for item in normalized
                        if not IntakeService._is_none_answer(item)
                    ]
            else:
                return None
        except (TypeError, ValueError):
            return None
        choices = spec.get("enum")
        return result if not choices or result in choices else None

    @staticmethod
    def _in_bounds(value: Any, spec: dict[str, Any]) -> bool:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in spec and value < spec["minimum"]:
                return False
            if "maximum" in spec and value > spec["maximum"]:
                return False
        return True

    @staticmethod
    def _merge(
        current: dict[str, Any],
        new: dict[str, Any],
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        """단일값은 최신 답변으로, 배열은 순서를 보존하며 누적한다.

        명시적인 빈 배열은 기존 목록을 지우는 정정 답변으로 취급한다.
        """
        merged = dict(current)
        properties = schema.get("properties", {})
        for key, value in new.items():
            if IntakeService._is_empty(value):
                continue
            if properties.get(key, {}).get("type") != "array":
                merged[key] = value
                continue
            if value == []:
                merged[key] = []
                continue
            previous = merged.get(key)
            combined = [*(previous if isinstance(previous, list) else []), *value]
            seen: set[str] = set()
            deduplicated: list[str] = []
            for item in combined:
                normalized = str(item).strip().casefold()
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                deduplicated.append(str(item).strip())
            merged[key] = deduplicated
        return merged

    @staticmethod
    def _is_empty(value: Any) -> bool:
        # 빈 배열은 지원자가 명시적으로 "없음"이라고 답한 유효 값이다.
        return value is None or (isinstance(value, str) and not value.strip())

    @staticmethod
    def _is_none_answer(value: str) -> bool:
        normalized = value.strip().casefold().rstrip(".!?。 ")
        return normalized in NONE_ANSWERS

    def _missing_fields(
        self, record: dict[str, Any], schema: dict[str, Any]
    ) -> list[dict[str, Any]]:
        required = schema["json_schema"].get("required", [])
        missing = [name for name in required if self._is_empty(record["data"].get(name))]
        properties = schema["json_schema"].get("properties", {})
        return [
            {
                "name": name,
                "title": properties[name].get("title", name),
                "description": properties[name].get("description", ""),
                "type": properties[name].get("type", "string"),
            }
            for name in missing
        ]

    def _process(
        self, record: dict[str, Any], schema: dict[str, Any]
    ) -> dict[str, Any]:
        missing_fields = self._missing_fields(record, schema)
        if not missing_fields:
            record["status"] = "COMPLETE"
            self._store.save_application(record)
            return self._response(record, schema)

        if record["follow_up_count"] >= MAX_FOLLOW_UPS:
            record["status"] = "MAX_FOLLOW_UPS_REACHED"
        else:
            record["follow_up_count"] += 1
            record["status"] = "NEEDS_MORE_INFO"
        self._store.save_application(record)
        return self._response(record, schema)

    def _response(self, record: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        missing_fields = self._missing_fields(record, schema)
        status = record.get("status", "NEEDS_MORE_INFO")
        prompt = None
        if missing_fields and status == "NEEDS_MORE_INFO":
            questions = [
                f"{index}. {item['description'] or item['title']}"
                for index, item in enumerate(missing_fields, start=1)
            ]
            prompt = "다음 질문에 아는 범위에서 답해 주세요.\n" + "\n".join(questions)
        return {
            "application_id": record["application_id"],
            "schema_id": record["schema_id"],
            "trial_id": record["trial_id"],
            "status": status,
            "data": record["data"],
            "missing_fields": missing_fields,
            "follow_up_prompt": prompt,
            "notice_text": schema["notice_text"],
            "iteration": record["iteration"],
            "follow_up_count": record["follow_up_count"],
            "max_follow_ups": MAX_FOLLOW_UPS,
            "updated_at": record["updated_at"],
            # 어떤 필드를 계정에서 채웠고 지원자 답변과 어긋난 곳이 있는지
            # 드러낸다. 값이 어디서 왔는지 화면과 감사 양쪽에서 보여야 한다.
            "account_profile_fields": list(record.get("account_profile_fields") or []),
            "profile_conflicts": list(record.get("profile_conflicts") or []),
        }

    def _require_schema(self, schema_id: str) -> dict[str, Any]:
        value = self._store.get_schema(schema_id)
        if value is None:
            raise ApplicationSchemaNotFound(schema_id)
        return value

    def _require_application(
        self, application_id: str, *, owner_sub: str | None = None
    ) -> dict[str, Any]:
        value = self._store.get_application(application_id)
        if value is None:
            raise ApplicationNotFound(application_id)
        if owner_sub is not None and value.get("owner_sub") != owner_sub:
            # 다른 사용자의 지원서 존재 여부도 노출하지 않는다.
            raise ApplicationNotFound(application_id)
        return value
