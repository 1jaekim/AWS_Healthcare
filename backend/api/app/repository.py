from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


VERDICT_PATTERN = re.compile(r"\s*임상시험 판정:.*$", re.DOTALL)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _optional_float(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    return float(value)


def _as_int(value: str | None, default: int = 0) -> int:
    if value is None or value.strip() == "":
        return default
    return int(float(value))


def _split_pipe(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split("|") if item.strip()]


def _sanitize_note(text: str) -> str:
    return VERDICT_PATTERN.sub("", text).rstrip()


class DatasetRepository:
    """Read-only repository backed by the generated local dataset."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self._validate_files()
        self.patients = {
            _as_int(row["person_id"]): row
            for row in _read_csv(data_dir / "patient_cohort.csv")
        }
        self.timeline_rows = _read_csv(data_dir / "timeline_flat.csv")
        self.trials = {
            row["trial_id"]: row
            for row in _read_csv(data_dir / "trial_definitions.csv")
        }
        self.criteria_rows = _read_csv(data_dir / "trial_criteria.csv")
        self.match_rows = _read_csv(data_dir / "patient_trial_matches.csv")
        self.evidence_rows = _read_csv(data_dir / "eligibility_evidence.csv")

        self.timelines: dict[int, list[dict[str, str]]] = defaultdict(list)
        for row in self.timeline_rows:
            self.timelines[_as_int(row["person_id"])].append(row)
        for events in self.timelines.values():
            events.sort(key=lambda row: _as_int(row["sequence_no"]))

        self.criteria: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in self.criteria_rows:
            self.criteria[row["trial_id"]].append(row)

        self.matches = {
            (_as_int(row["person_id"]), row["trial_id"]): row
            for row in self.match_rows
        }

        self.evidence: dict[tuple[int, str, str], list[dict[str, str]]] = defaultdict(list)
        for row in self.evidence_rows:
            key = (_as_int(row["person_id"]), row["trial_id"], row["encounter_id"])
            self.evidence[key].append(row)

        self.notes = self._load_canonical_notes(data_dir / "clinical_notes.jsonl")

    def _validate_files(self) -> None:
        required = (
            "patient_cohort.csv",
            "timeline_flat.csv",
            "trial_definitions.csv",
            "trial_criteria.csv",
            "patient_trial_matches.csv",
            "eligibility_evidence.csv",
            "clinical_notes.jsonl",
        )
        missing = [name for name in required if not (self.data_dir / name).exists()]
        if missing:
            raise FileNotFoundError(
                f"Dataset files are missing from {self.data_dir}: {', '.join(missing)}"
            )

    @staticmethod
    def _load_canonical_notes(path: Path) -> dict[str, str]:
        notes: dict[str, str] = {}
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                note = json.loads(line)
                if note.get("canonical_flag") == 1:
                    notes[str(note["encounter_id"])] = _sanitize_note(
                        str(note["free_text_emr"])
                    )
        return notes

    def counts(self) -> dict[str, int]:
        return {
            "patients": len(self.patients),
            "encounters": len(self.timeline_rows),
            "canonical_notes": len(self.notes),
            "trials": len(self.trials),
            "criteria": len(self.criteria_rows),
        }

    def list_patients(self, offset: int, limit: int) -> tuple[list[dict[str, Any]], int]:
        rows = [self.patient_summary(self.patients[key]) for key in sorted(self.patients)]
        return rows[offset : offset + limit], len(rows)

    def patient_summary(self, row: dict[str, str]) -> dict[str, Any]:
        return {
            "person_id": _as_int(row["person_id"]),
            "sex": row["sex"],
            "birth_date": row["birth_date"],
            "followup_start_date": row["followup_start_date"],
            "index_date": row["index_date"],
            "encounter_count": _as_int(row["selected_encounter_count"]),
            "synthetic_followup_count": _as_int(row["selected_synthetic_followups"]),
            "data_label": row["data_label"],
        }

    def timeline_event(self, row: dict[str, str]) -> dict[str, Any]:
        return {
            "encounter_id": row["encounter_id"],
            "sequence_no": _as_int(row["sequence_no"]),
            "encounter_date": row["encounter_date"],
            "synthetic_visit": row["synthetic_visit_flag"] == "1",
            "age": _as_int(row["age"]),
            "diabetes_status": row["diabetes_status"],
            "measurements": {
                "hba1c_pct": _optional_float(row["hba1c_pct"]),
                "fasting_glucose_mg_dl": _optional_float(row["fasting_glucose_mg_dl"]),
                "random_glucose_mg_dl": _optional_float(row["random_glucose_mg_dl"]),
                "bmi_kg_m2": _optional_float(row["bmi_kg_m2"]),
                "systolic_bp_mmhg": _optional_float(row["systolic_bp_mmhg"]),
                "diastolic_bp_mmhg": _optional_float(row["diastolic_bp_mmhg"]),
                "creatinine_mg_dl": _optional_float(row["creatinine_mg_dl"]),
                "egfr_ml_min_1_73m2": _optional_float(row["egfr_ml_min_1_73m2"]),
                "uacr_mg_g": _optional_float(row["uacr_mg_g"]),
            },
            "regimen": row["synthetic_diabetes_regimen"],
            "adherence_level": row["adherence_level"],
            "stable_regimen_days": _as_int(row["stable_regimen_days"]),
            "treatment_change": row["treatment_change"],
            "conditions": _split_pipe(row["source_condition_names"]),
            "medications": _split_pipe(row["source_drug_names"]),
            "canonical_note": self.notes.get(row["encounter_id"]),
        }

    def list_trials(self) -> list[dict[str, Any]]:
        return [self.trial_summary(self.trials[key]) for key in sorted(self.trials)]

    def trial_summary(self, row: dict[str, str]) -> dict[str, Any]:
        trial_id = row["trial_id"]
        return {
            "trial_id": trial_id,
            "trial_name": row["trial_name"],
            "description": row["description"],
            "purpose": row["purpose"],
            "synthetic_trial": row["synthetic_trial_flag"] == "1",
            "criteria_count": len(self.criteria[trial_id]),
        }

    def trial_criteria(self, trial_id: str) -> list[dict[str, Any]]:
        return [
            {
                "criterion_id": row["criterion_id"],
                "criterion_type": row["criterion_type"],
                "field": row["field"],
                "operator": row["operator"],
                "value_low": row["value_low"] or None,
                "value_high": row["value_high"] or None,
                "unit": row["unit"] or None,
            }
            for row in self.criteria[trial_id]
        ]

    def screening(self, person_id: int, trial_id: str) -> dict[str, Any] | None:
        match = self.matches.get((person_id, trial_id))
        if not match:
            return None
        evidence_key = (person_id, trial_id, match["index_encounter_id"])
        evidence = [
            {
                "criterion_id": row["criterion_id"],
                "criterion_type": row["criterion_type"],
                "criterion_name": row["criterion_name"],
                "status": row["criterion_status"],
                "observed_value": row["observed_value"] or None,
                "expected_condition": row["expected_condition"] or None,
                "unit": row["unit"] or None,
                "snapshot_date": row["snapshot_date"],
                "encounter_id": row["encounter_id"],
            }
            for row in self.evidence[evidence_key]
        ]
        failed = _split_pipe(match["failed_criteria"])
        return {
            "index_encounter_id": match["index_encounter_id"],
            "index_date": match["index_date"],
            "eligibility_status": match["eligibility_status"],
            "criteria_passed": _as_int(match["criteria_passed"]),
            "criteria_total": _as_int(match["criteria_total"]),
            "failed_criteria": failed,
            "evidence": evidence,
        }

