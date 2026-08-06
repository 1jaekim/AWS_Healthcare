"""Create a deterministic, balanced A2A evaluation sample from TrialGPT."""

from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any


DATASET = "ncbi/TrialGPT-Criterion-Annotations"
ROWS_ENDPOINT = "https://datasets-server.huggingface.co/rows"
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "evaluation_data"
    / "trialgpt_criterion_sample.jsonl"
)


def expected_recommendation(label: str) -> str:
    mapping = {
        "included": "OK",
        "not excluded": "OK",
        "not included": "NOT_OK",
        "excluded": "NOT_OK",
        "not enough information": "UNKNOWN",
        "not applicable": "UNKNOWN",
    }
    try:
        return mapping[label.strip().lower()]
    except KeyError as exc:
        raise ValueError(f"unsupported expert label: {label}") from exc


def fetch_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    total = None
    while total is None or offset < total:
        query = urllib.parse.urlencode(
            {
                "dataset": DATASET,
                "config": "default",
                "split": "train",
                "offset": offset,
                "length": 100,
            }
        )
        request = urllib.request.Request(
            f"{ROWS_ENDPOINT}?{query}",
            headers={"User-Agent": "clinical-a2a-evaluator/1.0"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            page = json.load(response)
        total = int(page["num_rows_total"])
        batch = [item["row"] for item in page.get("rows", [])]
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)
    return rows


def balanced_sample(
    rows: list[dict[str, Any]], *, per_label: int
) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sorted(rows, key=lambda item: int(item["annotation_id"])):
        buckets[str(row["expert_eligibility"]).lower()].append(row)

    selected: list[dict[str, Any]] = []
    labels = sorted(buckets)
    # Interleave labels so --limit 6/12/... remains balanced during cheap pilots.
    for index in range(per_label):
        for label in labels:
            if index < len(buckets[label]):
                selected.append(buckets[label][index])
    return selected


def convert(row: dict[str, Any]) -> dict[str, Any]:
    label = str(row["expert_eligibility"]).strip().lower()
    return {
        "schema": "clinical-deliberation-eval/v1",
        "case_id": f"trialgpt-{row['annotation_id']}",
        "source": DATASET,
        "annotation_id": int(row["annotation_id"]),
        "patient_id": str(row["patient_id"]),
        "trial_id": str(row["trial_id"]),
        "trial_title": str(row["trial_title"]),
        "criterion_type": str(row["criterion_type"]).upper(),
        "criterion_text": str(row["criterion_text"]),
        "patient_note": str(row["note"]),
        "expert_eligibility": label,
        "expected_recommendation": expected_recommendation(label),
        "expert_sentences": str(row.get("expert_sentences", "")),
        "gpt4_explanation": str(row.get("gpt4_explanation", "")),
        "explanation_correctness": str(row.get("explanation_correctness", "")),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-label", type=int, default=5)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.per_label < 1:
        raise ValueError("--per-label must be positive")

    rows = fetch_rows()
    sample = [convert(row) for row in balanced_sample(rows, per_label=args.per_label)]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="\n") as handle:
        for item in sample:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    counts: dict[str, int] = defaultdict(int)
    for item in sample:
        counts[item["expert_eligibility"]] += 1
    print(
        json.dumps(
            {
                "source_rows": len(rows),
                "sample_rows": len(sample),
                "labels": dict(sorted(counts.items())),
                "output": str(args.output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
