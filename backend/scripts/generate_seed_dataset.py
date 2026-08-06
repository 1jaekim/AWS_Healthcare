#!/usr/bin/env python3
"""배포용 시드 데이터셋 생성기.

`backend/api/app/repository.py` 의 `DatasetRepository` 는 기동 시점에 CSV 7종을
읽는다. 원본 합성 데이터셋(`outputs/longitudinal_emr_v2/`)은 레포 밖에서 공유되고
`.gitignore` 로 빠져 있어서, 배포 패키지에 넣을 것이 없다. 데이터셋이 없으면
API 는 `FileNotFoundError` 로 기동 자체가 안 된다.

그래서 스키마만 같은 최소 시드를 만든다. 실제 환자 데이터가 아니라 배포된 API 가
뜨고 화면이 돌아가는 것을 확인하기 위한 자리표시자다. 진짜 데이터셋이 준비되면
`EMR_DATA_DIR` 를 그쪽으로 돌리거나 이 스크립트가 만든 디렉터리를 덮어쓰면 된다.

    python3 backend/scripts/generate_seed_dataset.py backend/api/dataset

값은 난수가 아니라 고정된 표를 쓴다. 같은 입력에 같은 판정이 재현되어야 한다는
아키텍처 원칙(`doc/ARCHITECTURE_V2.md` 보안 원칙 마지막 줄)이 시드에도 그대로
적용되기 때문이다.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path


DATA_LABEL = "SEED_PLACEHOLDER"
"""모든 시드 행에 붙는 표식. 실제 코호트와 섞였을 때 구분하기 위한 것이다."""


# ─── 공고 ────────────────────────────────────────────────────────────────
# 기준은 `TrialCriteriaTable` 의 JSON 과 같은 모양(field/operator/value/unit)으로
# 둔다. 판정 Source of Truth 가 기준 JSON 이라는 아키텍처 결정과 어긋나지 않게
# 하기 위해서다.

TRIALS = [
    {
        "trial_id": "SEED-T2D-001",
        "trial_name": "제2형 당뇨 혈당조절 강화 연구 (시드)",
        "description": "경구약 단독으로 목표에 도달하지 못한 성인 대상 시드 공고",
        "purpose": "혈당조절 강화 요법의 유효성 평가",
        "synthetic_trial_flag": "1",
    },
    {
        "trial_id": "SEED-CKD-002",
        "trial_name": "당뇨병성 신질환 진행 억제 연구 (시드)",
        "description": "알부민뇨가 확인된 제2형 당뇨 환자 대상 시드 공고",
        "purpose": "신기능 저하 속도 감소 평가",
        "synthetic_trial_flag": "1",
    },
]

CRITERIA = [
    # trial_id, criterion_id, type, field, operator, low, high, unit
    #
    # `field` 는 FIELD_CATALOG 의 키여야 한다. 측정 컬럼 이름(`hba1c_pct`)이 아니다.
    # TimelineGraphTool 의 _MEASUREMENT_COLUMNS 가 카탈로그 키를 측정 컬럼으로
    # 옮겨주므로, 여기에 컬럼 이름을 적으면 spec_for() 가 카탈로그를 못 찾아
    # 조건이 NARRATIVE 로 떨어진다. 그러면 그래프 조회와 규칙 계산을 건너뛰고
    # 자유서술 검색으로만 확인하게 되어, 색인이 비어 있는 동안 전부 UNKNOWN 이
    # 된다. 지원서 스키마의 기준 파생 항목도 만들어지지 않는다.
    #
    # EXCLUSION 은 '위험 없음' 조건으로 적는다. 규칙 충족이 곧 통과라는 규약이다
    # (aggregator._resolve_status, judgment._check_criterion_type 참고).
    # "eGFR 45 미만은 제외" 는 `egfr >= 45` 로 적어야 한다. `egfr < 45` 로 적으면
    # 방향이 뒤집혀, eGFR 이 정상인 환자가 CONTRADICTED 로 차단된다.
    ("SEED-T2D-001", "inc_age_001", "INCLUSION", "age", ">=", "19", "", "years"),
    ("SEED-T2D-001", "inc_hba1c_001", "INCLUSION", "hba1c", "between", "7.0", "11.0", "%"),
    ("SEED-T2D-001", "exc_egfr_001", "EXCLUSION", "egfr", ">=", "45", "", "mL/min/1.73m2"),
    ("SEED-CKD-002", "inc_age_002", "INCLUSION", "age", ">=", "19", "", "years"),
    ("SEED-CKD-002", "inc_uacr_002", "INCLUSION", "uacr", ">=", "30", "", "mg/g"),
    ("SEED-CKD-002", "exc_egfr_002", "EXCLUSION", "egfr", ">=", "25", "", "mL/min/1.73m2"),
]


# ─── 환자 ────────────────────────────────────────────────────────────────
# 세 명이면 화면의 목록·상세·타임라인·판정 경로를 모두 밟을 수 있다. 한 명은
# 적격, 한 명은 부적격, 한 명은 근거 부족(UNKNOWN)이 나오도록 값을 잡았다.

PATIENTS = [
    {
        "person_id": 1,
        "sex": "F",
        "birth_date": "1972-04-11",
        "followup_start_date": "2024-01-15",
        "index_date": "2026-05-20",
        "age": 54,
        "hba1c_pct": "8.2",
        "fasting_glucose_mg_dl": "162",
        "random_glucose_mg_dl": "214",
        "bmi_kg_m2": "27.4",
        "systolic_bp_mmhg": "134",
        "diastolic_bp_mmhg": "82",
        "creatinine_mg_dl": "0.9",
        "egfr_ml_min_1_73m2": "78",
        "uacr_mg_g": "42",
        "regimen": "metformin",
        "conditions": "제2형 당뇨병|고혈압",
        "drugs": "metformin|amlodipine",
    },
    {
        "person_id": 2,
        "sex": "M",
        "birth_date": "1958-09-02",
        "followup_start_date": "2023-11-03",
        "index_date": "2026-06-08",
        "age": 67,
        "hba1c_pct": "6.4",
        "fasting_glucose_mg_dl": "118",
        "random_glucose_mg_dl": "141",
        "bmi_kg_m2": "24.1",
        "systolic_bp_mmhg": "128",
        "diastolic_bp_mmhg": "76",
        "creatinine_mg_dl": "1.8",
        "egfr_ml_min_1_73m2": "38",
        "uacr_mg_g": "310",
        "regimen": "metformin+dpp4",
        "conditions": "제2형 당뇨병|만성 신장질환",
        "drugs": "metformin|sitagliptin",
    },
    {
        "person_id": 3,
        "sex": "F",
        "birth_date": "1985-01-27",
        "followup_start_date": "2025-02-19",
        "index_date": "2026-07-01",
        "age": 41,
        "hba1c_pct": "",  # 최근 검사 없음 → UNKNOWN 경로
        "fasting_glucose_mg_dl": "148",
        "random_glucose_mg_dl": "",
        "bmi_kg_m2": "31.2",
        "systolic_bp_mmhg": "142",
        "diastolic_bp_mmhg": "88",
        "creatinine_mg_dl": "0.8",
        "egfr_ml_min_1_73m2": "92",
        "uacr_mg_g": "",
        "regimen": "none",
        "conditions": "제2형 당뇨병",
        "drugs": "",
    },
]

VISITS_PER_PATIENT = 3
"""환자당 방문 수. 타임라인이 한 점이면 화면에서 순서를 확인할 수 없다."""


PATIENT_COHORT_COLUMNS = [
    "person_id",
    "sex",
    "birth_date",
    "followup_start_date",
    "index_date",
    "selected_encounter_count",
    "selected_synthetic_followups",
    "data_label",
]

TIMELINE_COLUMNS = [
    "person_id",
    "encounter_id",
    "sequence_no",
    "encounter_date",
    "synthetic_visit_flag",
    "age",
    "diabetes_status",
    "hba1c_pct",
    "fasting_glucose_mg_dl",
    "random_glucose_mg_dl",
    "bmi_kg_m2",
    "systolic_bp_mmhg",
    "diastolic_bp_mmhg",
    "creatinine_mg_dl",
    "egfr_ml_min_1_73m2",
    "uacr_mg_g",
    "synthetic_diabetes_regimen",
    "adherence_level",
    "stable_regimen_days",
    "treatment_change",
    "source_condition_names",
    "source_drug_names",
]

TRIAL_COLUMNS = [
    "trial_id",
    "trial_name",
    "description",
    "purpose",
    "synthetic_trial_flag",
]

CRITERIA_COLUMNS = [
    "trial_id",
    "criterion_id",
    "criterion_type",
    "field",
    "operator",
    "value_low",
    "value_high",
    "unit",
]

MATCH_COLUMNS = [
    "person_id",
    "trial_id",
    "index_encounter_id",
    "index_date",
    "eligibility_status",
    "criteria_passed",
    "criteria_total",
    "failed_criteria",
]

EVIDENCE_COLUMNS = [
    "person_id",
    "trial_id",
    "encounter_id",
    "criterion_id",
    "criterion_type",
    "criterion_name",
    "criterion_status",
    "observed_value",
    "expected_condition",
    "unit",
    "snapshot_date",
]


def _encounter_id(person_id: int, sequence_no: int) -> str:
    return f"seed-enc-{person_id:03d}-{sequence_no}"


def _visit_dates(index_date: str) -> list[str]:
    """마지막 방문이 index_date 가 되도록 3회 방문 날짜를 만든다.

    판정은 인덱스 방문 시점의 관찰값으로 계산되므로(`app/main.py` 의 한계 문구),
    마지막 방문이 인덱스와 같아야 시드 판정과 화면이 어긋나지 않는다.
    """
    year, month, day = (int(part) for part in index_date.split("-"))
    earlier_months = [month - 6, month - 3, month]
    dates = []
    for shift in earlier_months:
        shifted_year, shifted_month = year, shift
        while shifted_month < 1:
            shifted_month += 12
            shifted_year -= 1
        dates.append(f"{shifted_year:04d}-{shifted_month:02d}-{day:02d}")
    return dates


def _criterion_name(field: str) -> str:
    return {
        "age": "연령",
        "hba1c_pct": "당화혈색소",
        "egfr_ml_min_1_73m2": "추정 사구체여과율",
        "uacr_mg_g": "요 알부민/크레아티닌 비",
    }.get(field, field)


def _expected_condition(operator: str, low: str, high: str, unit: str) -> str:
    if operator == "between":
        return f"{low} ~ {high} {unit}".strip()
    return f"{operator} {low} {unit}".strip()


def _evaluate(operator: str, low: str, high: str, observed: str) -> str:
    """시드 판정. 값이 없으면 UNKNOWN 이다.

    아키텍처의 Verifier 규칙과 같은 방향이다: 근거가 없으면 실패가 아니라
    UNKNOWN 이고, 추가 정보 수집 대상이 된다.
    """
    if observed == "":
        return "UNKNOWN"
    value = float(observed)
    if operator == ">=":
        return "PASS" if value >= float(low) else "FAIL"
    if operator == "<":
        return "PASS" if value < float(low) else "FAIL"
    if operator == "between":
        return "PASS" if float(low) <= value <= float(high) else "FAIL"
    return "UNKNOWN"


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def generate(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)

    cohort_rows = []
    timeline_rows = []
    note_rows = []

    for patient in PATIENTS:
        person_id = int(patient["person_id"])
        dates = _visit_dates(str(patient["index_date"]))

        cohort_rows.append(
            {
                "person_id": person_id,
                "sex": patient["sex"],
                "birth_date": patient["birth_date"],
                "followup_start_date": patient["followup_start_date"],
                "index_date": patient["index_date"],
                "selected_encounter_count": VISITS_PER_PATIENT,
                "selected_synthetic_followups": VISITS_PER_PATIENT - 1,
                "data_label": DATA_LABEL,
            }
        )

        for sequence_no, encounter_date in enumerate(dates, start=1):
            encounter_id = _encounter_id(person_id, sequence_no)
            is_index = sequence_no == VISITS_PER_PATIENT
            timeline_rows.append(
                {
                    "person_id": person_id,
                    "encounter_id": encounter_id,
                    "sequence_no": sequence_no,
                    "encounter_date": encounter_date,
                    "synthetic_visit_flag": "0" if is_index else "1",
                    "age": patient["age"],
                    "diabetes_status": "T2DM",
                    "hba1c_pct": patient["hba1c_pct"] if is_index else "",
                    "fasting_glucose_mg_dl": patient["fasting_glucose_mg_dl"] if is_index else "",
                    "random_glucose_mg_dl": patient["random_glucose_mg_dl"] if is_index else "",
                    "bmi_kg_m2": patient["bmi_kg_m2"] if is_index else "",
                    "systolic_bp_mmhg": patient["systolic_bp_mmhg"] if is_index else "",
                    "diastolic_bp_mmhg": patient["diastolic_bp_mmhg"] if is_index else "",
                    "creatinine_mg_dl": patient["creatinine_mg_dl"] if is_index else "",
                    "egfr_ml_min_1_73m2": patient["egfr_ml_min_1_73m2"] if is_index else "",
                    "uacr_mg_g": patient["uacr_mg_g"] if is_index else "",
                    "synthetic_diabetes_regimen": patient["regimen"],
                    "adherence_level": "GOOD",
                    "stable_regimen_days": 90,
                    "treatment_change": "NONE",
                    "source_condition_names": patient["conditions"],
                    "source_drug_names": patient["drugs"],
                }
            )
            note_rows.append(
                {
                    "encounter_id": encounter_id,
                    "canonical_flag": 1,
                    "free_text_emr": (
                        f"{encounter_date} 외래. 제2형 당뇨로 추적 중. "
                        f"현재 요법: {patient['regimen']}. "
                        "이 문서는 배포 확인용 시드 기록이며 실제 진료 기록이 아니다."
                    ),
                }
            )

    _write_csv(target / "patient_cohort.csv", PATIENT_COHORT_COLUMNS, cohort_rows)
    _write_csv(target / "timeline_flat.csv", TIMELINE_COLUMNS, timeline_rows)
    _write_csv(target / "trial_definitions.csv", TRIAL_COLUMNS, TRIALS)

    criteria_rows = [
        dict(zip(CRITERIA_COLUMNS, row)) for row in CRITERIA
    ]
    _write_csv(target / "trial_criteria.csv", CRITERIA_COLUMNS, criteria_rows)

    match_rows = []
    evidence_rows = []
    for patient in PATIENTS:
        person_id = int(patient["person_id"])
        index_encounter_id = _encounter_id(person_id, VISITS_PER_PATIENT)
        for trial in TRIALS:
            trial_id = trial["trial_id"]
            trial_criteria = [row for row in criteria_rows if row["trial_id"] == trial_id]
            passed = 0
            failed: list[str] = []
            for criterion in trial_criteria:
                field = str(criterion["field"])
                observed = str(patient["age"]) if field == "age" else str(patient.get(field, ""))
                status = _evaluate(
                    str(criterion["operator"]),
                    str(criterion["value_low"]),
                    str(criterion["value_high"]),
                    observed,
                )
                # 제외 기준은 조건을 만족하면 탈락이다. 부호를 뒤집어 화면에
                # 보이는 PASS/FAIL 의미를 선정 기준과 통일한다.
                if criterion["criterion_type"] == "EXCLUSION" and status in {"PASS", "FAIL"}:
                    status = "FAIL" if status == "PASS" else "PASS"
                if status == "PASS":
                    passed += 1
                elif status == "FAIL":
                    failed.append(str(criterion["criterion_id"]))
                evidence_rows.append(
                    {
                        "person_id": person_id,
                        "trial_id": trial_id,
                        "encounter_id": index_encounter_id,
                        "criterion_id": criterion["criterion_id"],
                        "criterion_type": criterion["criterion_type"],
                        "criterion_name": _criterion_name(field),
                        "criterion_status": status,
                        "observed_value": observed,
                        "expected_condition": _expected_condition(
                            str(criterion["operator"]),
                            str(criterion["value_low"]),
                            str(criterion["value_high"]),
                            str(criterion["unit"]),
                        ),
                        "unit": criterion["unit"],
                        "snapshot_date": patient["index_date"],
                    }
                )

            total = len(trial_criteria)
            if failed:
                eligibility = "INELIGIBLE"
            elif passed == total:
                eligibility = "ELIGIBLE"
            else:
                eligibility = "UNKNOWN"

            match_rows.append(
                {
                    "person_id": person_id,
                    "trial_id": trial_id,
                    "index_encounter_id": index_encounter_id,
                    "index_date": patient["index_date"],
                    "eligibility_status": eligibility,
                    "criteria_passed": passed,
                    "criteria_total": total,
                    "failed_criteria": "|".join(failed),
                }
            )

    _write_csv(target / "patient_trial_matches.csv", MATCH_COLUMNS, match_rows)
    _write_csv(target / "eligibility_evidence.csv", EVIDENCE_COLUMNS, evidence_rows)

    with (target / "clinical_notes.jsonl").open("w", encoding="utf-8") as handle:
        for note in note_rows:
            handle.write(json.dumps(note, ensure_ascii=False) + "\n")

    print(f"시드 데이터셋 생성 완료: {target}")
    print(f"  환자 {len(cohort_rows)}명 · 방문 {len(timeline_rows)}건 · 공고 {len(TRIALS)}건")


if __name__ == "__main__":
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("backend/api/dataset")
    generate(destination.resolve())
