"""에이전트 계층 조립과 상태 관리.

Screening Orchestrator 는 컨테이너에서 전체 실행 흐름에 붙지만, 모델 기반
에이전트와 로컬 fallback 조립은 이 파일에서 한 번에 관리한다. 데이터 조회와
규칙 계산은 Agent가 아니라 Gateway에 등록된 Tool로 둔다.
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
from .deliberation import UnknownDeliberationAgent
from .intake import IntakeAgent
from .judge import CriterionJudgeAgent
from .loop import EvidenceGatheringAgent
from .model import ModelClient, StubModelClient
from .narration import ModelExplanationAgent, ModelNextBestEvidenceAgent
from .prompts import prompt_versions
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
    prompts: list[dict[str, Any]] = field(default_factory=prompt_versions)
    """사용 중인 프롬프트의 id·버전·체크섬. 어떤 프롬프트로 낸 판정인지 되짚기 위함."""


@dataclass
class ManagedAgents:
    """오케스트레이터에 꽂을 에이전트 구현 묶음."""

    verifier: Any
    explainer: Any
    next_best: Any
    gatherer: EvidenceGatheringAgent | None
    deliberator: UnknownDeliberationAgent | None
    intake: IntakeAgent
    status: AgentStatus
    judge: CriterionJudgeAgent | None = None
    """기준별 LLM 판단자.

    붙어 있으면 오케스트레이터가 `LLM 판단 → Verifier → Rule Aggregator` 경로를
    사용하고, `verifier` 는 규칙 판정만 담당한다.
    """


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
                deliberator=None,
                # Intake 는 규칙 추출기만으로 완결되므로 모델이 없어도 켜 둔다.
                intake=self._build_intake(model=None),
                status=status,
                # 판단자도 모델 없이 켜 둔다. 규칙 판정을 그대로 승계하므로 상태는
                # 바뀌지 않고, Verifier 와 Rule Aggregator 가 같은 경로를 돈다.
                judge=self._build_judge(model=None),
            )

        judge = self._build_judge(model=client)
        # 판단자가 붙으면 기준별 모델 호출은 한 번이면 된다. NLI 검증기를 겹쳐
        # 부르지 않고 규칙 판정만 맡긴다.
        verifier = (
            local_verifier
            if judge is not None
            else ModelEvidenceVerifier(
                model=client, fallback=local_verifier, trace=self._trace
            )
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
        deliberator = UnknownDeliberationAgent(
            model=client,
            trace=self._trace,
            max_criteria=self._config.max_deliberation_criteria,
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
            deliberator=deliberator,
            intake=self._build_intake(model=client),
            status=status,
            judge=judge,
        )

    def _build_judge(
        self, *, model: ModelClient | None
    ) -> CriterionJudgeAgent | None:
        """기준별 판단자를 조립한다.

        설정으로 끄면 None 을 돌려주고, 호출자는 기존 NLI 검증기 경로를 쓴다.
        """
        if not getattr(self._config, "criterion_judge_enabled", True):
            return None
        return CriterionJudgeAgent(model=model, trace=self._trace)

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
            managed_agents=self._managed_agent_info(
                enabled=enabled,
                mode=mode,
                judge_enabled=bool(
                    getattr(self._config, "criterion_judge_enabled", True)
                ),
            ),
        )

    @staticmethod
    def _managed_agent_info(
        *, enabled: bool, mode: str, judge_enabled: bool = True
    ) -> list[ManagedAgentInfo]:
        model_backed = enabled
        gatherer_mode = mode if enabled else "disabled"
        # 판단자가 켜지면 기준별 모델 호출은 판단자가 맡고, 검증기는 규칙만 돈다.
        verifier_model_backed = model_backed and not judge_enabled
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
                name="criterion_judge",
                role="기준별 OK·NOT_OK·UNKNOWN 제안, 확정은 Rule Aggregator",
                enabled=judge_enabled,
                # 모델이 없으면 규칙 판정을 승계하므로 결정론적으로 동작한다.
                mode=(
                    (mode if enabled else "deterministic")
                    if judge_enabled
                    else "disabled"
                ),
                model_backed=model_backed and judge_enabled,
                source="agent/judge.py",
            ),
            ManagedAgentInfo(
                name="evidence_verifier",
                role="근거와 기준의 지지·충돌·누락 검증",
                enabled=True,
                mode=mode if verifier_model_backed else "deterministic",
                model_backed=verifier_model_backed,
                source="agent/verifier.py",
            ),
            ManagedAgentInfo(
                name="unknown_deliberation",
                role="미해소 기준을 2라운드로 교차 검토해 OK·NOT_OK·UNKNOWN 추천",
                enabled=enabled,
                mode=gatherer_mode,
                model_backed=model_backed,
                source="agent/deliberation.py",
            ),
            ManagedAgentInfo(
                name="question_agent",
                role="정보 가치순으로 부족 정보 확인 질문 생성, 최대 5개",
                enabled=True,
                mode=mode,
                model_backed=model_backed,
                source="agent/narration.py",
            ),
            ManagedAgentInfo(
                name="result_explanation_agent",
                role="추천 결과와 사전 부적합 사유를 관리자·참여자별로 설명",
                enabled=True,
                mode=mode,
                model_backed=model_backed,
                source="agent/narration.py",
            ),
        ]
