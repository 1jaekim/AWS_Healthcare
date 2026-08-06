from __future__ import annotations

import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_ROOT = BACKEND_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from evaluate_trialgpt_a2a import a2a_criterion  # noqa: E402
from prepare_trialgpt_eval import (  # noqa: E402
    balanced_sample,
    expected_recommendation,
)


def test_trialgpt_expert_labels_map_to_three_way_recommendations():
    assert expected_recommendation("included") == "OK"
    assert expected_recommendation("not excluded") == "OK"
    assert expected_recommendation("not included") == "NOT_OK"
    assert expected_recommendation("excluded") == "NOT_OK"
    assert expected_recommendation("not enough information") == "UNKNOWN"
    assert expected_recommendation("not applicable") == "UNKNOWN"


def test_balanced_sample_interleaves_labels_for_limited_pilots():
    rows = [
        {"annotation_id": 1, "expert_eligibility": "included"},
        {"annotation_id": 2, "expert_eligibility": "included"},
        {"annotation_id": 3, "expert_eligibility": "excluded"},
        {"annotation_id": 4, "expert_eligibility": "excluded"},
    ]

    sample = balanced_sample(rows, per_label=2)

    assert [row["expert_eligibility"] for row in sample] == [
        "excluded",
        "included",
        "excluded",
        "included",
    ]


def test_exclusion_criterion_preserves_note_as_citable_evidence():
    criterion = a2a_criterion(
        {
            "case_id": "trialgpt-1",
            "patient_id": "patient-1",
            "criterion_type": "EXCLUSION",
            "criterion_text": "History of condition X",
            "patient_note": "The synthetic patient has no history of condition X.",
        }
    )

    assert criterion["expected_condition"].startswith("Applicant must not meet")
    assert criterion["source_ids"] == ["TRIALGPT-NOTE-patient-1"]
    assert criterion["narratives"][0]["text"].startswith("The synthetic patient")
