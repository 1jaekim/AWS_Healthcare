"""Guardrails: 의료적 확정 표현 차단과 Contextual Grounding 검사.

Bedrock Guardrails 로 교체할 지점이며, 지금은 동일한 계약을 로컬 규칙으로 구현한다.
Runtime / Verifier / Next-Best-Evidence / Explanation 출력이 모두 이 필터를 통과한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class GuardrailFinding:
    """차단 또는 완화된 표현 한 건."""

    rule_id: str
    matched: str
    replacement: str
    severity: str


@dataclass
class GuardrailVerdict:
    """필터 통과 결과. text 는 완화가 적용된 최종 문구."""

    text: str
    blocked: bool = False
    findings: list[GuardrailFinding] = field(default_factory=list)

    @property
    def modified(self) -> bool:
        return bool(self.findings)


class Guardrail(Protocol):
    """Bedrock Guardrails 어댑터가 만족해야 하는 계약."""

    def review(self, text: str, *, audience: str) -> GuardrailVerdict: ...


_CERTAINTY_RULES: tuple[tuple[str, str, str, str], ...] = (
    ("GR-DIAG-01", r"확진(?:되었|됨|입니다|이다)", "진단 기록이 확인", "HIGH"),
    ("GR-DIAG-02", r"(?<!미)확정 진단", "기록상 진단", "HIGH"),
    ("GR-TREAT-01", r"복용(?:을)?\s*중단하(?:세요|십시오|시기)", "담당 의료진과 상의", "HIGH"),
    ("GR-TREAT-02", r"처방(?:을)?\s*변경하(?:세요|십시오)", "담당 의료진과 상의", "HIGH"),
    ("GR-PROG-01", r"완치(?:됩니다|된다|가능합니다)", "경과는 의료진이 판단", "HIGH"),
    ("GR-ELIG-01", r"참여가?\s*확정(?:되었|됩니다|입니다)", "참여 가능성이 있다고 판단", "MEDIUM"),
    ("GR-ELIG-02", r"반드시\s*적합", "적합 가능성이 높음", "MEDIUM"),
    ("GR-ADVICE-01", r"치료(?:를)?\s*받으세요", "의료진 상담을 권고", "MEDIUM"),
)

_BLOCK_RULES: tuple[tuple[str, str], ...] = (
    ("GR-BLOCK-01", r"(?:진단|처방)(?:을)?\s*대신\s*(?:하|해)"),
    ("GR-BLOCK-02", r"의사\s*없이\s*(?:복용|투여|중단)"),
)

_PATIENT_DISCLAIMER = (
    "이 내용은 참여 가능성 확인을 돕는 사전 안내이며 의료적 진단이나 치료 권고가 아닙니다."
)
_ADMIN_DISCLAIMER = (
    "합성 데이터 기반 사전 스크리닝 결과이며 최종 등록 판단은 연구 담당자의 검토가 필요합니다."
)


class LocalGuardrail:
    """규칙 기반 Guardrail. Bedrock Guardrails 연결 전까지 사용한다."""

    def __init__(self, *, attach_disclaimer: bool = True) -> None:
        self._attach_disclaimer = attach_disclaimer
        self._certainty = [
            (rule_id, re.compile(pattern), replacement, severity)
            for rule_id, pattern, replacement, severity in _CERTAINTY_RULES
        ]
        self._blocks = [
            (rule_id, re.compile(pattern)) for rule_id, pattern in _BLOCK_RULES
        ]

    def review(self, text: str, *, audience: str = "admin") -> GuardrailVerdict:
        """확정 표현을 완화하고, 위험 표현이면 차단한다."""
        findings: list[GuardrailFinding] = []

        for rule_id, pattern in self._blocks:
            hit = pattern.search(text)
            if hit:
                findings.append(
                    GuardrailFinding(rule_id, hit.group(0), "", "CRITICAL")
                )
                return GuardrailVerdict(
                    text="안전 정책에 따라 표시할 수 없는 내용입니다.",
                    blocked=True,
                    findings=findings,
                )

        result = text
        for rule_id, pattern, replacement, severity in self._certainty:
            for hit in pattern.finditer(result):
                findings.append(
                    GuardrailFinding(rule_id, hit.group(0), replacement, severity)
                )
            result = pattern.sub(replacement, result)

        if self._attach_disclaimer:
            disclaimer = (
                _PATIENT_DISCLAIMER if audience == "patient" else _ADMIN_DISCLAIMER
            )
            if disclaimer not in result:
                result = f"{result.rstrip()} {disclaimer}"

        return GuardrailVerdict(text=result, blocked=False, findings=findings)

    def check_grounding(
        self, statement: str, *, source_ids: list[str]
    ) -> GuardrailVerdict:
        """근거 출처가 없는 설명 문구를 차단한다 (Contextual Grounding)."""
        if source_ids:
            return GuardrailVerdict(text=statement)
        return GuardrailVerdict(
            text="근거 출처가 확인되지 않아 설명을 생성하지 않았습니다.",
            blocked=True,
            findings=[
                GuardrailFinding("GR-GROUND-01", statement[:40], "", "HIGH")
            ],
        )
