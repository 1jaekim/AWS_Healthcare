"""제한형 A2A 결과를 반영한 임상시험 추천 계약."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.actions.recommendation import TrialRanker
from app.domain.models import CriterionResult
from app.domain.states import CriterionKind, CriterionStatus
from app.main import app
from app.orchestration.recommendation import RecommendationOrchestrator


def _criterion(
    criterion_id: str,
    status: CriterionStatus,
) -> CriterionResult:
    return CriterionResult(
        criterion_id=criterion_id,
        criterion_type="INCLUSION",
        label=criterion_id,
        field_name="hba1c",
        kind=CriterionKind.NUMERIC_POINT,
        status=status,
        observed_value="7.8",
        expected_condition=">=7.0",
        unit="%",
        observed_at="2026-01-01",
        confidence=0.5,
        explanation="검사 결과와 기준을 비교했습니다.",
        source_ids=("NOTE-1",),
    )


def _output(
    trial_id: str,
    *,
    status: CriterionStatus,
    decision: str,
    deliberation: dict | None = None,
):
    result = _criterion(f"{trial_id}-C01", status)
    run = SimpleNamespace(
        run_id=f"RUN-{trial_id}",
        trial_id=trial_id,
        results=[result],
        metadata={"deliberation": deliberation or {}},
    )
    outcome = SimpleNamespace(
        screening_decision=decision,
        criteria_met=int(status is CriterionStatus.EVIDENCE_FOUND),
        criteria_total=1,
    )
    return SimpleNamespace(
        run=run,
        outcome=outcome,
        requests=[],
        review_ticket_id=None,
    )


def test_ranker_uses_only_grounded_a2a_consensus() -> None:
    criterion_id = "TRIAL-A-C01"
    grounded = {
        "enabled": True,
        "rounds": 2,
        "max_rounds": 2,
        "items": [
            {
                "criterion_id": criterion_id,
                "recommendation": "OK",
                "agreement": True,
                "grounded": True,
                "source_ids": ["NOTE-1"],
            }
        ],
    }
    outputs = [
        _output(
            "TRIAL-A",
            status=CriterionStatus.UNKNOWN,
            decision="UNKNOWN",
            deliberation=grounded,
        ),
        _output(
            "TRIAL-B",
            status=CriterionStatus.UNKNOWN,
            decision="UNKNOWN",
        ),
        _output(
            "TRIAL-C",
            status=CriterionStatus.CONTRADICTED,
            decision="NOT_OK",
        ),
    ]
    catalog = {
        trial_id: {"trial_name": trial_id, "description": ""}
        for trial_id in ("TRIAL-A", "TRIAL-B", "TRIAL-C")
    }

    recommended, excluded = TrialRanker().rank(
        outputs, trial_catalog=catalog, top_k=3
    )

    assert [item["trial_id"] for item in recommended] == ["TRIAL-A", "TRIAL-B"]
    assert recommended[0]["rank_score"] == 0.8
    assert recommended[0]["screening_decision"] == "UNKNOWN"
    assert recommended[0]["recommendation_decision"] == "OK"
    assert recommended[0]["criteria"][0]["a2a_applied"] is True
    assert recommended[1]["rank_score"] == 0.4
    assert [item["trial_id"] for item in excluded] == ["TRIAL-C"]


def test_grounded_a2a_not_ok_excludes_only_from_recommendation() -> None:
    deliberation = {
        "enabled": True,
        "rounds": 2,
        "max_rounds": 2,
        "items": [
            {
                "criterion_id": "TRIAL-A-C01",
                "recommendation": "NOT_OK",
                "agreement": True,
                "grounded": True,
                "source_ids": ["NOTE-1"],
            }
        ],
    }
    output = _output(
        "TRIAL-A",
        status=CriterionStatus.UNKNOWN,
        decision="UNKNOWN",
        deliberation=deliberation,
    )

    recommended, excluded = TrialRanker().rank(
        [output],
        trial_catalog={
            "TRIAL-A": {"trial_name": "TRIAL-A", "description": ""}
        },
        top_k=1,
    )

    assert recommended == []
    assert excluded[0]["screening_decision"] == "UNKNOWN"
    assert excluded[0]["recommendation_decision"] == "NOT_OK"


def test_recommendation_api_runs_candidates_and_persists_result() -> None:
    with TestClient(app) as client:
        trials = client.get("/api/v1/trials").json()[:3]
        person_id = client.get("/api/v1/patients?limit=1").json()["items"][0][
            "person_id"
        ]
        response = client.post(
            "/api/v1/recommendations/run",
            json={
                "person_id": person_id,
                "trial_ids": [item["trial_id"] for item in trials],
                "top_k": 2,
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["recommendation_id"].startswith("REC-")
        assert body["evaluated_trials"] == len(trials)
        assert len(body["recommended_trials"]) <= 2
        assert body["limits"] == {
            "top_k": 2,
            "a2a_max_rounds": 2,
            "a2a_max_criteria_per_trial": 5,
            "recommendation_max_workers": len(trials),
        }
        assert [item["rank"] for item in body["recommended_trials"]] == list(
            range(1, len(body["recommended_trials"]) + 1)
        )
        assert all(
            item["recommendation_decision"] != "NOT_OK"
            for item in body["recommended_trials"]
        )
        assert all(
            item["recommendation_decision"] == "NOT_OK"
            for item in body["excluded_trials"]
        )

        stored = client.get(
            f"/api/v1/recommendations/{body['recommendation_id']}"
        )
        assert stored.status_code == 200
        assert stored.json() == body

        audit = client.get(
            f"/api/v1/audit/{body['recommendation_id']}"
        ).json()
        assert any(
            event["action"] == "RECOMMENDATION_COMPLETED" for event in audit
        )


def test_recommendation_api_rejects_unknown_trial() -> None:
    with TestClient(app) as client:
        person_id = client.get("/api/v1/patients?limit=1").json()["items"][0][
            "person_id"
        ]
        response = client.post(
            "/api/v1/recommendations/run",
            json={"person_id": person_id, "trial_ids": ["MISSING"]},
        )

        assert response.status_code == 404
        assert response.json()["detail"]["trial_ids"] == ["MISSING"]


def test_recommendation_runs_trials_concurrently_and_preserves_order() -> None:
    active = 0
    peak_active = 0
    lock = threading.Lock()

    class ScreeningStub:
        def run(self, *, person_id: int, trial_id: str, actor: str):
            nonlocal active, peak_active
            with lock:
                active += 1
                peak_active = max(peak_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return SimpleNamespace(run=SimpleNamespace(trial_id=trial_id))

    class RankerStub:
        def rank(self, outputs, *, trial_catalog, top_k):
            return ([{"trial_id": item.run.trial_id} for item in outputs], [])

    class RunStoreStub:
        def new_recommendation_id(self):
            return "REC-test"

        def save_recommendation(self, recommendation_id, payload):
            self.payload = payload

    class AuditStub:
        def record(self, *args, **kwargs):
            return None

    orchestrator = RecommendationOrchestrator(
        screening=ScreeningStub(),
        repository=SimpleNamespace(
            trials={trial_id: {} for trial_id in ("T-1", "T-2", "T-3")}
        ),
        run_store=RunStoreStub(),
        audit=AuditStub(),
        ranker=RankerStub(),
        max_workers=2,
    )

    result = orchestrator.run(
        person_id=1,
        trial_ids=["T-1", "T-2", "T-3"],
        top_k=3,
        actor="test",
    )

    assert peak_active == 2
    assert [item["trial_id"] for item in result["recommended_trials"]] == [
        "T-1",
        "T-2",
        "T-3",
    ]
    assert result["limits"]["recommendation_max_workers"] == 2
