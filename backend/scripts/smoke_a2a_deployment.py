"""Smoke-test the deployed independent A2A Lambdas with synthetic data.

Run from ``backend`` after ``HealthcareA2AStack`` is deployed::

    .venv/Scripts/python.exe scripts/smoke_a2a_deployment.py
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import boto3


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
API_ROOT = BACKEND_ROOT / "api"
for path in (str(REPO_ROOT), str(BACKEND_ROOT), str(API_ROOT)):
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)

from a2a_agents.contracts import REQUEST_SCHEMA, RESPONSE_SCHEMA  # noqa: E402
from app.orchestration.a2a_deliberation import A2AHttpTransport  # noqa: E402


def _outputs(*, stack_name: str, region: str) -> dict[str, str]:
    cloudformation = boto3.client("cloudformation", region_name=region)
    stacks = cloudformation.describe_stacks(StackName=stack_name)["Stacks"]
    return {
        item["OutputKey"]: item["OutputValue"]
        for item in stacks[0].get("Outputs", [])
    }


def _assert_independent_runtimes(outputs: dict[str, str], *, region: str) -> None:
    reviewer_name = outputs["ReviewerFunctionName"]
    challenger_name = outputs["ChallengerFunctionName"]
    client = boto3.client("lambda", region_name=region)
    reviewer = client.get_function_configuration(FunctionName=reviewer_name)
    challenger = client.get_function_configuration(FunctionName=challenger_name)
    assert reviewer["FunctionArn"] != challenger["FunctionArn"]
    assert reviewer["Role"] != challenger["Role"]
    assert f":{region}:" in reviewer["FunctionArn"]
    assert f":{region}:" in challenger["FunctionArn"]


def _criterion() -> dict[str, Any]:
    return {
        "criterion_id": "SMOKE-INCLUSION-AGE",
        "criterion_type": "INCLUSION",
        "label": "Age is at least 18 years",
        "expected_condition": ">= 18",
        "observed_value": "34",
        "observed_at": "2026-08-05",
        "status": "UNKNOWN",
        "confidence": 0.5,
        "source_ids": ["SMOKE-ANSWER-AGE"],
        "narratives": [
            {
                "source_id": "SMOKE-ANSWER-AGE",
                "observed_at": "2026-08-05",
                "text": "Synthetic applicant age: 34 years.",
                "score": 1.0,
            }
        ],
        "conflicts": [],
    }


def _assert_review(
    response: dict[str, Any],
    *,
    role: str,
    discussion_id: str,
) -> None:
    assert response.get("schema") == RESPONSE_SCHEMA
    assert response.get("role") == role
    assert response.get("discussion_id") == discussion_id
    decisions = response.get("decisions")
    assert isinstance(decisions, list) and decisions
    decision = decisions[0]
    assert decision.get("criterion_id") == "SMOKE-INCLUSION-AGE"
    assert str(decision.get("rationale", "")).strip(), "rationale is missing"
    source_ids = set(decision.get("source_ids") or [])
    assert source_ids, "source_ids are missing"
    assert source_ids <= {"SMOKE-ANSWER-AGE"}


def run(*, stack_name: str, region: str, timeout_seconds: int) -> dict[str, Any]:
    if boto3.Session().get_credentials() is None:
        raise RuntimeError("AWS credentials are unavailable")

    outputs = _outputs(stack_name=stack_name, region=region)
    required = {
        "ReviewerA2AUrl",
        "ChallengerA2AUrl",
        "ReviewerFunctionName",
        "ChallengerFunctionName",
    }
    missing = sorted(required - outputs.keys())
    if missing:
        raise RuntimeError(f"stack outputs are missing: {', '.join(missing)}")
    _assert_independent_runtimes(outputs, region=region)

    discussion_id = f"SMOKE-{uuid.uuid4()}"
    base = {
        "schema": REQUEST_SCHEMA,
        "discussion_id": discussion_id,
        "run_id": discussion_id,
        "criteria": [_criterion()],
    }
    transport = A2AHttpTransport(
        region=region,
        timeout_seconds=timeout_seconds,
    )
    reviewer = transport.review(
        outputs["ReviewerA2AUrl"],
        {**base, "round": 1},
    )
    _assert_review(
        reviewer,
        role="evidence_reviewer",
        discussion_id=discussion_id,
    )
    challenger = transport.review(
        outputs["ChallengerA2AUrl"],
        {**base, "round": 2, "prior_review": reviewer["decisions"]},
    )
    _assert_review(
        challenger,
        role="challenge_reviewer",
        discussion_id=discussion_id,
    )
    return {
        "ok": True,
        "region": region,
        "stack": stack_name,
        "independent_functions": True,
        "reviewer": reviewer,
        "challenger": challenger,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="ap-northeast-2")
    parser.add_argument("--stack-name", default="HealthcareA2AStack")
    parser.add_argument("--timeout-seconds", type=int, default=90)
    args = parser.parse_args()
    result = run(
        stack_name=args.stack_name,
        region=args.region,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
