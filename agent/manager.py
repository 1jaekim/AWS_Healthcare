"""에이전트 계층 조립과 상태 관리.

Screening Orchestrator 는 컨테이너에서 전체 실행 흐름에 붙지만, 모델 기반
에이전트와 로컬 fallback 조립은 이 파일에서 한 번에 관리한다. Intake 처럼
아직 구현 전인 에이전트도 상태 목록에 명시해 아키텍처 응답에서 빠지지 않게 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .contracts import (
    AgentSettings,
    EvidenceVerifier,
    ExplanationWriter,
    FieldResolver,
    Guardrail,
    NextBestEvidenceWriter,
    ToolGateway,
    Tracer,
)
from .intake import IntakeAgent
from .loop import EvidenceGatheringAgent
from .model import ModelClient, StubModelClient
from .narration import ModelExplanationAgent, ModelNextBestEvidenceAgent
from .toolspec import tool_names
from .verifier import ModelEvidenceVerifier


ModelFactory = Callable[[AgentSettings], tuple[ModelClient | None, str | None]]


@dataclass
class ManagedAgentInfo:
    """아키텍처 응답에 노출할 에이전트 단위 상태."""

    name: str
    role: str
    enabled: bool
    mode: str
    model_backed: bool = False
    exposed_tools: list[str] = field(default_factory=list)
    source: str | None = None


@dataclass
class AgentStatus:
    """에이전트 계층 상태. /architecture 로 노출한다."""

    enabled: bool
    mode: str
    model_id: str | None
    region: str | None
    max_iterations: int
    guardrail_attached: bool
    fallback_reason: str | None = None
    managed_agents: list[ManagedAgentInfo] = field(default_factory=list)


@dataclass
class ManagedAgents:
    """오케스트레이터에 꽂을 에이전트 구현 묶음."""

    verifier: Any
    explainer: Any
    next_best: Any
    gatherer: EvidenceGatheringAgent | None
    intake: IntakeAgent
    status: AgentStatus


class AgentManager:
    """모델/로컬 fallback 기반 에이전트들을 한 곳에서 구성한다.

    로컬 fallback 구현은 호출자가 주입한다. 이 계층은 어떤 구체 구현이 오는지
    알지 않고, `contracts` 의 포트만 만족하면 그대로 조립한다.
    """

    def __init__(
        self,
        *,
        config: AgentSettings,
        trace: Tracer,
        guardrail: Guardrail,
        gateway: ToolGateway,
        local_verifier: EvidenceVerifier,
        local_explainer: ExplanationWriter,
        local_next_best: NextBestEvidenceWriter,
        narration_guardrail: Guardrail | None = None,
        field_resolver: FieldResolver | None = None,
        model_client: ModelClient | None = None,
        model_factory: ModelFactory | None = None,
    ) -> None:
        self._config = config
        self._trace = trace
        self._guardrail = guardrail
        self._gateway = gateway
        self._local_verifier = local_verifier
        self._local_explainer = local_explainer
        self._local_next_best = local_next_best
        # 질문 문장에는 면책 문구를 붙이지 않는다. 호출자가 그 설정의 Guardrail 을
        # 따로 주면 그것을 쓰고, 없으면 기본 Guardrail 을 쓴다.
        self._narration_guardrail = narration_guardrail or guardrail
        self._field_resolver = field_resolver
        self._model_client = model_client
        self._model_factory = model_factory

    def build(self) -> ManagedAgents:
        local_verifier = self._local_verifier
        local_explainer = self._local_explainer
        local_next_best = self._local_next_best

        client, fallback_reason = self._resolve_model_client()
        if client is None:
            mode = "deterministic"
            status = self._status(
                enabled=False,
                mode=mode,
                fallback_reason=fallback_reason,
                model_client=None,
            )
            return ManagedAgents(
                verifier=local_verifier,
                explainer=local_explainer,
                next_best=local_next_best,
                gatherer=None,
                # Intake 는 규칙 추출기만으로 완결되므로 모델이 없어도 켜 둔다.
                intake=self._build_intake(model=None),
                status=status,
            )

        verifier = ModelEvidenceVerifier(
            model=client, fallback=local_verifier, trace=self._trace
        )
        explainer = ModelExplanationAgent(
            model=client,
            guardrail=self._guardrail,
            fallback=local_explainer,
            trace=self._trace,
        )
        next_best = ModelNextBestEvidenceAgent(
            model=client,
            guardrail=self._narration_guardrail,
            fallback=local_next_best,
            trace=self._trace,
        )
        gatherer = EvidenceGatheringAgent(
            model=client,
            gateway=self._gateway,
            trace=self._trace,
            max_iterations=self._config.max_agent_iterations,
        )
        mode = f"agent:{getattr(client, 'mode', 'unknown')}"
        status = self._status(
            enabled=True,
            mode=mode,
            fallback_reason=fallback_reason,
            model_client=client,
        )
        return ManagedAgents(
            verifier=verifier,
            explainer=explainer,
            next_best=next_best,
            gatherer=gatherer,
            intake=self._build_intake(model=client),
            status=status,
        )

    def _build_intake(self, *, model: ModelClient | None) -> IntakeAgent:
        """Intake 를 조립한다.

        용어 라벨에는 면책 문구를 붙이지 않아야 하므로 narration 쪽 Guardrail 을
        함께 쓴다. Intake 는 문장을 생성하지 않고 검사 목적으로만 호출한다.
        """
        return IntakeAgent(
            guardrail=self._narration_guardrail,
            trace=self._trace,
            model=model,
            resolver=self._field_resolver,
        )

    def _resolve_model_client(self) -> tuple[ModelClient | None, str | None]:
        if self._model_client is not None:
            return self._model_client, None
        if not self._config.enabled:
            return None, None
        if self._model_factory is None:
            return StubModelClient(), "model factory not configured"

        client, fallback_reason = self._model_factory(self._config)
        if client is not None:
            return client, fallback_reason

        # Bedrock 을 켰는데 붙지 못했다. 스텁으로 내려앉는다.
        return StubModelClient(), fallback_reason

    def _status(
        self,
        *,
        enabled: bool,
        mode: str,
        fallback_reason: str | None,
        model_client: ModelClient | None,
    ) -> AgentStatus:
        return AgentStatus(
            enabled=enabled,
            mode=mode,
            model_id=self._config.model_id if model_client is not None else None,
            region=self._config.region if model_client is not None else None,
            max_iterations=self._config.max_agent_iterations,
            guardrail_attached=bool(self._config.guardrail_id),
            fallback_reason=fallback_reason,
            managed_agents=self._managed_agent_info(enabled=enabled, mode=mode),
        )

    @staticmethod
    def _managed_agent_info(*, enabled: bool, mode: str) -> list[ManagedAgentInfo]:
        model_backed = enabled
        gatherer_mode = mode if enabled else "disabled"
        return [
            ManagedAgentInfo(
                name="screening_orchestrator",
                role="환자 x 임상시험 기준 실행, Tool 호출 흐름 조율",
                enabled=True,
                mode=mode,
                model_backed=False,
                source="app/orchestration/runtime.py",
            ),
            ManagedAgentInfo(
                name="rag_evidence_retrieval",
                role="자유서술 EMR 문장 근거 검색",
                enabled=True,
                mode="local_keyword",
                model_backed=False,
                exposed_tools=["evidence_retrieval_tool"],
                source="app/tools/evidence_retrieval.py",
            ),
            ManagedAgentInfo(
                name="evidence_gathering_agent",
                role="미해소 기준에 필요한 RAG/타임라인 Tool 호출 계획",
                enabled=enabled,
                mode=gatherer_mode,
                model_backed=model_backed,
                exposed_tools=tool_names() if enabled else [],
                source="agent/loop.py",
            ),
            ManagedAgentInfo(
                name="intake_agent",
                role="자유 문장 입력을 측정값·약물·이상반응·상태 이벤트로 정규화",
                enabled=True,
                # 규칙 추출기가 항상 동작하므로 모델이 없어도 결정론적으로 켜진다.
                mode=mode if enabled else "deterministic",
                model_backed=model_backed,
                source="agent/intake.py",
            ),
            ManagedAgentInfo(
                name="evidence_verifier",
                role="근거와 기준의 지지·충돌·누락 검증",
                enabled=True,
                mode=mode,
                model_backed=model_backed,
                source="agent/verifier.py",
            ),
            ManagedAgentInfo(
                name="next_best_evidence",
                role="정보가 부족한 기준의 다음 확인 질문 생성",
                enabled=True,
                mode=mode,
                model_backed=model_backed,
                source="agent/narration.py",
            ),
            ManagedAgentInfo(
                name="explanation_agent",
                role="관리자·참여자별 판정 설명 생성",
                enabled=True,
                mode=mode,
                model_backed=model_backed,
                source="agent/narration.py",
            ),
        ]
