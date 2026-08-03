"""Intake 에이전트: 자유 문장을 구조화 이벤트로 정규화한다.

참여자나 의료진이 쓴 문장은 그대로는 판정에 쓸 수 없다. 이 계층이 문장을
측정값·약물·이상반응·상태 이벤트로 바꿔 놓으면 이후 계층이 관찰값처럼 다룰 수 있다.

책임 분리는 다른 에이전트와 같다.

- FM 은 문장에서 조각을 **추출**만 한다. 값을 해석하거나 판정하지 않는다.
- 정규화와 검증은 **규칙**이 한다. 날짜 계산, 필드 정교화, 단위 확인, 범위 검사.
- 원문에 없는 근거는 통과시키지 않는다. `span` 이 원문에 없으면 버린다.

모델이 없거나 실패해도 규칙 추출기만으로 동작한다. 같은 입력에 같은 결과를 낸다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

from .contracts import FieldResolver, Guardrail, Tracer
from .model import Conversation, ModelClient, ModelError
from .prompts import INTAKE_NORMALIZER

EventType = Literal["MEASUREMENT", "MEDICATION", "ADVERSE_EVENT", "CONDITION"]
DatePrecision = Literal["DAY", "MONTH", "APPROX"]
Origin = Literal["rule", "model"]

_LOW_CONFIDENCE = 0.6
_MODEL_CONFIDENCE_CAP = 0.8
"""모델 추출은 규칙 확인을 거치지 않았으므로 확신도 상한을 둔다."""


# ---------------------------------------------------------------------------
# 결과 모델
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntakeEvent:
    """자유 문장에서 뽑아 정규화한 이벤트 한 건."""

    event_type: EventType
    term: str
    """정규화된 용어. 측정값이면 `hba1c` 처럼 필드 키를 쓴다."""
    label: str
    value: float | str | bool | None
    unit: str | None
    occurred_at: str | None
    """ISO 날짜. 원문에 시점이 없으면 None."""
    date_precision: DatePrecision | None = None
    field_name: str | None = None
    """호출자 카탈로그로 정교화된 기준 필드. 등록되지 않은 용어면 None."""
    source_span: str = ""
    """근거가 된 원문 구간. 반드시 원문에 존재한다."""
    confidence: float = 0.0
    needs_review: bool = False
    origin: Origin = "rule"
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "term": self.term,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "occurred_at": self.occurred_at,
            "date_precision": self.date_precision,
            "field": self.field_name,
            "source_span": self.source_span,
            "confidence": self.confidence,
            "needs_review": self.needs_review,
            "origin": self.origin,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class IntakeResult:
    """정규화 결과."""

    text: str
    reference_date: str
    events: tuple[IntakeEvent, ...] = ()
    dropped: tuple[dict[str, Any], ...] = ()
    """버린 후보와 이유. 조용히 사라지지 않게 남긴다."""
    mode: str = "rule"
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None

    @property
    def needs_review(self) -> bool:
        """사람이 확인해야 하는 이벤트가 있는지."""
        return any(item.needs_review for item in self.events)

    def by_type(self, event_type: EventType) -> tuple[IntakeEvent, ...]:
        return tuple(item for item in self.events if item.event_type == event_type)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "reference_date": self.reference_date,
            "mode": self.mode,
            "event_count": len(self.events),
            "needs_review": self.needs_review,
            "events": [item.to_dict() for item in self.events],
            "dropped": list(self.dropped),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# 어휘
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MeasurementTerm:
    """측정값 어휘 한 건."""

    term: str
    label: str
    unit: str
    patterns: tuple[str, ...]
    low: float
    high: float


_MEASUREMENTS: tuple[MeasurementTerm, ...] = (
    MeasurementTerm(
        "hba1c", "HbA1c", "%", ("HbA1c", "A1c", "당화혈색소"), 3.0, 20.0
    ),
    MeasurementTerm(
        "fasting_glucose", "공복혈당", "mg/dL", ("공복혈당", "공복 혈당"), 20.0, 600.0
    ),
    MeasurementTerm(
        "random_glucose",
        "임의혈당",
        "mg/dL",
        ("임의혈당", "임의 혈당", "식후혈당", "식후 혈당"),
        20.0,
        800.0,
    ),
    MeasurementTerm("bmi", "BMI", "kg/m2", ("BMI", "체질량지수"), 10.0, 70.0),
    MeasurementTerm("weight_kg", "체중", "kg", ("체중", "몸무게"), 20.0, 300.0),
    MeasurementTerm("height_cm", "키", "cm", ("신장", "키"), 100.0, 230.0),
    MeasurementTerm(
        "creatinine", "크레아티닌", "mg/dL", ("크레아티닌",), 0.1, 15.0
    ),
    MeasurementTerm(
        "egfr",
        "eGFR",
        "mL/min/1.73m2",
        ("eGFR", "사구체여과율", "신장기능"),
        1.0,
        200.0,
    ),
    MeasurementTerm(
        "uacr", "UACR", "mg/g", ("UACR", "소변 알부민", "알부민뇨"), 0.0, 5000.0
    ),
    MeasurementTerm("age", "연령", "years", ("연령", "나이"), 0.0, 120.0),
)

_BP_TERMS: dict[str, tuple[str, float, float]] = {
    "systolic_bp": ("수축기 혈압", 60.0, 260.0),
    "diastolic_bp": ("이완기 혈압", 30.0, 160.0),
}

_MEDICATIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("metformin", "metformin", ("metformin", "메트포르민", "메트포민")),
    ("dpp4_inhibitor", "DPP-4 억제제", ("DPP-4", "DPP4", "시타글립틴", "리나글립틴")),
    (
        "glp1_agonist",
        "GLP-1 수용체 작용제",
        ("GLP-1", "GLP1", "리라글루타이드", "세마글루타이드"),
    ),
    ("sglt2_inhibitor", "SGLT2 억제제", ("SGLT2", "다파글리플로진", "엠파글리플로진")),
    ("sulfonylurea", "설포닐우레아", ("설포닐우레아", "글리메피리드", "글리클라지드")),
    ("insulin", "인슐린", ("인슐린",)),
    ("lifestyle", "생활습관 관리", ("생활습관 관리", "생활습관관리", "식이 요법")),
)

_MEDICATION_ACTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("STOPPED", ("중단", "중지", "끊었", "끊고")),
    ("STARTED", ("시작", "개시", "복용하기")),
    ("INCREASED", ("증량", "강화", "올렸", "늘렸")),
    ("DECREASED", ("감량", "줄였", "낮췄")),
    ("CONTINUED", ("유지", "계속", "그대로")),
    ("CHANGED", ("변경", "바꿨", "교체")),
)

_ADVERSE_EVENTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("hypoglycemia", "저혈당", ("저혈당",)),
    ("hyperglycemia", "고혈당", ("고혈당",)),
    ("consciousness_change", "의식 변화", ("의식 변화", "의식변화", "의식 저하")),
    ("nausea", "오심", ("오심", "구역", "메스꺼")),
    ("vomiting", "구토", ("구토",)),
    ("diarrhea", "설사", ("설사",)),
    ("abdominal_pain", "복통", ("복통", "배가 아프", "속쓰림")),
    ("dizziness", "어지러움", ("어지러", "현기증")),
    ("headache", "두통", ("두통",)),
    ("edema", "부종", ("부종", "붓기", "다리가 부")),
    ("rash", "발진", ("발진", "두드러기")),
    ("myalgia", "근육통", ("근육통",)),
    ("ketoacidosis", "케톤산증", ("케톤산증",)),
    ("blurred_vision", "시야 흐림", ("시야 흐림", "시야가 흐")),
    ("weight_loss", "체중 감소", ("체중 감소", "살이 빠")),
    ("appetite_loss", "식욕 부진", ("식욕 부진", "입맛이 없")),
)

_CONDITIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("active_pregnancy", "임신 여부", ("임신",)),
    ("lactation", "수유 여부", ("수유", "모유")),
    (
        "uncontrolled_bp",
        "조절되지 않는 고혈압",
        ("조절되지 않는 고혈압", "혈압 조절 불량", "고혈압 미조절", "저항성 고혈압"),
    ),
    (
        "diabetes_status",
        "당뇨 상태",
        ("제2형 당뇨", "2형 당뇨", "내당능장애", "당뇨전단계", "당뇨 전단계"),
    ),
)

_NEGATION_HINTS = (
    "없",
    "부인",
    "아니",
    "미확인",
    "해당하지",
    "않았",
    "않는",
    "않다",
    "않습니다",
)
"""부정 표현 단서. `app/tools/evidence_retrieval.py` 와 같은 어휘를 쓴다."""

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_WHITESPACE = re.compile(r"\s+")
_NUMBER = r"(-?\d+(?:\.\d+)?)"


def _split_sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT.split(text) if part.strip()]


def _collapse(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def _is_negated(sentence: str) -> bool:
    return any(hint in sentence for hint in _NEGATION_HINTS)


# ---------------------------------------------------------------------------
# 날짜 정규화
# ---------------------------------------------------------------------------


def _add_months(anchor: date, months: int) -> date:
    """월 단위 가감. 말일은 해당 월의 마지막 날로 자른다."""
    total = anchor.year * 12 + (anchor.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    if month == 12:
        next_month_first = date(year + 1, 1, 1)
    else:
        next_month_first = date(year, month + 1, 1)
    last_day = (next_month_first - timedelta(days=1)).day
    return date(year, month, min(anchor.day, last_day))


_REL_UNIT_DAYS: dict[str, int] = {"일": 1, "주": 7}


def parse_when(
    expression: str | None, reference: date
) -> tuple[str | None, DatePrecision | None, str | None]:
    """원문 시점 표현을 ISO 날짜로 바꾼다.

    반환값은 (ISO 날짜, 정밀도, 메모). 해석하지 못하면 날짜가 None 이고 메모가 남는다.
    모델에게 날짜 계산을 맡기지 않기 위해 이 함수가 전담한다.
    """
    if not expression:
        return None, None, None
    raw = _collapse(str(expression))
    if not raw:
        return None, None, None

    # 절대 날짜: 2024-05-03 / 2024.5.3 / 2024/05/03
    hit = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", raw)
    if hit:
        parsed = _safe_date(int(hit[1]), int(hit[2]), int(hit[3]))
        if parsed:
            return parsed.isoformat(), "DAY", None
        return None, None, f"날짜를 해석하지 못했습니다: {raw}"

    # 2024년 5월 3일 / 2024년 5월
    hit = re.search(r"(\d{4})\s*년\s*(\d{1,2})\s*월(?:\s*(\d{1,2})\s*일)?", raw)
    if hit:
        day = int(hit[3]) if hit[3] else 1
        parsed = _safe_date(int(hit[1]), int(hit[2]), day)
        if parsed:
            return parsed.isoformat(), "DAY" if hit[3] else "MONTH", None
        return None, None, f"날짜를 해석하지 못했습니다: {raw}"

    # 올해/작년 N월
    hit = re.search(r"(올해|금년|작년|지난해)\s*(\d{1,2})\s*월", raw)
    if hit:
        year = reference.year - 1 if hit[1] in ("작년", "지난해") else reference.year
        parsed = _safe_date(year, int(hit[2]), 1)
        if parsed:
            return parsed.isoformat(), "MONTH", None
        return None, None, f"날짜를 해석하지 못했습니다: {raw}"

    # N일/주 전
    hit = re.search(rf"{_NUMBER}\s*(일|주)\s*(?:전|쯤 전|정도 전)", raw)
    if hit:
        amount = int(float(hit[1]))
        unit = hit[2]
        moved = reference - timedelta(days=amount * _REL_UNIT_DAYS[unit])
        return moved.isoformat(), "DAY" if unit == "일" else "APPROX", None

    # N개월/달 전
    hit = re.search(rf"{_NUMBER}\s*(?:개월|달)\s*(?:전|쯤 전|정도 전)", raw)
    if hit:
        moved = _add_months(reference, -int(float(hit[1])))
        return moved.isoformat(), "APPROX", None

    # N년 전
    hit = re.search(rf"{_NUMBER}\s*년\s*(?:전|쯤 전|정도 전)", raw)
    if hit:
        moved = _add_months(reference, -int(float(hit[1])) * 12)
        return moved.isoformat(), "APPROX", None

    keywords: dict[str, tuple[date, DatePrecision]] = {
        "오늘": (reference, "DAY"),
        "어제": (reference - timedelta(days=1), "DAY"),
        "그제": (reference - timedelta(days=2), "DAY"),
        "그저께": (reference - timedelta(days=2), "DAY"),
        "지난주": (reference - timedelta(days=7), "APPROX"),
        "저번주": (reference - timedelta(days=7), "APPROX"),
        "이번주": (reference, "APPROX"),
        "지난달": (_add_months(reference, -1), "MONTH"),
        "전달": (_add_months(reference, -1), "MONTH"),
        "이번달": (reference, "MONTH"),
        "작년": (_add_months(reference, -12), "APPROX"),
        "지난해": (_add_months(reference, -12), "APPROX"),
    }
    for keyword, (moved, precision) in keywords.items():
        if keyword in raw:
            return moved.isoformat(), precision, None

    # M월 D일 (연도 없음) -> 기준일 이전의 가장 가까운 시점
    hit = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", raw)
    if hit:
        month, day = int(hit[1]), int(hit[2])
        parsed = _safe_date(reference.year, month, day)
        if parsed and parsed > reference:
            parsed = _safe_date(reference.year - 1, month, day)
        if parsed:
            return parsed.isoformat(), "DAY", None

    return None, None, f"시점 표현을 해석하지 못했습니다: {raw}"


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 규칙 추출기
# ---------------------------------------------------------------------------


@dataclass
class _Candidate:
    """정규화 전 후보. 규칙과 모델이 같은 형태로 내놓는다."""

    event_type: EventType
    term: str
    label: str
    value: float | str | bool | None
    unit: str | None
    when: str | None
    span: str
    confidence: float
    origin: Origin
    negated: bool = False
    notes: list[str] = field(default_factory=list)


class RuleIntakeExtractor:
    """정규식 기반 결정론적 추출기.

    모델이 없어도 이 추출기만으로 동작한다. 숫자와 단위가 문장에 그대로 적혀 있는
    경우를 담당하므로, 모델보다 이 결과를 우선한다.
    """

    def extract(self, text: str) -> list[_Candidate]:
        found: list[_Candidate] = []
        for sentence in _split_sentences(text):
            when = self._when_expression(sentence)
            negated = _is_negated(sentence)
            found.extend(self._measurements(sentence, when))
            found.extend(self._blood_pressure(sentence, when))
            found.extend(self._medications(sentence, when, negated))
            found.extend(self._keyword_events(sentence, when, negated))
        return found

    # -- 개별 추출 ---------------------------------------------------------

    @staticmethod
    def _when_expression(sentence: str) -> str | None:
        """문장에서 시점 표현을 찾는다. 날짜 계산은 parse_when 이 한다."""
        patterns = (
            r"\d{4}[-./]\d{1,2}[-./]\d{1,2}",
            r"\d{4}\s*년\s*\d{1,2}\s*월(?:\s*\d{1,2}\s*일)?",
            r"(?:올해|금년|작년|지난해)\s*\d{1,2}\s*월",
            r"\d+\s*(?:일|주|개월|달|년)\s*(?:전|쯤 전|정도 전)",
            r"\d{1,2}\s*월\s*\d{1,2}\s*일",
            r"오늘|어제|그제|그저께|지난주|저번주|이번주|지난달|전달|이번달|작년|지난해",
        )
        for pattern in patterns:
            hit = re.search(pattern, sentence)
            if hit:
                return hit.group(0)
        return None

    def _measurements(self, sentence: str, when: str | None) -> list[_Candidate]:
        found: list[_Candidate] = []
        for spec in _MEASUREMENTS:
            for alias in spec.patterns:
                # '용어 ... 숫자 [단위]' 형태만 값으로 인정한다.
                pattern = re.compile(
                    rf"{re.escape(alias)}\s*(?:는|은|이|가|:|=)?\s*{_NUMBER}\s*"
                    rf"(%|mg/dL|mg/g|kg/m2|kg|cm|mmHg|mL/min/1\.73m2|세)?",
                    re.IGNORECASE,
                )
                hit = pattern.search(sentence)
                if hit is None:
                    continue
                found.append(
                    _Candidate(
                        event_type="MEASUREMENT",
                        term=spec.term,
                        label=spec.label,
                        value=float(hit[1]),
                        unit=self._normalize_unit(hit[2], spec.unit),
                        when=when,
                        span=hit.group(0).strip(),
                        confidence=0.9,
                        origin="rule",
                    )
                )
                break
        return found

    @staticmethod
    def _normalize_unit(raw: str | None, expected: str) -> str:
        """원문 단위가 없으면 카탈로그 단위를 채운다. '세' 는 years 로 바꾼다."""
        if not raw:
            return expected
        if raw == "세":
            return "years"
        return raw

    @staticmethod
    def _blood_pressure(sentence: str, when: str | None) -> list[_Candidate]:
        """'혈압 143/76' 은 수축기·이완기 두 이벤트로 나눈다."""
        pattern = re.compile(
            rf"혈압\s*(?:는|은|이|가|:|=)?\s*{_NUMBER}\s*/\s*{_NUMBER}\s*(mmHg)?"
        )
        hit = pattern.search(sentence)
        if hit is None:
            return []
        span = hit.group(0).strip()
        found: list[_Candidate] = []
        for term, raw in (("systolic_bp", hit[1]), ("diastolic_bp", hit[2])):
            label = _BP_TERMS[term][0]
            found.append(
                _Candidate(
                    event_type="MEASUREMENT",
                    term=term,
                    label=label,
                    value=float(raw),
                    unit="mmHg",
                    when=when,
                    span=span,
                    confidence=0.9,
                    origin="rule",
                )
            )
        return found

    @staticmethod
    def _medications(
        sentence: str, when: str | None, negated: bool
    ) -> list[_Candidate]:
        found: list[_Candidate] = []
        for term, label, aliases in _MEDICATIONS:
            alias = next(
                (
                    item
                    for item in aliases
                    if item.lower() in sentence.lower()
                ),
                None,
            )
            if alias is None:
                continue
            action = next(
                (
                    name
                    for name, cues in _MEDICATION_ACTIONS
                    if any(cue in sentence for cue in cues)
                ),
                "MENTIONED",
            )
            found.append(
                _Candidate(
                    event_type="MEDICATION",
                    term=term,
                    label=label,
                    value=action,
                    unit=None,
                    when=when,
                    span=sentence[:200],
                    confidence=0.85 if action != "MENTIONED" else 0.7,
                    origin="rule",
                    negated=negated and action == "MENTIONED",
                )
            )
        return found

    @staticmethod
    def _keyword_events(
        sentence: str, when: str | None, negated: bool
    ) -> list[_Candidate]:
        found: list[_Candidate] = []
        groups: tuple[tuple[EventType, tuple[tuple[str, str, tuple[str, ...]], ...]], ...] = (
            ("ADVERSE_EVENT", _ADVERSE_EVENTS),
            ("CONDITION", _CONDITIONS),
        )
        for event_type, catalog in groups:
            for term, label, aliases in catalog:
                alias = next(
                    (item for item in aliases if item in sentence), None
                )
                if alias is None:
                    continue
                found.append(
                    _Candidate(
                        event_type=event_type,
                        term=term,
                        label=label,
                        # 부정이면 False. 증상이 '없다'는 것도 사실이다.
                        value=not negated,
                        unit="boolean",
                        when=when,
                        span=sentence[:200],
                        confidence=0.8 if not negated else 0.75,
                        origin="rule",
                        negated=negated,
                    )
                )
        return found


# ---------------------------------------------------------------------------
# 에이전트
# ---------------------------------------------------------------------------


_RANGES: dict[str, tuple[float, float]] = {
    **{spec.term: (spec.low, spec.high) for spec in _MEASUREMENTS},
    **{term: (low, high) for term, (_, low, high) in _BP_TERMS.items()},
}

_EXPECTED_UNITS: dict[str, str] = {
    **{spec.term: spec.unit for spec in _MEASUREMENTS},
    **{term: "mmHg" for term in _BP_TERMS},
}

_VALID_TYPES: frozenset[str] = frozenset(
    {"MEASUREMENT", "MEDICATION", "ADVERSE_EVENT", "CONDITION"}
)

_ALIAS_STRIP = re.compile(r"[\s·/()\[\]{}.,\-_]+")


def _alias_key(term: str) -> str:
    return _ALIAS_STRIP.sub("", str(term)).lower()


def _build_alias_index() -> dict[str, dict[str, tuple[str, str]]]:
    """용어 표기 → (정규화 용어, 라벨) 색인.

    모델이 '당화혈색소' 처럼 표면형을 돌려줘도 규칙 결과와 같은 용어로 모이게 한다.
    이 정규화가 없으면 같은 사실이 두 이벤트로 남는다.
    """
    index: dict[str, dict[str, tuple[str, str]]] = {
        "MEASUREMENT": {},
        "MEDICATION": {},
        "ADVERSE_EVENT": {},
        "CONDITION": {},
    }
    for spec in _MEASUREMENTS:
        entry = (spec.term, spec.label)
        for alias in (spec.term, spec.label, *spec.patterns):
            index["MEASUREMENT"][_alias_key(alias)] = entry
    for term, (label, _low, _high) in _BP_TERMS.items():
        for alias in (term, label):
            index["MEASUREMENT"][_alias_key(alias)] = (term, label)
    for event_type, catalog in (
        ("MEDICATION", _MEDICATIONS),
        ("ADVERSE_EVENT", _ADVERSE_EVENTS),
        ("CONDITION", _CONDITIONS),
    ):
        for term, label, aliases in catalog:
            entry = (term, label)
            for alias in (term, label, *aliases):
                index[event_type][_alias_key(alias)] = entry
    return index


_ALIAS_INDEX = _build_alias_index()


def _canonical(
    event_type: str, term: str, label: str
) -> tuple[str, str]:
    """표면형 용어를 에이전트 어휘로 맞춘다. 모르는 용어는 그대로 둔다."""
    hit = _ALIAS_INDEX.get(event_type, {}).get(_alias_key(term))
    if hit is None:
        hit = _ALIAS_INDEX.get(event_type, {}).get(_alias_key(label))
    if hit is None:
        return term, label
    return hit


class IntakeAgent:
    """자유 문장을 이벤트로 정규화한다.

    모델은 추출만 하고, 이 클래스가 정규화·검증을 규칙으로 수행한다.
    모델을 주지 않으면 규칙 추출기만 쓰는 결정론적 모드로 동작한다.
    """

    def __init__(
        self,
        *,
        guardrail: Guardrail,
        trace: Tracer,
        model: ModelClient | None = None,
        resolver: FieldResolver | None = None,
        extractor: RuleIntakeExtractor | None = None,
    ) -> None:
        self._guardrail = guardrail
        self._trace = trace
        self._model = model
        self._resolver = resolver
        self._extractor = extractor or RuleIntakeExtractor()

    @property
    def mode(self) -> str:
        if self._model is None:
            return "deterministic"
        return f"intake:{getattr(self._model, 'mode', 'unknown')}"

    def normalize(
        self,
        text: str,
        *,
        reference_date: str | date | None = None,
        run_id: str | None = None,
    ) -> IntakeResult:
        """문장을 이벤트로 바꾼다. 해석하지 못한 후보는 dropped 에 남긴다."""
        reference = _as_date(reference_date)
        cleaned = (text or "").strip()
        if not cleaned:
            return IntakeResult(
                text="",
                reference_date=reference.isoformat(),
                mode=self.mode,
            )

        candidates = self._extractor.extract(cleaned)

        model_error: str | None = None
        input_tokens = output_tokens = 0
        if self._model is not None:
            proposed, model_error, input_tokens, output_tokens = self._ask_model(
                cleaned, run_id
            )
            candidates.extend(proposed)

        events, dropped = self._finalize(candidates, cleaned, reference)
        return IntakeResult(
            text=cleaned,
            reference_date=reference.isoformat(),
            events=events,
            dropped=dropped,
            mode=self.mode,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            error=model_error,
        )

    # -- 모델 --------------------------------------------------------------

    def _ask_model(
        self, text: str, run_id: str | None
    ) -> tuple[list[_Candidate], str | None, int, int]:
        """모델에게 후보 추출을 맡긴다. 실패하면 규칙 결과만 쓴다."""
        assert self._model is not None
        conversation = Conversation()
        conversation.user_text(
            "다음 문장에서 사실 조각을 뽑아라.\n\n"
            + json.dumps(
                {"task": "intake", "text": text}, ensure_ascii=False, indent=2
            )
        )
        try:
            with self._trace.span(
                run_id or "unattached",
                "model:intake_normalizer",
                "MODEL",
                mode=getattr(self._model, "mode", "unknown"),
            ) as attributes:
                response = self._model.converse(
                    conversation=conversation, system=INTAKE_NORMALIZER
                )
                attributes["input_tokens"] = response.input_tokens
                attributes["output_tokens"] = response.output_tokens
        except ModelError as exc:
            return [], str(exc), 0, 0

        parsed = response.json_payload()
        if parsed is None:
            return (
                [],
                "모델 응답 형식이 올바르지 않아 규칙 추출 결과만 사용했습니다.",
                response.input_tokens,
                response.output_tokens,
            )

        return (
            self._to_candidates(parsed.get("events")),
            None,
            response.input_tokens,
            response.output_tokens,
        )

    @staticmethod
    def _to_candidates(raw: Any) -> list[_Candidate]:
        """모델 출력을 후보로 바꾼다. 형식이 어긋난 항목은 조용히 뺀다."""
        if not isinstance(raw, list):
            return []
        candidates: list[_Candidate] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            event_type = str(item.get("type", "")).strip().upper()
            if event_type not in _VALID_TYPES:
                continue
            term = str(item.get("term", "")).strip()
            span = str(item.get("span", "")).strip()
            if not term or not span:
                continue
            candidates.append(
                _Candidate(
                    event_type=event_type,  # type: ignore[arg-type]
                    term=term,
                    label=term,
                    value=item.get("value"),
                    unit=(
                        str(item["unit"]).strip()
                        if item.get("unit") not in (None, "")
                        else None
                    ),
                    when=(
                        str(item["when"]).strip()
                        if item.get("when") not in (None, "")
                        else None
                    ),
                    span=span,
                    confidence=_clamp(item.get("confidence"), 0.5),
                    origin="model",
                    negated=bool(item.get("negated", False)),
                )
            )
        return candidates

    # -- 정규화 ------------------------------------------------------------

    def _finalize(
        self, candidates: list[_Candidate], text: str, reference: date
    ) -> tuple[tuple[IntakeEvent, ...], tuple[dict[str, Any], ...]]:
        haystack = _collapse(text)
        events: list[IntakeEvent] = []
        dropped: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str | None]] = set()
        seen_undated: set[tuple[str, str, str]] = set()

        for candidate in candidates:
            reason = self._reject_reason(candidate, haystack)
            if reason is not None:
                dropped.append(
                    {
                        "term": candidate.term,
                        "span": candidate.span,
                        "origin": candidate.origin,
                        "reason": reason,
                    }
                )
                continue

            event = self._build(candidate, reference)
            loose = (event.event_type, event.term, str(event.value))
            key = (*loose, event.occurred_at)
            # 시점 없는 후보는 같은 사실이 이미 날짜와 함께 잡혀 있으면 버린다.
            # 규칙 결과를 먼저 넣으므로 중복이면 모델 쪽이 밀린다.
            duplicate = key in seen or (
                event.occurred_at is None and loose in seen_undated
            )
            if duplicate:
                dropped.append(
                    {
                        "term": candidate.term,
                        "span": candidate.span,
                        "origin": candidate.origin,
                        "reason": "이미 추출된 이벤트와 중복입니다.",
                    }
                )
                continue
            seen.add(key)
            seen_undated.add(loose)
            events.append(event)

        return tuple(events), tuple(dropped)

    def _reject_reason(self, candidate: _Candidate, haystack: str) -> str | None:
        """통과시킬 수 없는 후보를 걸러낸다."""
        span = _collapse(candidate.span)
        if not span:
            return "근거 구간이 비어 있습니다."
        # 그라운딩: 원문에 없는 구간은 모델이 만들어낸 것으로 본다.
        if span not in haystack:
            return "근거 구간이 원문에 없습니다."
        verdict = self._guardrail.review(candidate.label, audience="admin")
        if verdict.blocked:
            return "안전 정책에 따라 제외했습니다."
        return None

    def _build(self, candidate: _Candidate, reference: date) -> IntakeEvent:
        notes = list(candidate.notes)
        occurred_at, precision, when_note = parse_when(candidate.when, reference)
        if when_note:
            notes.append(when_note)

        term, label, field_name = self._resolve(candidate)
        value, unit, value_notes, suspect = self._check_value(
            term, candidate.value, candidate.unit
        )
        notes.extend(value_notes)

        confidence = candidate.confidence
        if candidate.origin == "model":
            confidence = min(confidence, _MODEL_CONFIDENCE_CAP)

        if occurred_at and occurred_at > reference.isoformat():
            notes.append("기준일보다 뒤의 시점입니다.")
            suspect = True
        if candidate.when and occurred_at is None:
            suspect = True
        if field_name is None and candidate.event_type == "MEASUREMENT":
            notes.append("등록된 기준 필드로 연결되지 않았습니다.")

        needs_review = suspect or confidence < _LOW_CONFIDENCE
        return IntakeEvent(
            event_type=candidate.event_type,
            term=term,
            label=label,
            value=value,
            unit=unit,
            occurred_at=occurred_at,
            date_precision=precision,
            field_name=field_name,
            source_span=_collapse(candidate.span),
            confidence=round(confidence, 3),
            needs_review=needs_review,
            origin=candidate.origin,
            notes=tuple(notes),
        )

    def _resolve(self, candidate: _Candidate) -> tuple[str, str, str | None]:
        """용어를 에이전트 어휘로 맞춘 뒤 호출자 카탈로그 필드로 정교화한다."""
        term, label = _canonical(
            candidate.event_type, candidate.term, candidate.label
        )
        if self._resolver is None:
            return term, label, None
        for probe in (term, label, candidate.term):
            resolved = self._resolver.resolve(probe)
            if resolved is not None:
                return resolved.name, resolved.label, resolved.name
        return term, label, None

    @staticmethod
    def _check_value(
        term: str, value: Any, unit: str | None
    ) -> tuple[float | str | bool | None, str | None, list[str], bool]:
        """단위와 범위를 확인한다. 어긋나면 버리지 않고 검토 대상으로 올린다."""
        notes: list[str] = []
        suspect = False

        expected = _EXPECTED_UNITS.get(term)
        if expected and unit and _unit_key(unit) != _unit_key(expected):
            notes.append(f"단위 표기가 다릅니다: 기대 {expected} / 입력 {unit}")
            suspect = True
        if expected and not unit:
            unit = expected

        bounds = _RANGES.get(term)
        if bounds is None or value is None:
            return value, unit, notes, suspect

        try:
            number = float(value)
        except (TypeError, ValueError):
            if isinstance(value, bool):
                return value, unit, notes, suspect
            notes.append("숫자로 읽을 수 없는 값입니다.")
            return value, unit, notes, True

        low, high = bounds
        if not low <= number <= high:
            notes.append(
                f"통상 범위를 벗어난 값입니다: {number} (기대 {low}~{high})"
            )
            suspect = True
        return number, unit, notes, suspect


def _unit_key(unit: str) -> str:
    return _WHITESPACE.sub("", unit).lower()


def _clamp(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return round(max(0.0, min(1.0, number)), 3)


def _as_date(value: str | date | None) -> date:
    if isinstance(value, date):
        return value
    if value:
        try:
            return datetime.fromisoformat(str(value)[:10]).date()
        except ValueError:
            pass
    return datetime.now().date()
