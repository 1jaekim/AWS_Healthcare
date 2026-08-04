"""직접 식별자 탐지.

LLM 출력이 화면·보고서·감사 로그로 흘러가기 전에 이름·연락처·등록번호 같은
직접 식별자가 섞였는지 검사한다. MATCHING_MODEL_V2 의 Verifier 검증 항목 중
'개인정보' 항목이 이 모듈을 쓴다.

탐지는 보수적으로 잡는다. 놓치는 것보다 잡는 쪽으로 기울여야 하지만, 임상 수치를
식별자로 오인하면 정상 판정이 전부 검토로 올라간다. 그래서 형식이 뚜렷한 것만
규칙으로 둔다.

- 주민등록번호, 전화번호, 이메일, 계좌·카드처럼 자리수 형식이 정해진 값
- `person_id=123`, `환자번호 12345` 처럼 내부 식별자를 라벨과 함께 적은 경우
- `홍길동 님`, `김철수씨` 처럼 호칭이 붙은 한국어 이름

가명 키(`pt_...`)와 출처 ID(`ENC-...`, `NOTE-...`, `evt_...`)는 식별자가 아니다.
비식별 근거를 가리키는 값이므로 통과시킨다.

한계를 적어둔다. 호칭 없이 적힌 이름은 형식만으로 가려낼 수 없다. `당뇨 환자`
같은 임상 표현과 구분되지 않기 때문이다. 그래서 이 모듈은 출력에 이름이 없다는
보장이 아니라, 형식이 뚜렷한 유출을 막는 1차 방어선이다. 애초에 이름을 모델에
넣지 않는 것(`patient_key` 만 전달)이 실제 방어책이다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ALLOWED_ID = re.compile(
    r"^(?:pt_[0-9a-f]+|evt_[0-9a-f]+|NOTE-[\w.-]+|ENC-[\w.-]+|ANS-[\w.-]+)$",
    re.IGNORECASE,
)
"""식별자로 보지 않는 참조 키. 가명 키와 출처 ID."""

# (규칙 ID, 패턴, 설명)
_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "PII-RRN-01",
        re.compile(r"\b\d{6}\s*-\s*[0-4]\d{6}\b"),
        "주민등록번호 형식",
    ),
    (
        "PII-PHONE-01",
        re.compile(r"\b01[016789][\s.-]?\d{3,4}[\s.-]?\d{4}\b"),
        "휴대전화번호 형식",
    ),
    (
        "PII-PHONE-02",
        re.compile(r"\b0(?:2|[3-6]\d)[\s.-]?\d{3,4}[\s.-]?\d{4}\b"),
        "유선전화번호 형식",
    ),
    (
        "PII-EMAIL-01",
        re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
        "이메일 주소",
    ),
    (
        "PII-MRN-01",
        re.compile(
            r"(?:person[_\s]?id|patient[_\s]?id|환자\s*번호|등록\s*번호|차트\s*번호)"
            r"\s*[:=]?\s*\d{2,}"
        ),
        "내부 환자 식별자",
    ),
    (
        "PII-NAME-01",
        re.compile(r"(?<![가-힣])[가-힣]{2,4}\s*(?:님|씨)(?![가-힣]{2,})"),
        "호칭이 붙은 한국어 이름",
    ),
)

_NAME_ALLOWLIST = frozenset(
    {
        "선생님",
        "환자님",
        "보호자님",
        "담당자님",
        "고객님",
        "간호사님",
        "의료진님",
        "어머님",
        "아버님",
    }
)
"""이름이 아니라 일반 호칭인 경우. `선생님`, `보호자님` 같은 문구를 흘려보낸다."""


@dataclass(frozen=True)
class PiiFinding:
    """탐지된 직접 식별자 한 건."""

    rule_id: str
    description: str
    matched: str

    def redacted(self) -> str:
        """감사 로그에 남길 마스킹 표기. 원문 값을 남기지 않는다."""
        return f"{self.matched[:2]}***" if self.matched else "***"


def scan_pii(*texts: str | None) -> tuple[PiiFinding, ...]:
    """문자열들에서 직접 식별자를 찾는다. 없으면 빈 튜플."""
    findings: list[PiiFinding] = []
    seen: set[tuple[str, str]] = set()
    for text in texts:
        if not text:
            continue
        for rule_id, pattern, description in _RULES:
            for hit in pattern.finditer(text):
                matched = hit.group(0).strip()
                if _is_allowed(rule_id, matched):
                    continue
                key = (rule_id, matched)
                if key in seen:
                    continue
                seen.add(key)
                findings.append(PiiFinding(rule_id, description, matched))
    return tuple(findings)


def contains_pii(*texts: str | None) -> bool:
    """직접 식별자가 하나라도 있으면 True."""
    return bool(scan_pii(*texts))


def is_reference_key(value: str) -> bool:
    """가명 키·출처 ID처럼 식별자로 보지 않는 값인지 확인한다."""
    return bool(_ALLOWED_ID.match(value.strip()))


def _is_allowed(rule_id: str, matched: str) -> bool:
    if _ALLOWED_ID.match(matched):
        return True
    if rule_id == "PII-NAME-01":
        return matched.replace(" ", "") in _NAME_ALLOWLIST
    return False


__all__ = ["PiiFinding", "contains_pii", "is_reference_key", "scan_pii"]
