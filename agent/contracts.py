"""에이전트 계층의 외부 계약(포트)과 공유 용어.

이 모듈은 `agent` 패키지 밖의 어떤 것도 import 하지 않는다. 에이전트가 필요한
동작은 Protocol 로 선언하고, 호출자(backend)가 그 계약을 만족하는 구현을 주입한다.
의존성 방향은 `backend → agent` 한 방향으로만 흐른다.

두 가지만 구체 타입으로 둔다.

- `CriterionStatus`: 판정 상태 어휘. 에이전트가 값을 만들어 넘기고 호출자가 그 값으로
  분기하므로, 같은 enum 객체를 공유해야 한다. 문자열로 두면 `is` 비교가 깨진다.
- `ToolPermissionDenied`: 에이전트가 잡아야 하는 예외. 호출자가 이 예외를 상속해
  구체 예외로 특화한다.

나머지는 모두 구조적 타입(Protocol)이라 런타임 의존성이 없다.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from enum import StrEnum
from typing import Any, Protocol


class CriterionStatus(StrEnum):
    """기준 한 건에 대한 판정 상태.

    에이전트와 호출자가 공유하는 어휘다. 에이전트는 상태를 '제안'만 하고
    확정은 호출자 쪽 Aggregator 가 한다.
    """

    EVIDENCE_FOUND = "EVIDENCE_FOUND"
    """근거가 확인되어 조건을 충족한다."""

    CONTRADICTED = "CONTRADICTED"
    """근거가 확인되었으나 조건과 충돌한다."""

    UNKNOWN = "UNKNOWN"
    """판정에 필요한 관찰값이 없다."""

    CONFLICTING = "CONFLICTING"
    """기록 간 값이 서로 어긋난다."""

    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    """자동 판정 신뢰도가 낮아 사람 검토가 필요하다."""


class ToolPermissionDenied(RuntimeError):
    """도구 호출이 권한 정책에 막혔을 때 발생한다.

    에이전트 루프는 이 예외를 잡아 모델에게 '허용되지 않음'을 되돌려준다.
    호출자는 이 예외를 상속해 자신의 권한 모델에 맞게 특화한다.
    """


# ---------------------------------------------------------------------------
# 읽기 전용 데이터 형태
# ---------------------------------------------------------------------------


class Observation(Protocol):
    """관찰값 한 건. 출처와 측정 시점을 동반한다."""

    field_name: str
    value: float | str | bool | None
    unit: str | None
    observed_at: str | None
    source_id: str
    detail: str | None


class NarrativeSnippet(Protocol):
    """자유서술 EMR 에서 추출한 근거 문장."""

    note_id: str
    note_date: str
    snippet: str
    score: float


class RuleOutcome(Protocol):
    """결정론적 규칙 계산 결과."""

    satisfied: bool | None
    explanation: str


class CriterionRule(Protocol):
    """기준 한 건의 실행 규칙."""

    criterion_id: str
    criterion_type: str
    field_name: str
    label: str
    unit: str | None

    def expected_repr(self) -> str:
        """기준 조건을 사람이 읽을 수 있는 문자열로 만든다."""
        ...


class EvidenceBundle(Protocol):
    """조건 · 관찰값 · 원문 · 출처를 묶은 검증 입력 단위."""

    rule: CriterionRule
    narrative: Sequence[NarrativeSnippet]
    outcome: RuleOutcome | None

    @property
    def primary_observation(self) -> Observation | None:
        """규칙 판정에 사용한 대표 관찰값."""
        ...


class Verification(Protocol):
    """검증 결과. 에이전트는 이 형태를 받아 필드만 바꿔 되돌려준다."""

    criterion_id: str
    proposed_status: CriterionStatus
    confidence: float
    grounded: bool
    conflicts: tuple[str, ...]
    notes: tuple[str, ...]


class CriterionResult(Protocol):
    """확정된 기준별 결과."""

    criterion_id: str
    criterion_type: str
    label: str
    field_name: str
    status: CriterionStatus
    observed_value: str | None
    expected_condition: str
    unit: str | None
    observed_at: str | None
    confidence: float
    source_ids: tuple[str, ...]


class AggregateOutcome(Protocol):
    """실행 단위 종합 판정."""

    eligibility_status: Any
    decision_label: str
    criteria_total: int
    criteria_met: int


class CriterionPlan(Protocol):
    """기준 한 건의 실행 계획."""

    rule: CriterionRule
    needs_graph: bool
    needs_narrative: bool
    narrative_terms: tuple[str, ...]

    @property
    def criterion_id(self) -> str: ...


class ExecutionPlan(Protocol):
    """실행 전체 계획."""

    plans: Sequence[CriterionPlan]


class ToolContext(Protocol):
    """도구 호출 단위 컨텍스트."""

    run_id: str
    person_id: int
    trial_id: str
    index_encounter_id: str
    index_date: str


class Explanation(Protocol):
    """대상별 설명 묶음."""

    audience: str
    summary: str
    highlights: list[str]
    blocked: bool
    guardrail_findings: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]: ...


class EvidenceRequest(Protocol):
    """확인 항목 한 건."""

    request_id: str
    criterion_id: str
    field_name: str
    label: str
    reason: str
    question: str
    information_value: float
    effort: float
    priority: int
    target: str


class AgentSettings(Protocol):
    """에이전트 계층이 읽는 설정."""

    enabled: bool
    region: str
    model_id: str
    max_tokens: int
    temperature: float
    max_agent_iterations: int
    guardrail_id: str | None
    guardrail_version: str


# ---------------------------------------------------------------------------
# 서비스 포트
# ---------------------------------------------------------------------------


class Tracer(Protocol):
    """호출 구간 추적기. with 블록에 넣은 dict 는 스팬 attribute 로 병합된다."""

    def span(
        self,
        run_id: str,
        name: str,
        kind: str,
        **attributes: Any,
    ) -> AbstractContextManager[dict[str, Any]]: ...


class GuardrailFinding(Protocol):
    """차단 또는 완화된 표현 한 건."""

    rule_id: str
    severity: str


class GuardrailVerdict(Protocol):
    """필터 통과 결과. text 는 완화가 적용된 최종 문구."""

    text: str
    blocked: bool
    findings: Sequence[GuardrailFinding]


class Guardrail(Protocol):
    """생성 문장 검사기."""

    def review(self, text: str, *, audience: str = ...) -> GuardrailVerdict: ...

    def check_grounding(
        self, statement: str, *, source_ids: list[str]
    ) -> GuardrailVerdict:
        """근거 출처가 없는 문구를 차단한다."""
        ...


class ToolGateway(Protocol):
    """권한 검사를 거쳐 도구를 호출하는 관문."""

    def invoke(
        self, tool_name: str, context: ToolContext, /, **kwargs: Any
    ) -> Any: ...


class EvidenceVerifier(Protocol):
    """근거 검증기. 모델 검증기의 폴백으로 주입된다."""

    def verify(
        self, bundle: EvidenceBundle, *, run_id: str | None = None
    ) -> Verification: ...


class ExplanationWriter(Protocol):
    """설명 생성기. 모델 설명 생성기의 폴백으로 주입된다."""

    def explain(
        self,
        *,
        outcome: AggregateOutcome,
        results: list[CriterionResult],
        audience: str,
        run_id: str | None = None,
    ) -> Explanation: ...


class NextBestEvidenceWriter(Protocol):
    """확인 질문 생성기. 정보 가치 계산을 담당하며 폴백으로 주입된다."""

    def propose(
        self,
        run_id: str,
        results: list[CriterionResult],
        *,
        limit: int = 5,
    ) -> list[EvidenceRequest]: ...
