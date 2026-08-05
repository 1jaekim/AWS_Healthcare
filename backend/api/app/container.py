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
from .actions.explanation import ExplanationAgent
from .actions.next_best import NextBestEvidenceAgent
from .actions.packet import EvidencePacketBuilder
from agent.intake import IntakeAgent
from agent.manager import AgentManager, AgentStatus
from agent.model import BedrockModelClient, ModelClient, ModelError
from .config import (
    CriteriaStoreSettings,
    GraphRagSettings,
    ModelSettings,
    settings as default_settings,
)
from .criteria_repository import DynamoDBCriteriaRepository, LocalTrialCatalog
from .domain.intake_vocabulary import CatalogFieldResolver
from .intake import IntakeService, IntakeStore
from .intake.model import StubApplicationModelClient
from .orchestration.gateway import ToolGateway, default_policy
from .orchestration.recommendation import RecommendationOrchestrator
from .orchestration.router import CriterionRouter
from .orchestration.runtime import ScreeningOrchestrator
from .persistence.audit import AuditTrail
from .persistence.run_store import RunStore
from .reasoning.aggregator import DeterministicAggregator
from .reasoning.bundle import EvidenceBundleBuilder
from .reasoning.judgment import JudgmentVerifier
from .reasoning.rule_aggregator import RuleAggregator
from .reasoning.verifier import LocalEvidenceVerifier
from .repository import DatasetRepository
from .safety.guardrails import LocalGuardrail
from .safety.observability import TraceCollector
from .safety.pseudonyms import PatientKeyResolver
from .tools.criteria_tool import CriteriaTool
from .tools.evidence_retrieval import (
    BedrockKnowledgeBaseEvidenceRetrievalTool,
    EvidenceRetrievalTool,
)
from .tools.rule_evaluator import RuleEvaluator
from .tools.timeline_graph import TimelineGraphTool


@dataclass
class Container:
    """실행에 필요한 모든 구성 요소."""

    repository: DatasetRepository
    trial_catalog: object
    trace: TraceCollector
    guardrail: LocalGuardrail
    audit: AuditTrail
    run_store: RunStore
    gateway: ToolGateway
    criteria_tool: CriteriaTool
    timeline_tool: TimelineGraphTool
    orchestrator: ScreeningOrchestrator
    recommendation_orchestrator: RecommendationOrchestrator
    cohort_selector: CohortSelector
    intake: IntakeAgent
    application_intake: IntakeService
    intake_store: IntakeStore
    agent: AgentStatus
    retrieval_mode: str


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
    graphrag_config: GraphRagSettings | None = None,
    retrieval_client: object | None = None,
    secrets_client: object | None = None,
    criteria_store_config: CriteriaStoreSettings | None = None,
    criteria_table: object | None = None,
) -> Container:
    """컨테이너를 구성한다.

    model_client 를 직접 주면 그것을 쓴다. 테스트에서 스텁을 꽂기 위한 경로다.
    """
    config = model_config or default_settings.model
    rag_config = graphrag_config or default_settings.graphrag
    criteria_config = criteria_store_config or default_settings.criteria_store
    repository = DatasetRepository(data_dir)
    trace = TraceCollector()
    guardrail = LocalGuardrail()
    audit = AuditTrail()
    run_store = RunStore()
    intake_store = IntakeStore()

    shared_model = model_client
    if shared_model is None:
        shared_model, _ = _build_model_client(config)

    criteria_source = (
        DynamoDBCriteriaRepository(
            table_name=str(criteria_config.table_name),
            region=criteria_config.region,
            table=criteria_table,
        )
        if criteria_config.enabled
        else repository
    )
    criteria_tool = CriteriaTool(repository, criteria_source=criteria_source)
    trial_catalog = (
        criteria_source if criteria_config.enabled else LocalTrialCatalog(repository)
    )
    timeline_tool = TimelineGraphTool(repository)
    patient_key_resolver = None
    if rag_config.enabled:
        patient_key_resolver = PatientKeyResolver(
            secret=rag_config.patient_pseudonym_secret,
            secret_arn=rag_config.patient_pseudonym_secret_arn,
            region=rag_config.region,
            secrets_client=secrets_client,
        )
        # KB 만 리전이 다를 수 있다. 위의 Secrets Manager 는 이 서비스와 같은
        # 리전(rag_config.region)을 보고, KB 는 자기 리전을 본다. 한 값으로
        # 묶으면 둘 중 하나가 반드시 틀린 리전을 보게 된다.
        retrieval_tool = BedrockKnowledgeBaseEvidenceRetrievalTool(
            knowledge_base_id=str(rag_config.knowledge_base_id),
            region=rag_config.knowledge_base_region,
            client=retrieval_client,
        )
    else:
        retrieval_tool = EvidenceRetrievalTool(repository)
    rule_evaluator = RuleEvaluator()

    gateway = ToolGateway(trace)
    policy = default_policy()
    for tool in (criteria_tool, retrieval_tool, timeline_tool, rule_evaluator):
        gateway.register(tool, allow=policy[tool.name])

    # 에이전트 계층은 폴백 구현을 스스로 만들지 않는다. 어떤 구현을 쓸지는
    # 이 파일에서 결정하고, 에이전트는 contracts 의 포트만 보고 조립한다.
    agents = AgentManager(
        config=config,
        trace=trace,
        guardrail=guardrail,
        gateway=gateway,
        local_verifier=LocalEvidenceVerifier(
            LocalGuardrail(attach_disclaimer=False)
        ),
        local_explainer=ExplanationAgent(guardrail),
        local_next_best=NextBestEvidenceAgent(
            LocalGuardrail(attach_disclaimer=False)
        ),
        narration_guardrail=LocalGuardrail(attach_disclaimer=False),
        field_resolver=CatalogFieldResolver(),
        model_client=shared_model,
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
        deliberator=agents.deliberator,
        judge=agents.judge,
        judgment_verifier=JudgmentVerifier(),
        rule_aggregator=RuleAggregator(),
        patient_key_resolver=patient_key_resolver,
        mode=agents.status.mode,
    )
    recommendation_orchestrator = RecommendationOrchestrator(
        screening=orchestrator,
        repository=repository,
        trial_catalog=trial_catalog,
        run_store=run_store,
        audit=audit,
        a2a_max_criteria=config.max_deliberation_criteria,
        max_workers=config.recommendation_max_workers,
    )
    # 지원서 모듈은 로컬에서도 계약을 검증할 수 있도록 결정론적 스텁을 사용한다.
    # 운영에서 Bedrock이 활성화되면 스크리닝과 같은 모델 클라이언트를 공유한다.
    application_intake = IntakeService(
        store=intake_store,
        model=shared_model or StubApplicationModelClient(),
    )

    return Container(
        repository=repository,
        trial_catalog=trial_catalog,
        trace=trace,
        guardrail=guardrail,
        audit=audit,
        run_store=run_store,
        gateway=gateway,
        criteria_tool=criteria_tool,
        timeline_tool=timeline_tool,
        orchestrator=orchestrator,
        recommendation_orchestrator=recommendation_orchestrator,
        cohort_selector=CohortSelector(),
        intake=agents.intake,
        application_intake=application_intake,
        intake_store=intake_store,
        agent=agents.status,
        retrieval_mode=retrieval_tool.retrieval_mode,
    )
