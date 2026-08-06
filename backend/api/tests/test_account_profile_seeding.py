"""가입 정보가 지원서 기본값으로 반영되는지 고정한다.

배경
────
가입할 때 생년월일과 성별을 이미 받는데도 지원서가 그 값을 자연어로 다시 물었고,
나이를 정규식으로 재추출했다. 첫 번째 `\\d+세` 를 집는 구현이어서
`5세 아이를 키우는 32세 여성` 의 나이가 5로 확정됐고, 그 값이 모델 추출 결과를
덮어썼다. 나이가 5로 굳으면 `18세 이상` 기준에서 방향이 틀린 부적격 판정이 난다.

여기서 고정하는 계약은 세 가지다.
  1. 제3자 나이를 본인 나이로 확정하지 않는다. 모호하면 비운다.
  2. 계정 생년월일·성별이 지원서 초기값으로 들어간다.
  3. 지원자가 명시한 값이 계정 값을 덮어쓰고, 어긋난 사실은 기록에 남는다.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.auth.profile import (
    account_profile_values,
    interest_areas,
    korean_age,
)
from app.intake.service import IntakeService
from app.intake.store import IntakeStore

TODAY = date(2026, 8, 6)

AGE_SCHEMA = {
    "properties": {
        "age": {"type": "integer", "minimum": 0, "maximum": 130},
        "sex": {
            "type": "string",
            "enum": ["male", "female", "other", "prefer_not_to_say"],
        },
    }
}


class _SilentModel:
    """도구 호출을 하지 않는 모델. 규칙 추출 경로만 타게 한다."""

    mode = "stub"

    def converse(self, **_kwargs):
        class _Response:
            tool_uses: list = []

            @staticmethod
            def json_payload():
                return {}

        return _Response()


@pytest.fixture()
def service() -> IntakeService:
    store = IntakeStore()
    base = IntakeService.base_schema()
    store.save_schema(
        {
            "schema_id": "SCHEMA-TEST",
            "trial_id": "TRIAL-TEST",
            "notice_text": "테스트 공고",
            "json_schema": base["json_schema"],
            "version": base["version"],
        }
    )
    return IntakeService(store=store, model=_SilentModel())


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("32세 여성입니다.", 32),
        ("당뇨를 10년 앓았고 올해 54세입니다.", 54),
        ("만 24세 여자이고 남자친구와 함께 지원합니다.", 24),
        # 제3자 나이가 섞인 문장
        ("5세 아이를 키우는 32세 여성입니다.", 32),
        ("어머니가 70세이고 저는 41세입니다.", 41),
        ("BMI 27.5이고 3세 반려견이 있습니다. 저는 45세 남성.", 45),
        # 본인 나이를 알 수 없으면 비운다
        ("5세 아이를 키우고 있습니다.", None),
        ("아이가 5세이고 조카가 7세입니다.", None),
    ],
)
def test_age_extraction_ignores_other_people(text: str, expected: int | None) -> None:
    assert IntakeService._extract_common_values(text, AGE_SCHEMA).get("age") == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("32세 여성입니다.", "female"),
        ("저는 45세 남성.", "male"),
        ("만 24세 여자이고 남자친구와 함께 지원합니다.", "female"),
        # 두 성별이 함께 나오면 판단하지 않는다
        ("어머니는 여성이고 저는 남성입니다.", None),
    ],
)
def test_sex_extraction_defers_when_ambiguous(text: str, expected: str | None) -> None:
    assert IntakeService._extract_common_values(text, AGE_SCHEMA).get("sex") == expected


@pytest.mark.parametrize(
    ("birthdate", "expected"),
    [
        ("2002-06-16", 24),
        ("2004-05-04", 22),
        ("2002-08-06", 24),  # 생일 당일
        ("2002-08-07", 23),  # 생일 하루 전
        ("2030-01-01", None),  # 미래
        ("2002-02-30", None),  # 존재하지 않는 날
        ("1990/01/01", None),  # 형식 불일치
        ("", None),
    ],
)
def test_korean_age(birthdate: str, expected: int | None) -> None:
    assert korean_age(birthdate, today=TODAY) == expected


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        ({"birthdate": "2002-06-16", "gender": "M"}, {"age": 24, "sex": "male"}),
        ({"birthdate": "2004-05-04", "gender": "F"}, {"age": 22, "sex": "female"}),
        ({"gender": "F"}, {"sex": "female"}),
        ({"birthdate": "2002-06-16"}, {"age": 24}),
        ({"gender": "기타"}, {}),  # 모르는 표기는 버린다
        ({}, {}),
        (None, {}),
    ],
)
def test_account_profile_values(claims: dict | None, expected: dict) -> None:
    assert account_profile_values(claims, today=TODAY) == expected


def test_profile_fills_application_without_restating(service: IntakeService) -> None:
    """나이·성별을 다시 쓰지 않아도 계정 값으로 채워진다."""
    result = service.start_application(
        schema_id="SCHEMA-TEST",
        application_text="특별히 앓는 질환은 없습니다.",
        owner_sub="sub-1",
        account_profile={"age": 24, "sex": "female"},
    )
    assert result["data"]["age"] == 24
    assert result["data"]["sex"] == "female"
    assert result["account_profile_fields"] == ["age", "sex"]
    assert result["profile_conflicts"] == []


def test_applicant_answer_overrides_profile(service: IntakeService) -> None:
    """계정 정보가 낡을 수 있으므로 지원자가 쓴 값이 우선한다."""
    result = service.start_application(
        schema_id="SCHEMA-TEST",
        application_text="저는 31세 남성입니다.",
        owner_sub="sub-1",
        account_profile={"age": 24, "sex": "female"},
    )
    assert result["data"]["age"] == 31
    assert result["data"]["sex"] == "male"
    conflicts = {item["field"]: item for item in result["profile_conflicts"]}
    assert conflicts["age"]["account_value"] == 24
    assert conflicts["age"]["applicant_value"] == 31
    assert conflicts["sex"]["applicant_value"] == "male"


def test_third_party_age_does_not_override_profile(service: IntakeService) -> None:
    """자녀 나이가 본인 나이를 덮어쓰지 않는다. 이게 원래 사고였다."""
    result = service.start_application(
        schema_id="SCHEMA-TEST",
        application_text="5세 아이를 키우고 있습니다.",
        owner_sub="sub-1",
        account_profile={"age": 24, "sex": "female"},
    )
    assert result["data"]["age"] == 24
    assert result["profile_conflicts"] == []


def test_no_profile_keeps_previous_behaviour(service: IntakeService) -> None:
    """계정 정보가 없으면 예전처럼 답변에서만 값을 얻는다."""
    result = service.start_application(
        schema_id="SCHEMA-TEST",
        application_text="42세 남성입니다.",
        owner_sub="sub-1",
        account_profile=None,
    )
    assert result["data"]["age"] == 42
    assert result["data"]["sex"] == "male"
    assert result["account_profile_fields"] == []


# ---------------------------------------------------------------------------
# 가입 설문 반영
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("medication", "expected"),
    [
        ("없음", {"current_medications": []}),
        # `있음` 은 약이 있다는 것만 알려주고 어떤 약인지는 모른다.
        ("있음", {}),
        ("잘 모름", {}),
        ("", {}),
    ],
)
def test_survey_medication_only_uses_definite_none(
    medication: str, expected: dict
) -> None:
    claims = {"custom:survey_medication": medication}
    assert account_profile_values(claims, today=TODAY) == expected


@pytest.mark.parametrize(
    ("allergy", "expected"),
    [
        ("페니실린 알레르기", {"allergies": ["페니실린 알레르기"]}),
        ("", {}),
    ],
)
def test_survey_allergy_seeds_list(allergy: str, expected: dict) -> None:
    claims = {"custom:survey_allergy": allergy}
    assert account_profile_values(claims, today=TODAY) == expected


@pytest.mark.parametrize(
    ("purpose", "expected"),
    [
        ("이전에 임상시험에 참여한 적이 있습니다", {"prior_trial_participation": True}),
        # 처음 찾아본다는 것은 참여한 적이 없다는 진술이 아니다.
        ("처음 임상시험을 찾아봅니다", {}),
        ("특정 질환의 임상시험을 찾습니다", {}),
        ("참여 가능 여부만 확인하고 싶습니다", {}),
    ],
)
def test_survey_purpose_only_confirms_prior_participation(
    purpose: str, expected: dict
) -> None:
    claims = {"custom:survey_purpose": purpose}
    assert account_profile_values(claims, today=TODAY) == expected


def test_interest_areas_never_become_diagnoses() -> None:
    """관심 분야는 희망이지 진단이 아니다. 판정 사실로 새지 않아야 한다."""
    claims = {"custom:interest_areas": "당뇨 · 내분비,피부"}
    values = account_profile_values(claims, today=TODAY)
    assert values == {}
    assert "diagnosed_conditions" not in values


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("당뇨 · 내분비,피부", ("당뇨 · 내분비", "피부")),
        ("피부", ("피부",)),
        ("", ()),
        (None, ()),
    ],
)
def test_interest_areas_parsing(raw: str | None, expected: tuple) -> None:
    claims = {"custom:interest_areas": raw} if raw is not None else {}
    assert interest_areas(claims) == expected


def test_survey_allergy_none_answer_becomes_empty_list(
    service: IntakeService,
) -> None:
    """`없음` 이라고 적으면 빈 목록이 된다. 배열 강제 변환이 처리한다."""
    result = service.start_application(
        schema_id="SCHEMA-TEST",
        application_text="42세 남성입니다.",
        owner_sub="sub-1",
        account_profile={"allergies": ["없음"]},
    )
    assert result["data"]["allergies"] == []


def test_full_survey_removes_follow_up_questions(service: IntakeService) -> None:
    """가입 정보와 설문이 다 있으면 기본 필드를 다시 묻지 않는다."""
    claims = {
        "birthdate": "2002-06-16",
        "gender": "M",
        "custom:survey_medication": "없음",
        "custom:survey_allergy": "없음",
        "custom:survey_purpose": "이전에 임상시험에 참여한 적이 있습니다",
    }
    profile = account_profile_values(claims, today=TODAY)
    assert set(profile) == {
        "age",
        "sex",
        "current_medications",
        "allergies",
        "prior_trial_participation",
    }

    result = service.start_application(
        schema_id="SCHEMA-TEST",
        application_text="고혈압으로 약을 먹은 적은 없습니다.",
        owner_sub="sub-1",
        account_profile=profile,
    )
    # 남는 질문은 진단 질환뿐이다. 나이·성별·복용약·알레르기·과거 참여는 채워졌다.
    remaining = {item["name"] for item in result["missing_fields"]}
    assert remaining == {"diagnosed_conditions"}, remaining
    assert result["data"]["age"] == 24
    assert result["data"]["prior_trial_participation"] is True
