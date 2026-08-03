from fastapi.testclient import TestClient

from app.main import app


def test_health_reports_loaded_dataset() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["data_counts"]["patients"] == 348
    assert body["data_counts"]["encounters"] == 2097
    assert body["data_counts"]["canonical_notes"] == 2097
    assert body["rag_status"] == "not_configured"


def test_patient_timeline_excludes_embedded_trial_verdict() -> None:
    with TestClient(app) as client:
        patient_id = client.get("/api/v1/patients?limit=1").json()["items"][0]["person_id"]
        response = client.get(f"/api/v1/patients/{patient_id}/timeline")

    assert response.status_code == 200
    events = response.json()["events"]
    assert events
    assert all("임상시험 판정:" not in (event["canonical_note"] or "") for event in events)


def test_trial_detail_contains_versionable_criteria() -> None:
    with TestClient(app) as client:
        trial_id = client.get("/api/v1/trials").json()[0]["trial_id"]
        response = client.get(f"/api/v1/trials/{trial_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["criteria_count"] == len(body["criteria"])
    assert body["criteria_count"] > 0


def test_screening_returns_decision_and_criterion_evidence() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/screenings",
            json={"person_id": 3, "trial_id": "SYN-T2D-INTENSIFY-01"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["eligibility_status"] == "INELIGIBLE"
    assert body["criteria_total"] == len(body["evidence"])
    assert body["failed_criteria"]
    assert body["metadata"] == {"rag_used": False, "graph_used": False}


def test_unknown_patient_returns_404() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/patients/999999999")

    assert response.status_code == 404

