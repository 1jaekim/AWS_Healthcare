"""Matching API coordination of two remote A2A agent endpoints."""

from __future__ import annotations

import httpx
from botocore.credentials import Credentials

from a2a_agents.contracts import RESPONSE_SCHEMA
from app.domain.models import CriterionResult, NarrativeSnippet
from app.domain.states import CriterionKind, CriterionStatus
from app.orchestration.a2a_deliberation import (
    A2ADeliberationAgent,
    LambdaFunctionUrlSigV4,
)
from app.safety.observability import TraceCollector


class ScriptedTransport:
    def __init__(self, reviewer: str = "OK", challenger: str = "OK") -> None:
        self.reviewer = reviewer
        self.challenger = challenger
        self.calls: list[tuple[str, dict]] = []

    def review(self, url: str, payload: dict) -> dict:
        self.calls.append((url, payload))
        role = "challenge_reviewer" if "challenger" in url else "evidence_reviewer"
        recommendation = self.challenger if role == "challenge_reviewer" else self.reviewer
        return {
            "schema": RESPONSE_SCHEMA,
            "role": role,
            "discussion_id": payload["discussion_id"],
            "round": payload["round"],
            "decisions": [
                {
                    "criterion_id": "T-C01",
                    "recommendation": recommendation,
                    "source_ids": ["APPLICATION-1"],
                    "rationale": f"{role} explanation",
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }


class StaticSession:
    def get_credentials(self):
        return Credentials("access-key", "secret-key", "session-token")


def _criterion() -> CriterionResult:
    return CriterionResult(
        criterion_id="T-C01",
        criterion_type="INCLUSION",
        label="HbA1c range",
        field_name="hba1c",
        kind=CriterionKind.NUMERIC_POINT,
        status=CriterionStatus.UNKNOWN,
        observed_value="7.8",
        expected_condition="7.5-10.5",
        unit="%",
        observed_at="2026-01-01",
        confidence=0.4,
        explanation="Review required",
        source_ids=("APPLICATION-1",),
        narrative=(
            NarrativeSnippet(
                note_id="APPLICATION-1",
                encounter_id="application",
                note_date="2026-01-01",
                snippet="Applicant reported HbA1c 7.8%.",
                matched_terms=("HbA1c",),
                score=0.9,
            ),
        ),
    )


def test_two_independent_calls_produce_grounded_explanation() -> None:
    transport = ScriptedTransport()
    trace = TraceCollector()
    result = A2ADeliberationAgent(
        reviewer_url="https://reviewer.lambda-url.test/",
        challenger_url="https://challenger.lambda-url.test/",
        transport=transport,
        trace=trace,
    ).deliberate([_criterion()], run_id="RUN-A2A")

    assert [call[0] for call in transport.calls] == [
        "https://reviewer.lambda-url.test/",
        "https://challenger.lambda-url.test/",
    ]
    assert transport.calls[1][1]["prior_review"]
    assert transport.calls[0][1]["discussion_id"] == transport.calls[1][1]["discussion_id"]
    assert result.items[0].recommendation == "OK"
    assert result.items[0].rationale == (
        "evidence_reviewer explanation",
        "challenge_reviewer explanation",
    )
    assert len(trace.spans_for("RUN-A2A")) == 2


def test_lambda_function_url_requests_are_sigv4_signed() -> None:
    request = httpx.Request(
        "POST",
        "https://reviewer.lambda-url.us-east-1.on.aws/",
        json={"jsonrpc": "2.0"},
    )
    auth = LambdaFunctionUrlSigV4(
        region="us-east-1",
        session=StaticSession(),  # type: ignore[arg-type]
    )

    signed = list(auth.auth_flow(request))[0]

    assert signed.headers["Authorization"].startswith("AWS4-HMAC-SHA256")
    assert signed.headers["X-Amz-Security-Token"] == "session-token"


def test_agent_disagreement_fails_closed_to_unknown() -> None:
    result = A2ADeliberationAgent(
        reviewer_url="https://reviewer.lambda-url.test/",
        challenger_url="https://challenger.lambda-url.test/",
        transport=ScriptedTransport(reviewer="OK", challenger="NOT_OK"),
        trace=TraceCollector(),
    ).deliberate([_criterion()])

    assert result.items[0].recommendation == "UNKNOWN"
    assert result.items[0].agreement is False


def test_definitive_answer_without_reason_fails_closed() -> None:
    transport = ScriptedTransport()
    original_review = transport.review

    def without_reason(url: str, payload: dict) -> dict:
        response = original_review(url, payload)
        response["decisions"][0]["rationale"] = ""
        return response

    transport.review = without_reason  # type: ignore[method-assign]
    result = A2ADeliberationAgent(
        reviewer_url="https://reviewer.lambda-url.test/",
        challenger_url="https://challenger.lambda-url.test/",
        transport=transport,
        trace=TraceCollector(),
    ).deliberate([_criterion()])

    assert result.items[0].recommendation == "UNKNOWN"
