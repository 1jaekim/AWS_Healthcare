"""Evaluate deployed Reviewer/Challenger A2A Lambdas on TrialGPT labels."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import boto3


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
API_ROOT = BACKEND_ROOT / "api"
DEFAULT_DATASET = (
    BACKEND_ROOT / "evaluation_data" / "trialgpt_criterion_sample.jsonl"
)
for path in (str(REPO_ROOT), str(BACKEND_ROOT), str(API_ROOT)):
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)

from a2a_agents.contracts import REQUEST_SCHEMA  # noqa: E402
from app.orchestration.a2a_deliberation import A2AHttpTransport  # noqa: E402
from smoke_a2a_deployment import (  # noqa: E402
    _assert_independent_runtimes,
    _outputs,
)


def load_cases(path: Path, *, limit: int) -> list[dict[str, Any]]:
    cases = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return cases[:limit] if limit else cases


def a2a_criterion(case: dict[str, Any]) -> dict[str, Any]:
    source_id = f"TRIALGPT-NOTE-{case['patient_id']}"
    criterion_text = str(case["criterion_text"])
    if str(case["criterion_type"]).upper() == "EXCLUSION":
        expected = f"Applicant must not meet this exclusion condition: {criterion_text}"
    else:
        expected = criterion_text
    return {
        "criterion_id": str(case["case_id"]),
        "criterion_type": str(case["criterion_type"]).upper(),
        "label": criterion_text,
        "expected_condition": expected,
        "observed_value": "See the supplied synthetic patient note.",
        "observed_at": None,
        "status": "UNKNOWN",
        "confidence": 0.0,
        "source_ids": [source_id],
        "narratives": [
            {
                "source_id": source_id,
                "observed_at": None,
                "text": str(case["patient_note"]),
                "score": 1.0,
            }
        ],
        "conflicts": [],
    }


def decision(response: dict[str, Any], criterion_id: str) -> dict[str, Any]:
    for item in response.get("decisions", []):
        if str(item.get("criterion_id")) == criterion_id:
            return dict(item)
    return {
        "criterion_id": criterion_id,
        "recommendation": "UNKNOWN",
        "source_ids": [],
        "rationale": "",
    }


def evaluate(
    cases: list[dict[str, Any]],
    *,
    reviewer_url: str,
    challenger_url: str,
    region: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    transport = A2AHttpTransport(region=region, timeout_seconds=timeout_seconds)
    rows: list[dict[str, Any]] = []
    for case in cases:
        discussion_id = f"EVAL-{uuid.uuid4()}"
        criterion = a2a_criterion(case)
        base = {
            "schema": REQUEST_SCHEMA,
            "discussion_id": discussion_id,
            "run_id": discussion_id,
            "criteria": [criterion],
        }
        reviewer = transport.review(reviewer_url, {**base, "round": 1})
        left = decision(reviewer, criterion["criterion_id"])
        challenger = transport.review(
            challenger_url,
            {**base, "round": 2, "prior_review": reviewer["decisions"]},
        )
        right = decision(challenger, criterion["criterion_id"])
        left_rec = str(left.get("recommendation", "UNKNOWN")).upper()
        right_rec = str(right.get("recommendation", "UNKNOWN")).upper()
        left_cited = set(left.get("source_ids") or [])
        right_cited = set(right.get("source_ids") or [])
        allowed = set(criterion["source_ids"])
        grounded = (
            bool(left_cited)
            and bool(right_cited)
            and left_cited <= allowed
            and right_cited <= allowed
        )
        explained = bool(str(left.get("rationale", "")).strip()) and bool(
            str(right.get("rationale", "")).strip()
        )
        valid_consensus = (
            left_rec == right_rec
            and left_rec in {"OK", "NOT_OK", "UNKNOWN"}
            and grounded
            and explained
        )
        consensus = left_rec if valid_consensus else "UNKNOWN"
        rows.append(
            {
                "case_id": case["case_id"],
                "expert": case["expected_recommendation"],
                "reviewer": left_rec,
                "challenger": right_rec,
                "consensus": consensus,
                "agreement": left_rec == right_rec,
                "grounded": grounded,
                "explained": explained,
                "valid_consensus": valid_consensus,
                "correct": valid_consensus
                and consensus == case["expected_recommendation"],
                "reviewer_rationale": left.get("rationale", ""),
                "challenger_rationale": right.get("rationale", ""),
            }
        )

    total = len(rows)
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        confusion[row["expert"]][row["consensus"]] += 1
    per_class_accuracy = {
        gold: sum(
            row["correct"] for row in rows if row["expert"] == gold
        )
        / count
        for gold, count in Counter(row["expert"] for row in rows).items()
    }
    decisive = [row for row in rows if row["expert"] != "UNKNOWN"]
    return {
        "schema": "clinical-deliberation-evaluation-result/v1",
        "total": total,
        "accuracy": sum(row["correct"] for row in rows) / total if total else 0.0,
        "agreement_rate": sum(row["agreement"] for row in rows) / total if total else 0.0,
        "grounded_rate": sum(row["grounded"] for row in rows) / total if total else 0.0,
        "explanation_rate": sum(row["explained"] for row in rows) / total if total else 0.0,
        "valid_consensus_rate": sum(row["valid_consensus"] for row in rows) / total
        if total
        else 0.0,
        "decisive_accuracy": sum(row["correct"] for row in decisive) / len(decisive)
        if decisive
        else 0.0,
        "prediction_counts": dict(Counter(row["consensus"] for row in rows)),
        "gold_counts": dict(Counter(row["expert"] for row in rows)),
        "per_class_accuracy": per_class_accuracy,
        "confusion_matrix": {
            gold: dict(predictions) for gold, predictions in sorted(confusion.items())
        },
        "cases": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--region", default="ap-northeast-2")
    parser.add_argument("--stack-name", default="HealthcareA2AStack")
    parser.add_argument("--timeout-seconds", type=int, default=90)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    cases = load_cases(args.dataset, limit=args.limit)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "ok": True,
                    "cases": len(cases),
                    "gold_counts": dict(
                        Counter(case["expected_recommendation"] for case in cases)
                    ),
                    "first_case": cases[0]["case_id"] if cases else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if boto3.Session().get_credentials() is None:
        raise RuntimeError("AWS credentials are unavailable")
    outputs = _outputs(stack_name=args.stack_name, region=args.region)
    _assert_independent_runtimes(outputs, region=args.region)
    result = evaluate(
        cases,
        reviewer_url=outputs["ReviewerA2AUrl"],
        challenger_url=outputs["ChallengerA2AUrl"],
        region=args.region,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
