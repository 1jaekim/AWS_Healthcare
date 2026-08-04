"""9계층 오케스트레이터 통합 검증."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.tools.base import Action, DataStore, Permission, PermissionDenied


def test_screening_run_returns_states_and_evidence() -> None:
    with TestClient(app) as client:
        trials = client.get("/api/v1/trials").json()
        assert trials
        trial_id = trials[0]["trial_id"]

        patients = client.get("/api/v1/patients?limit=1").json()["items"]
        person_id = patients[0]["person_id"]

        response = client.post(
            "/api/v1/screening/run",
            json={"person_id": person_id, "trial_id": trial_id},
        )
        assert response.status_code == 200
        body = response.json()

        assert body["eligibility_status"] in {
            "ELIGIBLE",
            "INELIGIBLE",
            "NEEDS_MORE_EVIDENCE",
            "REVIEW_REQUIRED",
        }
        assert body["screening_decision"] in {"OK", "NOT_OK", "UNKNOWN"}
        expected_decision = {
            "ELIGIBLE": "OK",
            "INELIGIBLE": "NOT_OK",
            "NEEDS_MORE_EVIDENCE": "UNKNOWN",
            "REVIEW_REQUIRED": "UNKNOWN",
        }
        assert body["screening_decision"] == expected_decision[
            body["eligibility_status"]
        ]
        assert body["criteria_total"] > 0
        assert len(body["packet"]["items"]) == body["criteria_total"]

        valid_states = {
            "EVIDENCE_FOUND",
            "CONTRADICTED",
            "UNKNOWN",
            "CONFLICTING",
            "REVIEW_REQUIRED",
        }
        for item in body["packet"]["items"]:
            assert item["status"] in valid_states
            assert item["expected_condition"]
            assert item["confidence_band"] in {"높음", "중간", "낮음"}

        assert set(body["explanations"]) == {"admin", "patient"}
        assert body["trace"]["span_count"] > 0


def test_run_is_reproducible() -> None:
    """동일 입력은 동일 상태를 낸다. Deterministic Aggregator 보장 확인."""
    with TestClient(app) as client:
        trial_id = client.get("/api/v1/trials").json()[0]["trial_id"]
        person_id = client.get("/api/v1/patients?limit=1").json()["items"][0][
            "person_id"
        ]
        payload = {"person_id": person_id, "trial_id": trial_id}

        first = client.post("/api/v1/screening/run", json=payload).json()
        second = client.post("/api/v1/screening/run", json=payload).json()

        assert first["eligibility_status"] == second["eligibility_status"]
        assert first["criteria_version"] == second["criteria_version"]
        assert [item["status"] for item in first["packet"]["items"]] == [
            item["status"] for item in second["packet"]["items"]
        ]


def test_evidence_has_source_ids() -> None:
    """근거 항목은 출처 ID를 갖는다. Contextual Grounding 전제."""
    with TestClient(app) as client:
        trial_id = client.get("/api/v1/trials").json()[0]["trial_id"]
        person_id = client.get("/api/v1/patients?limit=1").json()["items"][0][
            "person_id"
        ]
        run = client.post(
            "/api/v1/screening/run",
            json={"person_id": person_id, "trial_id": trial_id},
        ).json()

        evidence = client.get(
            f"/api/v1/screening/{run['run_id']}/evidence"
        ).json()
        grounded = [item for item in evidence["items"] if item["source_ids"]]
        assert grounded, "출처 ID를 가진 근거가 하나도 없습니다."


def test_audit_trail_records_run() -> None:
    with TestClient(app) as client:
        trial_id = client.get("/api/v1/trials").json()[0]["trial_id"]
        person_id = client.get("/api/v1/patients?limit=1").json()["items"][0][
            "person_id"
        ]
        run = client.post(
            "/api/v1/screening/run",
            json={"person_id": person_id, "trial_id": trial_id},
        ).json()

        events = client.get(f"/api/v1/audit/{run['run_id']}").json()
        actions = {event["action"] for event in events}
        assert "RUN_STARTED" in actions
        assert "CRITERIA_LOADED" in actions
        assert "CRITERION_RESOLVED" in actions
        assert "RUN_COMPLETED" in actions

        sequences = [event["sequence_no"] for event in events]
        assert sequences == sorted(sequences), "감사 이벤트 순서가 어긋납니다."


def test_cohort_funnel_and_bottlenecks() -> None:
    with TestClient(app) as client:
        trial_id = client.get("/api/v1/trials").json()[0]["trial_id"]
        response = client.post(
            "/api/v1/cohort/run", json={"trial_id": trial_id, "limit": 5}
        )
        assert response.status_code == 200
        body = response.json()

        assert body["total_screened"] > 0
        assert sum(stage["count"] for stage in body["funnel"]) == body["total_screened"]
        assert body["bottlenecks"]
        for item in body["bottlenecks"]:
            assert 0.0 <= item["block_rate"] <= 1.0


def test_gateway_enforces_declared_permissions() -> None:
    """선언되지 않은 권한을 허용하려 하면 등록 단계에서 막힌다."""
    from app.orchestration.gateway import ToolGateway
    from app.safety.observability import TraceCollector
    from app.tools.rule_evaluator import RuleEvaluator

    gateway = ToolGateway(TraceCollector())
    try:
        gateway.register(
            RuleEvaluator(),
            allow=(Permission(DataStore.RUN_TABLE, Action.WRITE),),
        )
    except ValueError as exc:
        assert "선언하지 않은 권한" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("권한 초과 등록이 허용되었습니다.")


def test_tool_self_check_blocks_unlisted_store() -> None:
    """Tool 자체 점검이 미선언 저장소 접근을 막는다."""
    from app.tools.criteria_tool import CriteriaTool

    tool = CriteriaTool.__new__(CriteriaTool)
    try:
        tool.assert_allowed(Permission(DataStore.TIMELINE_GRAPH, Action.READ))
    except PermissionDenied as exc:
        assert exc.tool_name == "criteria_tool"
    else:  # pragma: no cover
        raise AssertionError("미선언 저장소 접근이 허용되었습니다.")


def test_architecture_endpoint_lists_tool_permissions() -> None:
    with TestClient(app) as client:
        body = client.get("/api/v1/architecture").json()
        names = {entry["tool_name"] for entry in body["tools"]}
        assert names == {
            "criteria_tool",
            "evidence_retrieval_tool",
            "timeline_graph_tool",
            "rule_evaluator",
        }
        by_name = {entry["tool_name"]: entry["permissions"] for entry in body["tools"]}
        assert by_name["rule_evaluator"] == []
        assert by_name["criteria_tool"] == ["READ:dynamodb:trial_definitions"]


def test_review_queue_and_answer_flow() -> None:
    with TestClient(app) as client:
        trial_id = client.get("/api/v1/trials").json()[0]["trial_id"]
        person_id = client.get("/api/v1/patients?limit=1").json()["items"][0][
            "person_id"
        ]
        run = client.post(
            "/api/v1/screening/run",
            json={"person_id": person_id, "trial_id": trial_id},
        ).json()

        answer = client.post(
            f"/api/v1/patients/{person_id}/answers",
            json={
                "run_id": run["run_id"],
                "criterion_id": run["packet"]["items"][0]["criterion_id"],
                "value": "확인했습니다",
                "submitted_by": "participant",
            },
        )
        assert answer.status_code == 200
        assert answer.json()["person_id"] == person_id

        queue = client.get("/api/v1/review-queue").json()
        if queue:
            ticket_id = queue[0]["ticket_id"]
            decided = client.patch(
                f"/api/v1/review-queue/{ticket_id}",
                json={"decision": "APPROVED", "decided_by": "researcher-01"},
            )
            assert decided.status_code == 200
            assert decided.json()["status"] == "APPROVED"


def test_guardrail_blocks_certainty_expression() -> None:
    from app.safety.guardrails import LocalGuardrail

    guard = LocalGuardrail(attach_disclaimer=False)
    verdict = guard.review("당뇨로 확진되었습니다.", audience="patient")
    assert "확진" not in verdict.text
    assert verdict.modified

    grounding = guard.check_grounding("근거 없는 설명", source_ids=[])
    assert grounding.blocked


def _boolean_bundle(structured_value: bool, snippet: str, score: float):
    """파생 불리언 기준의 번들을 직접 만든다."""
    from app.domain.models import (
        CriterionRule,
        EvidenceBundle,
        NarrativeSnippet,
        Observation,
        RuleOutcome,
    )
    from app.domain.states import CriterionKind

    rule = CriterionRule(
        criterion_id="X-C01",
        criterion_type="EXCLUSION",
        field_name="uncontrolled_bp",
        operator="=",
        value_low="false",
        value_high=None,
        unit="boolean",
        label="조절되지 않는 고혈압",
        kind=CriterionKind.DERIVED_BOOLEAN,
        trial_id="T",
        criteria_version="v1-test",
    )
    bundle = EvidenceBundle(rule=rule)
    bundle.observations.append(
        Observation(
            field_name="uncontrolled_bp",
            value=structured_value,
            unit="boolean",
            observed_at="2020-01-01",
            source="TIMELINE_GRAPH",
            source_id="ENC-1",
        )
    )
    bundle.narrative.append(
        NarrativeSnippet(
            note_id="NOTE-ENC-1",
            encounter_id="ENC-1",
            note_date="2020-01-01",
            snippet=snippet,
            matched_terms=("조절되지 않는 고혈압",),
            score=score,
        )
    )
    bundle.outcome = RuleOutcome(
        criterion_id="X-C01",
        satisfied=structured_value is False,
        observed_repr="false" if not structured_value else "true",
        expected_repr="=false",
        explanation="구조화 판정",
    )
    return bundle


def test_conflicting_detected_when_narrative_asserts_condition() -> None:
    """구조화는 '비해당', 자유서술은 '해당' 이면 CONFLICTING 이 된다."""
    from app.reasoning.verifier import LocalEvidenceVerifier

    bundle = _boolean_bundle(
        structured_value=False,
        snippet="조절되지 않는 고혈압 소견이 관찰된다.",
        score=1.0,
    )
    verification = LocalEvidenceVerifier().verify(bundle)
    assert verification.proposed_status == "CONFLICTING"
    assert verification.conflicts


def test_generic_mention_does_not_trigger_conflict() -> None:
    """단어만 스친 낮은 일치도 문장은 충돌로 보지 않는다."""
    from app.reasoning.verifier import LocalEvidenceVerifier

    bundle = _boolean_bundle(
        structured_value=False,
        snippet="혈압 120/67 mmHg 로 측정되었다.",
        score=0.25,
    )
    verification = LocalEvidenceVerifier().verify(bundle)
    assert verification.proposed_status != "CONFLICTING"


def test_negated_narrative_agrees_with_structured_false() -> None:
    """자유서술이 부정 진술이면 구조화 '비해당' 과 일치한다."""
    from app.reasoning.verifier import LocalEvidenceVerifier

    bundle = _boolean_bundle(
        structured_value=False,
        snippet="조절되지 않는 고혈압은 없었다.",
        score=1.0,
    )
    verification = LocalEvidenceVerifier().verify(bundle)
    assert verification.proposed_status == "EVIDENCE_FOUND"


def test_unknown_when_observation_missing() -> None:
    """관찰값이 없으면 UNKNOWN 이고 확인 질문이 생성된다."""
    from app.actions.next_best import NextBestEvidenceAgent
    from app.domain.models import CriterionResult
    from app.domain.states import CriterionKind, CriterionStatus

    result = CriterionResult(
        criterion_id="X-C02",
        criterion_type="INCLUSION",
        label="HbA1c",
        field_name="hba1c",
        kind=CriterionKind.NUMERIC_POINT,
        status=CriterionStatus.UNKNOWN,
        observed_value=None,
        expected_condition="7.5-10.5",
        unit="%",
        observed_at=None,
        confidence=0.0,
        explanation="관찰값 없음",
        source_ids=(),
    )
    requests = NextBestEvidenceAgent().propose("RUN-test", [result])
    assert len(requests) == 1
    assert requests[0].criterion_id == "X-C02"
    assert requests[0].target == "participant"
    assert requests[0].priority == 1


def test_exclusion_triggered_marks_ineligible() -> None:
    """제외 기준이 충돌하면 종합 판정은 부적합이다."""
    from app.domain.models import CriterionResult
    from app.domain.states import CriterionKind, CriterionStatus
    from app.reasoning.aggregator import DeterministicAggregator

    def make(cid: str, ctype: str, st: CriterionStatus) -> CriterionResult:
        return CriterionResult(
            criterion_id=cid,
            criterion_type=ctype,
            label=cid,
            field_name="f",
            kind=CriterionKind.NUMERIC_POINT,
            status=st,
            observed_value="1",
            expected_condition="x",
            unit=None,
            observed_at="2020-01-01",
            confidence=0.9,
            explanation="",
            source_ids=("ENC-1",),
        )

    agg = DeterministicAggregator()

    all_ok = [
        make("A", "INCLUSION", CriterionStatus.EVIDENCE_FOUND),
        make("B", "EXCLUSION", CriterionStatus.EVIDENCE_FOUND),
    ]
    assert agg.aggregate(all_ok).eligibility_status == "ELIGIBLE"

    with_exclusion = [
        make("A", "INCLUSION", CriterionStatus.EVIDENCE_FOUND),
        make("B", "EXCLUSION", CriterionStatus.CONTRADICTED),
    ]
    assert agg.aggregate(with_exclusion).eligibility_status == "INELIGIBLE"

    with_unknown = [
        make("A", "INCLUSION", CriterionStatus.EVIDENCE_FOUND),
        make("B", "INCLUSION", CriterionStatus.UNKNOWN),
    ]
    assert agg.aggregate(with_unknown).eligibility_status == "NEEDS_MORE_EVIDENCE"

    with_review = [
        make("A", "INCLUSION", CriterionStatus.EVIDENCE_FOUND),
        make("B", "INCLUSION", CriterionStatus.REVIEW_REQUIRED),
    ]
    assert agg.aggregate(with_review).eligibility_status == "REVIEW_REQUIRED"
