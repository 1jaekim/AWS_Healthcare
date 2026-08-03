"""Intake 용어 → 기준 필드 정교화 사전.

Intake 에이전트는 자유 문장에서 용어를 뽑되, 어떤 필드명이 유효한지는 모른다.
유효한 필드는 `FIELD_CATALOG` 가 정하므로 그 연결을 이 모듈이 담당한다.
`agent.contracts.FieldResolver` 계약을 만족한다.

카탈로그에 없는 용어(체중, 크레아티닌, 수축기 혈압 등)는 None 을 돌려준다.
Intake 는 그 이벤트를 버리지 않고 필드 없이 통과시키므로, 기록은 남고 판정에는
쓰이지 않는다.
"""

from __future__ import annotations

import re

from .criteria import FIELD_CATALOG, FieldSpec

_SYNONYMS: dict[str, str] = {
    # HbA1c
    "hba1c": "hba1c",
    "a1c": "hba1c",
    "당화혈색소": "hba1c",
    # 공복혈당
    "fasting_glucose": "fasting_glucose",
    "공복혈당": "fasting_glucose",
    # BMI
    "bmi": "bmi",
    "체질량지수": "bmi",
    # eGFR
    "egfr": "egfr",
    "사구체여과율": "egfr",
    "신장기능": "egfr",
    # UACR
    "uacr": "uacr",
    "소변알부민": "uacr",
    "알부민뇨": "uacr",
    # 연령
    "age": "age",
    "연령": "age",
    "나이": "age",
    # 당뇨 상태
    "diabetes_status": "diabetes_status",
    "당뇨상태": "diabetes_status",
    "제2형당뇨": "diabetes_status",
    "2형당뇨": "diabetes_status",
    "내당능장애": "diabetes_status",
    "당뇨전단계": "diabetes_status",
    # 조절되지 않는 고혈압
    "uncontrolled_bp": "uncontrolled_bp",
    "조절되지않는고혈압": "uncontrolled_bp",
    "혈압조절불량": "uncontrolled_bp",
    "고혈압미조절": "uncontrolled_bp",
    "저항성고혈압": "uncontrolled_bp",
    # 임신
    "active_pregnancy": "active_pregnancy",
    "임신": "active_pregnancy",
    "임신여부": "active_pregnancy",
    "임신중": "active_pregnancy",
    # 기간 조건
    "t2d_duration_days": "t2d_duration_days",
    "당뇨진단기간": "t2d_duration_days",
    "제2형당뇨진단기간": "t2d_duration_days",
    "stable_regimen_days": "stable_regimen_days",
    "안정치료기간": "stable_regimen_days",
    "hba1c_count_365d": "hba1c_count_365d",
    "최근365일hba1c측정횟수": "hba1c_count_365d",
}

_NON_WORD = re.compile(r"[\s·/()\[\]{}.,\-_]+")


def _key(term: str) -> str:
    """조회 키를 만든다. 공백·구분자를 지우고 소문자로 맞춘다."""
    return _NON_WORD.sub("", str(term)).lower()


_LOOKUP: dict[str, str] = {_key(alias): target for alias, target in _SYNONYMS.items()}


class CatalogFieldResolver:
    """FIELD_CATALOG 기준으로 용어를 정교화한다."""

    def resolve(self, term: str) -> FieldSpec | None:
        if not term:
            return None
        key = _key(term)
        field_name = _LOOKUP.get(key)
        if field_name is None:
            # 필드명을 그대로 넣은 경우도 받아준다.
            field_name = key if key in FIELD_CATALOG else None
        if field_name is None:
            return None
        return FIELD_CATALOG.get(field_name)


__all__ = ["CatalogFieldResolver"]
