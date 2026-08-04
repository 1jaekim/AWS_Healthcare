"""Judgment Verifier: LLM 판단을 결정론적으로 검사한다.

MATCHING_MODEL_V2 의 `Verifier` 단계다. 모델이 낸 `CriterionJudgment` 을 받아
근거·시점·단위·연산자·기준 유형·개인정보·신뢰도 일곱 항목을 검사한다.

이 계층은 상태를 확정하지 않는다. 검사 결과만 만들고, 확정은 `RuleAggregator` 가
검사 결과와 규칙 판정을 함께 보고 결정한다. 검사와 확정을 나눠 두면 검사 항목을
늘려도 최종 판정 규칙이 한곳에 남는다.

FM 을 호출하지 않으므로 같은 입력에 항상 같은 검사 결과가 나온다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from agent.judge import RULE_STATUS_PROJECTION, CriterionJudgment

from ..domain.models import EvidenceBundle, Verification
from ..safety.pii import scan_pii

_LOW_CONFIDENCE = 0.55
"""이 아래는 자동 확정하지 않는다. 규칙 검증기와 같은 문턱을 쓴다."""

#: 단위 비교에서 예외로 두는 내부 표기. 값의 종류를 나타내는 꼬리표다.
_STRUCTURAL_UNITS = frozenset({"boolean", "category", "count"})

Severity = str
BLOCKING: Severity = "BLOCKING"
"""이 검사가 실패하면 OK / NOT_OK 로 확정할 수 없다."""

ADVISORY: Severity = "ADVISORY"
"""실패해도 확정을 막지는 않는다. 신뢰도와 설명에만 반영한다."""


@dataclass(frozen=True)
class VerificationCheck:
    """검증 항목 한 건의 결과."""

    check_id: str
    name: str
    passed: bool
    severity: Severity
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "name": self.name,
            "passed": self.passed,
            "severity": self.severity,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class JudgmentVerification:
    """LLM 판단에 대한 검증 결과 묶음."""

    criterion_id: str
    checks: tuple[VerificationCheck, ...]
    cited_evidence_ids: tuple[str, ...]
    fabricated_evidence_ids: tuple[str, ...]

    @property
    def failures(self) -> tuple[VerificationCheck, ...]:
        return tuple(item for item in self.checks if not item.passed)

    @property
    def blocking_failures(self) -> tuple[VerificationCheck, ...]:
        return tuple(
            item
            for item in self.failures
            if item.severity == BLOCKING
        )

    @property
    def passed(self) -> bool:
        """확정을 막는 실패가 없으면 통과."""
        return not self.blocking_failures

    @property
    def grounded(self) -> bool:
        """모델이 실제 존재하는 출처만 인용했고, 하나 이상 인용했는지."""
        return bool(self.cited_evidence_ids) and not self.fabricated_evidence_ids

    def notes(self) -> tuple[str, ...]:
        return tuple(item.detail for item in self.failures if item.detail)

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "passed": self.passed,
            "grounded": self.grounded,
            "cited_evidence_ids": list(self.cited_evidence_ids),
            "fabricated_evidence_ids": list(self.fabricated_evidence_ids),
            "failed_checks": [item.check_id for item in self.failures],
            "blocking_checks": [item.check_id for item in self.blocking_failures],
            "checks": [item.to_dict() for item in self.checks],
        }


class JudgmentVerifier:
    """LLM 판단을 일곱 항목으로 검사한다."""

    def verify(
        self,
        bundle: EvidenceBundle,
        judgment: CriterionJudgment,
        *,
        rule_verification: Verification,
        index_date: str | None = None,
    ) -> JudgmentVerification:
        """검사 결과를 만든다. 상태는 바꾸지 않는다."""
        available = tuple(dict.fromkeys(bundle.source_ids()))
        cited = tuple(dict.fromkeys(judgment.used_evidence_ids))
        fabricated = tuple(item for item in cited if item not in available)

        checks = (
            self._check_evidence(judgment, available, cited, fabricated),
            self._check_time_window(bundle, index_date),
            self._check_unit(bundle),
            self._check_operator(bundle, judgment),
            self._check_criterion_type(bundle, judgment),
            self._check_pii(judgment),
            self._check_confidence(judgment, rule_verification),
        )
        return JudgmentVerification(
            criterion_id=bundle.rule.criterion_id,
            checks=checks,
            cited_evidence_ids=tuple(item for item in cited if item in available),
            fabricated_evidence_ids=fabricated,
        )

    # -- 1. 근거 존재 ----------------------------------------------------

    @staticmethod
    def _check_evidence(
        judgment: CriterionJudgment,
        available: tuple[str, ...],
        cited: tuple[str, ...],
        fabricated: tuple[str, ...],
    ) -> VerificationCheck:
        """`used_evidence_ids` 가 실제 검색 결과에 있는지 확인한다."""
        decisive = judgment.proposed_status in {"OK", "NOT_OK"}
        if fabricated:
            return VerificationCheck(
                "V-EVIDENCE",
                "근거 존재",
                False,
                BLOCKING,
                "인용한 출처를 근거 목록에서 찾을 수 없습니다: "
                + ", ".join(sorted(fabricated)),
            )
        if decisive and not cited:
            return VerificationCheck(
                "V-EVIDENCE",
                "근거 존재",
                False,
                BLOCKING,
                "출처 ID 인용 없이 상태를 확정하려 했습니다.",
            )
        if not available:
            return VerificationCheck(
                "V-EVIDENCE",
                "근거 존재",
                not decisive,
                BLOCKING,
                "조회된 근거가 없습니다.",
            )
        return VerificationCheck(
            "V-EVIDENCE", "근거 존재", True, BLOCKING, "인용한 출처가 확인됩니다."
        )

    # -- 2. 시간 범위 ----------------------------------------------------

    @staticmethod
    def _check_time_window(
        bundle: EvidenceBundle, index_date: str | None
    ) -> VerificationCheck:
        """관찰 시점이 기준의 기간 조건 안에 있는지 확인한다."""
        rule = bundle.rule
        observation = bundle.primary_observation
        window = rule.time_window_days

        if observation is None or observation.value is None:
            return VerificationCheck(
                "V-WINDOW", "시간 범위", True, BLOCKING, "대조할 관찰값이 없습니다."
            )
        if not observation.observed_at:
            return VerificationCheck(
                "V-WINDOW",
                "시간 범위",
                False,
                BLOCKING,
                "관찰 시점이 기록되지 않아 기간 조건을 확인할 수 없습니다.",
            )
        if window is None:
            return VerificationCheck(
                "V-WINDOW", "시간 범위", True, BLOCKING, "기간 조건이 없는 기준입니다."
            )

        elapsed = _days_between(observation.observed_at, index_date)
        if elapsed is None:
            return VerificationCheck(
                "V-WINDOW",
                "시간 범위",
                False,
                ADVISORY,
                "날짜 형식을 해석할 수 없어 기간 조건을 확인하지 못했습니다.",
            )
        if elapsed < 0:
            return VerificationCheck(
                "V-WINDOW",
                "시간 범위",
                False,
                BLOCKING,
                f"관찰 시점({observation.observed_at})이 기준일보다 미래입니다.",
            )
        if elapsed > window:
            return VerificationCheck(
                "V-WINDOW",
                "시간 범위",
                False,
                BLOCKING,
                f"관찰 시점이 {elapsed}일 전으로 기준 기간 {window}일을 벗어납니다.",
            )
        return VerificationCheck(
            "V-WINDOW",
            "시간 범위",
            True,
            BLOCKING,
            f"관찰 시점이 기준 기간 {window}일 안에 있습니다.",
        )

    # -- 3. 단위 --------------------------------------------------------

    @staticmethod
    def _check_unit(bundle: EvidenceBundle) -> VerificationCheck:
        """기준 단위와 관찰 단위가 같은지 확인한다."""
        rule = bundle.rule
        observation = bundle.primary_observation
        if observation is None or not rule.unit or not observation.unit:
            return VerificationCheck(
                "V-UNIT", "단위", True, BLOCKING, "대조할 단위 표기가 없습니다."
            )
        if observation.unit == rule.unit:
            return VerificationCheck(
                "V-UNIT", "단위", True, BLOCKING, f"단위 {rule.unit} 이 일치합니다."
            )
        if observation.unit in _STRUCTURAL_UNITS or rule.unit in _STRUCTURAL_UNITS:
            return VerificationCheck(
                "V-UNIT",
                "단위",
                True,
                BLOCKING,
                "값의 종류를 나타내는 표기라 단위 비교 대상이 아닙니다.",
            )
        return VerificationCheck(
            "V-UNIT",
            "단위",
            False,
            BLOCKING,
            f"단위가 다릅니다: 기준 {rule.unit} / 관찰 {observation.unit}",
        )

    # -- 4. 연산자 ------------------------------------------------------

    @staticmethod
    def _check_operator(
        bundle: EvidenceBundle, judgment: CriterionJudgment
    ) -> VerificationCheck:
        """규칙 계산과 모델 판단이 같은 방향인지 확인한다.

        연산자 적용은 `rule_evaluator` 가 이미 결정론적으로 계산했다. 여기서는
        모델이 그 계산을 뒤집었는지만 본다. 뒤집혔다면 연산자를 잘못 읽은 것이다.
        """
        outcome = bundle.outcome
        if outcome is None or outcome.satisfied is None:
            return VerificationCheck(
                "V-OPERATOR",
                "연산자",
                True,
                ADVISORY,
                "규칙 계산 결과가 없어 대조하지 않았습니다.",
            )
        expected = "OK" if outcome.satisfied else "NOT_OK"
        if judgment.proposed_status == "UNKNOWN":
            return VerificationCheck(
                "V-OPERATOR",
                "연산자",
                True,
                ADVISORY,
                "모델이 판단을 보류해 대조하지 않았습니다.",
            )
        if judgment.proposed_status == expected:
            return VerificationCheck(
                "V-OPERATOR",
                "연산자",
                True,
                BLOCKING,
                f"규칙 계산({bundle.rule.operator})과 판단이 일치합니다.",
            )
        return VerificationCheck(
            "V-OPERATOR",
            "연산자",
            False,
            BLOCKING,
            f"규칙 계산은 {expected} 인데 판단은 {judgment.proposed_status} 입니다.",
        )

    # -- 5. 기준 유형 ---------------------------------------------------

    @staticmethod
    def _check_criterion_type(
        bundle: EvidenceBundle, judgment: CriterionJudgment
    ) -> VerificationCheck:
        """선정 기준과 제외 기준의 의미가 뒤집히지 않았는지 확인한다.

        제외 기준의 규칙은 '위험 없음'을 조건으로 기술한다(예: active_pregnancy=false).
        따라서 규칙 충족은 곧 통과다. 모델이 이를 '제외 대상이므로 OK' 처럼 반대로
        읽으면 근거 문장과 상태가 어긋난다.
        """
        rule = bundle.rule
        if rule.criterion_type != "EXCLUSION":
            return VerificationCheck(
                "V-TYPE", "기준 유형", True, BLOCKING, "선정 기준입니다."
            )
        outcome = bundle.outcome
        if outcome is None or outcome.satisfied is None:
            return VerificationCheck(
                "V-TYPE",
                "기준 유형",
                True,
                ADVISORY,
                "규칙 계산 결과가 없어 대조하지 않았습니다.",
            )
        # 제외 기준에서 규칙이 '위험 확인'(satisfied=False) 인데 모델이 OK 라고 하면
        # 제외 방향을 뒤집어 읽은 것이다.
        if outcome.satisfied is False and judgment.proposed_status == "OK":
            return VerificationCheck(
                "V-TYPE",
                "기준 유형",
                False,
                BLOCKING,
                "제외 기준에 해당하는 근거가 있는데 충족으로 판단했습니다.",
            )
        return VerificationCheck(
            "V-TYPE",
            "기준 유형",
            True,
            BLOCKING,
            "제외 기준 방향이 유지되었습니다.",
        )

    # -- 6. 개인정보 ----------------------------------------------------

    @staticmethod
    def _check_pii(judgment: CriterionJudgment) -> VerificationCheck:
        """모델 출력에 직접 식별자가 섞였는지 확인한다."""
        findings = scan_pii(judgment.reason, *judgment.missing_information)
        if not findings:
            return VerificationCheck(
                "V-PII",
                "개인정보",
                True,
                BLOCKING,
                "직접 식별 정보가 확인되지 않았습니다.",
            )
        return VerificationCheck(
            "V-PII",
            "개인정보",
            False,
            BLOCKING,
            "출력에 직접 식별 정보로 보이는 값이 있습니다: "
            + ", ".join(sorted({item.rule_id for item in findings})),
        )

    # -- 7. 신뢰도 ------------------------------------------------------

    @staticmethod
    def _check_confidence(
        judgment: CriterionJudgment, rule_verification: Verification
    ) -> VerificationCheck:
        """낮은 확신도로 상태를 확정하려 하는지 확인한다."""
        if judgment.proposed_status == "UNKNOWN":
            return VerificationCheck(
                "V-CONFIDENCE",
                "신뢰도",
                True,
                ADVISORY,
                "보류 판단에는 확신도 문턱을 적용하지 않습니다.",
            )
        score = min(judgment.confidence, rule_verification.confidence)
        if score >= _LOW_CONFIDENCE:
            return VerificationCheck(
                "V-CONFIDENCE",
                "신뢰도",
                True,
                BLOCKING,
                f"확신도 {score:.3g} 로 자동 확정 문턱을 넘습니다.",
            )
        return VerificationCheck(
            "V-CONFIDENCE",
            "신뢰도",
            False,
            BLOCKING,
            f"확신도 {score:.3g} 가 문턱 {_LOW_CONFIDENCE} 미만입니다.",
        )


def _days_between(observed_at: str, index_date: str | None) -> int | None:
    """관찰 시점이 기준일로부터 며칠 전인지 계산한다.

    기준일이 없으면 기간 조건을 검사할 수 없으므로 0을 돌려 통과시킨다.
    실행 기준일은 오케스트레이터가 항상 넘겨준다.
    """
    if not index_date:
        return 0
    try:
        observed = date.fromisoformat(observed_at[:10])
        reference = date.fromisoformat(index_date[:10])
    except ValueError:
        return None
    return (reference - observed).days


#: 검증 항목 목록. `/architecture` 노출과 문서 대조에 쓴다.
CHECK_CATALOG: tuple[tuple[str, str], ...] = (
    ("V-EVIDENCE", "근거 존재"),
    ("V-WINDOW", "시간 범위"),
    ("V-UNIT", "단위"),
    ("V-OPERATOR", "연산자"),
    ("V-TYPE", "기준 유형"),
    ("V-PII", "개인정보"),
    ("V-CONFIDENCE", "신뢰도"),
)

__all__ = [
    "ADVISORY",
    "BLOCKING",
    "CHECK_CATALOG",
    "JudgmentVerification",
    "JudgmentVerifier",
    "RULE_STATUS_PROJECTION",
    "VerificationCheck",
]
