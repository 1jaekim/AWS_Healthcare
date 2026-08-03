"""9계층 구성 요소 조립.

AWS 어댑터로 교체할 때 이 파일 한 곳만 바꾸면 되도록 의존성을 모아둔다.
Tool 은 반드시 Gateway 에 등록해 최소 권한 정책과 함께 사용한다.

에이전트 모드는 설정으로 켠다. 기본은 결정론적 모드이며, Bedrock 을 켰다가
클라이언트 생성이 실패하면 스텁으로 내려앉아 서비스가 죽지 않게 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .actions.cohort import CohortSelector
from .actions.packet import EvidencePacketBuilder
from agent.manager import AgentManager, AgentStatus
from agent.model import BedrockModelClient, ModelClient, ModelError
from .config import ModelSettings, settings as default_settings
from .orchestration.gateway import ToolGateway, default_policy
from .orchestration.router import CriterionRouter
from .orchestration.runtime import ScreeningOrchestrator
from .persistence.audit import AuditTrail
from .persistence.run_store import RunStore
from .reasoning.aggregator import DeterministicAggregator
from .reasoning.bundle import EvidenceBundleBuilder
from .repository import DatasetRepository
from .safety.guardrails import LocalGuardrail
from .safety.observability import TraceCollector
from .tools.criteria_tool import CriteriaTool
from .tools.evidence_retrieval import EvidenceRetrievalTool
from .tools.rule_evaluator import RuleEvaluator
from .tools.timeline_graph import TimelineGraphTool


@dataclass
class Container:
    """실행에 필요한 모든 구성 요소."""

    repository: DatasetRepository
    trace: TraceCollector
    guardrail: LocalGuardrail
    audit: AuditTrail
    run_store: RunStore
    gateway: ToolGateway
    criteria_tool: CriteriaTool
    timeline_tool: TimelineGraphTool
    orchestrator: ScreeningOrchestrator
    cohort_selector: CohortSelector
    agent: AgentStatus


def _build_model_client(
    config: ModelSettings,
) -> tuple[ModelClient | None, str | None]:
    """모델 클라이언트를 만든다. 실패하면 이유와 함께 None 을 돌려준다."""
    if not config.enabled:
        return None, None
    try:
        client = BedrockModelClient(
            model_id=config.model_id,
            region=config.region,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            guardrail_id=config.guardrail_id,
            guardrail_version=config.guardrail_version,
        )
    except ModelError as exc:
        return None, str(exc)
    except Exception as exc:  # noqa: BLE001 - 어떤 실패든 서비스는 살린다
        return None, f"{type(exc).__name__}: {exc}"
    return client, None


def build_container(
    data_dir: Path,
    *,
    model_config: ModelSettings | None = None,
    model_client: ModelClient | None = None,
) -> Container:
    """컨테이너를 구성한다.

    model_client 를 직접 주면 그것을 쓴다. 테스트에서 스텁을 꽂기 위한 경로다.
    """
    config = model_config or default_settings.model
    repository = DatasetRepository(data_dir)
    trace = TraceCollector()
    guardrail = LocalGuardrail()
    audit = AuditTrail()
    run_store = RunStore()

    criteria_tool = CriteriaTool(repository)
    timeline_tool = TimelineGraphTool(repository)
    retrieval_tool = EvidenceRetrievalTool(repository)
    rule_evaluator = RuleEvaluator()

    gateway = ToolGateway(trace)
    policy = default_policy()
    for tool in (criteria_tool, retrieval_tool, timeline_tool, rule_evaluator):
        gateway.register(tool, allow=policy[tool.name])

    agents = AgentManager(
        config=config,
        trace=trace,
        guardrail=guardrail,
        gateway=gateway,
        model_client=model_client,
        model_factory=_build_model_client,
    ).build()

    orchestrator = ScreeningOrchestrator(
        gateway=gateway,
        criteria_tool=criteria_tool,
        timeline_tool=timeline_tool,
        router=CriterionRouter(),
        bundler=EvidenceBundleBuilder(),
        verifier=agents.verifier,
        aggregator=DeterministicAggregator(),
        packet_builder=EvidencePacketBuilder(),
        next_best=agents.next_best,
        explainer=agents.explainer,
        run_store=run_store,
        audit=audit,
        trace=trace,
        gatherer=agents.gatherer,
        mode=agents.status.mode,
    )

    return Container(
        repository=repository,
        trace=trace,
        guardrail=guardrail,
        audit=audit,
        run_store=run_store,
        gateway=gateway,
        criteria_tool=criteria_tool,
        timeline_tool=timeline_tool,
        orchestrator=orchestrator,
        cohort_selector=CohortSelector(),
        agent=agents.status,
    )
