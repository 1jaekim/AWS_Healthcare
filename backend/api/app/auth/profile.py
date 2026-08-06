"""계정 프로필에서 지원서 기본값을 파생한다.

왜 필요한가
──────────
가입할 때 이미 생년월일과 성별을 받는다. 그런데 지원서는 그 값을 다시 자연어로
물어보고 정규식으로 나이를 뽑아냈다. `5세 아이를 키우는 32세 여성` 같은 문장에서
첫 번째 `\\d+세` 를 집어 나이를 5로 확정하는 사고가 실제로 재현됐다. 나이가 5로
굳으면 `18세 이상` 기준에서 방향이 틀린 부적격 판정이 나온다.

생년월일이 더 나은 근거인 이유는 두 가지다. 문장 파싱이 개입하지 않고, 낡지
않는다. 지원서에 `age: 24` 를 숫자로 굳혀두면 여섯 달 뒤 판정에서 틀린다. 임상
시험 기준은 스크리닝 시점 나이를 본다.

경계
────
`Principal` 에는 임상 정보를 담지 않는다는 규칙을 유지한다. 여기서는 검증을
통과한 원본 클레임(`Principal.claims`)에서 필요한 값만 그때그때 읽는다. 파생
결과는 지원서 `data` 초기값으로만 들어가므로 판정 사실의 출처는 계속
`APPLICATION` 이다.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any

BIRTHDATE_PATTERN = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

GENDER_TO_SEX = {
    "M": "male",
    "MALE": "male",
    "남": "male",
    "남성": "male",
    "F": "female",
    "FEMALE": "female",
    "여": "female",
    "여성": "female",
}
"""Cognito `gender` 는 자유 문자열이다. 아는 표기만 매핑하고 나머지는 버린다."""

MEDICATION_NONE = "없음"
"""가입 설문 Q3 의 선택지는 없음·있음·잘 모름 세 개다.

`없음` 만 값으로 쓴다. `있음` 은 약이 있다는 것만 알려주고 어떤 약인지는 모르므로
목록을 만들 수 없다. `잘 모름` 은 모른다는 뜻이고 없다는 뜻이 아니다. 둘 다
비워 두고 지원서에서 다시 묻는다.
"""

PRIOR_TRIAL_PURPOSE = "이전에 임상시험에 참여한 적이 있습니다"
"""가입 설문 Q1 에서 이것만 과거 참여를 확정한다.

`처음 임상시험을 찾아봅니다` 는 처음 찾아본다는 뜻이지 참여한 적이 없다는 진술이
아니다. 없다고 단정하면 `과거 참여자 제외` 기준에서 틀린 방향으로 확정된다.
"""


def korean_age(birthdate: str, *, today: date | None = None) -> int | None:
    """`YYYY-MM-DD` 를 만 나이로 바꾼다. 형식이 다르면 None."""
    match = BIRTHDATE_PATTERN.match(birthdate.strip())
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        born = date(year, month, day)
    except ValueError:
        return None
    reference = today or datetime.now(UTC).date()
    if born > reference:
        return None
    age = reference.year - born.year
    if (reference.month, reference.day) < (born.month, born.day):
        age -= 1
    return age if 0 <= age <= 130 else None


def account_profile_values(
    claims: dict[str, Any] | None, *, today: date | None = None
) -> dict[str, Any]:
    """토큰 클레임에서 지원서 기본 필드 값을 만든다.

    확신할 수 있는 것만 돌려준다. 클레임이 없거나 형식이 다르면 그 필드를 빼고,
    호출하는 쪽은 빈 dict 를 정상으로 취급해야 한다.
    """
    if not claims:
        return {}
    values: dict[str, Any] = {}

    birthdate = str(claims.get("birthdate") or "").strip()
    if birthdate:
        age = korean_age(birthdate, today=today)
        if age is not None:
            values["age"] = age

    gender = str(claims.get("gender") or "").strip().upper()
    sex = GENDER_TO_SEX.get(gender)
    if sex:
        values["sex"] = sex

    # 가입 설문 Q3. `없음` 만 확정 값이다.
    if str(claims.get("custom:survey_medication") or "").strip() == MEDICATION_NONE:
        values["current_medications"] = []

    # 가입 설문 Q4. 자유 입력이고 선택 항목이다. `없음` 류는 배열 강제 변환이
    # 빈 목록으로 정리하므로 여기서는 적힌 대로 넘긴다.
    allergy = str(claims.get("custom:survey_allergy") or "").strip()
    if allergy:
        values["allergies"] = [allergy]

    # 가입 설문 Q1. 과거 참여를 명시한 선택지만 True 로 본다.
    if str(claims.get("custom:survey_purpose") or "").strip() == PRIOR_TRIAL_PURPOSE:
        values["prior_trial_participation"] = True

    # `custom:interest_areas` 는 여기에 넣지 않는다. 관심 분야는 희망이지
    # 진단이 아니다. `당뇨 · 내분비` 를 골랐다는 것이 당뇨 진단을 뜻하지 않으므로
    # `diagnosed_conditions` 로 옮기면 없는 병을 사실로 만든다. 후보 공고를
    # 좁히는 데 쓸 값이며 판정 사실이 아니다.

    return values


def interest_areas(claims: dict[str, Any] | None) -> tuple[str, ...]:
    """가입 설문에서 고른 관심 분야.

    판정 사실이 아니다. 어떤 공고를 먼저 보여줄지 정하는 데만 쓴다. 후보를
    걸러내는 데 쓰면 안 된다 — 관심 분야에 없다고 제외하면 실제로 적격인 공고가
    사용자에게 보이지 않는다. 임상시험 매칭에서 그 방향의 실수가 더 나쁘다.

    프론트엔드가 쉼표로 이어 저장한다(`당뇨 · 내분비,피부`). 분야 이름 안에는
    가운뎃점만 쓰고 쉼표를 쓰지 않는다는 전제다.
    """
    if not claims:
        return ()
    raw = str(claims.get("custom:interest_areas") or "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


__all__ = [
    "GENDER_TO_SEX",
    "MEDICATION_NONE",
    "PRIOR_TRIAL_PURPOSE",
    "account_profile_values",
    "interest_areas",
    "korean_age",
]
