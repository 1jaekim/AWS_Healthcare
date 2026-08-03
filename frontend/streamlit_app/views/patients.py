"""환자 화면.

환자 목록과 방문 타임라인, 측정값 추이, 자유서술 기록을 보여준다.
"""

from __future__ import annotations

from typing import Any, Final

import pandas as pd
import streamlit as st

from api_client import ApiError, JsonDict, JsonList
from config import settings
from views.common import (
    client,
    load_patients,
    load_timeline,
    patient_selectbox,
    show_api_error,
    show_table,
)

MEASUREMENT_LABELS: Final[dict[str, str]] = {
    "hba1c_pct": "HbA1c (%)",
    "fasting_glucose_mg_dl": "공복혈당 (mg/dL)",
    "random_glucose_mg_dl": "임의혈당 (mg/dL)",
    "bmi_kg_m2": "BMI (kg/m²)",
    "systolic_bp_mmhg": "수축기혈압 (mmHg)",
    "diastolic_bp_mmhg": "이완기혈압 (mmHg)",
    "creatinine_mg_dl": "크레아티닌 (mg/dL)",
    "egfr_ml_min_1_73m2": "eGFR (mL/min/1.73m²)",
    "uacr_mg_g": "UACR (mg/g)",
}


def render() -> None:
    st.title("환자")
    st.caption("스크리닝 대상 환자의 방문 기록과 측정값을 확인합니다.")

    page_size = int(st.sidebar.number_input("페이지 크기", 5, 100, 20, 5, key="patients_page_size"))
    page = int(st.sidebar.number_input("페이지", 1, 999, 1, 1, key="patients_page")) - 1

    try:
        page_data = load_patients(settings.api_base_url, page_size, page * page_size)
    except ApiError as exc:
        show_api_error(exc, context="환자 목록을 불러오지 못했습니다.")
        return

    items: list[dict[str, Any]] = list(page_data.get("items") or [])
    total = int(page_data.get("total") or 0)
    st.caption(
        f"전체 {total}명 · {page * page_size + 1}–{page * page_size + len(items)} 표시"
    )

    show_table(
        [
            {
                "person_id": patient.get("person_id"),
                "성별": patient.get("sex"),
                "생년월일": patient.get("birth_date"),
                "추적 시작": patient.get("followup_start_date"),
                "인덱스 일자": patient.get("index_date"),
                "방문 수": patient.get("encounter_count"),
                "합성 추적": patient.get("synthetic_followup_count"),
                "라벨": patient.get("data_label"),
            }
            for patient in items
        ],
        empty_message="이 페이지에는 환자가 없습니다.",
    )

    if not items:
        return

    st.divider()
    person_id = patient_selectbox(items, key="patients_detail_select", label="상세 조회")
    if person_id is None:
        return

    _render_detail(int(person_id))
    _render_timeline(int(person_id))


def _render_detail(person_id: int) -> None:
    try:
        detail = client().get_patient(person_id)
    except ApiError as exc:
        show_api_error(exc, context="환자 상세를 불러오지 못했습니다.")
        return

    latest: JsonDict | None = detail.get("latest_event")
    st.subheader(f"person {person_id}")
    if latest is None:
        st.caption("방문 기록이 없습니다.")
        return

    columns = st.columns(4)
    columns[0].metric("최신 방문 나이", latest.get("age", "—"))
    columns[1].metric("당뇨 상태", latest.get("diabetes_status", "—"))
    columns[2].metric("요법 유지일", latest.get("stable_regimen_days", "—"))
    columns[3].metric("복약 순응도", latest.get("adherence_level", "—"))

    st.caption(
        f"인덱스 방문 `{latest.get('encounter_id')}` · {latest.get('encounter_date')}"
        f" · 요법 {latest.get('regimen')}"
    )


def _render_timeline(person_id: int) -> None:
    try:
        timeline = load_timeline(settings.api_base_url, person_id)
    except ApiError as exc:
        show_api_error(exc, context="타임라인을 불러오지 못했습니다.")
        return

    events: JsonList = list(timeline.get("events") or [])
    st.subheader("방문 타임라인")
    if not events:
        st.caption("방문 기록이 없습니다.")
        return

    show_table(
        [
            {
                "순번": event.get("sequence_no"),
                "방문일": event.get("encounter_date"),
                "encounter_id": event.get("encounter_id"),
                "합성 방문": event.get("synthetic_visit"),
                "나이": event.get("age"),
                "당뇨 상태": event.get("diabetes_status"),
                "요법": event.get("regimen"),
                "치료 변경": event.get("treatment_change"),
                **{
                    MEASUREMENT_LABELS.get(key, key): (event.get("measurements") or {}).get(key)
                    for key in MEASUREMENT_LABELS
                },
            }
            for event in events
        ]
    )

    _render_measurement_chart(events)
    _render_notes(events)


def _render_measurement_chart(events: JsonList) -> None:
    frame = pd.DataFrame(
        [
            {
                "방문일": event.get("encounter_date"),
                **{
                    MEASUREMENT_LABELS[key]: (event.get("measurements") or {}).get(key)
                    for key in MEASUREMENT_LABELS
                },
            }
            for event in events
        ]
    )
    available = [
        column
        for column in frame.columns
        if column != "방문일" and frame[column].notna().any()
    ]
    if not available:
        return

    st.markdown("**측정값 추이**")
    default = [
        label
        for label in (MEASUREMENT_LABELS["hba1c_pct"], MEASUREMENT_LABELS["egfr_ml_min_1_73m2"])
        if label in available
    ]
    selected = st.multiselect(
        "지표",
        options=available,
        default=default or available[:1],
        key="timeline_metrics",
    )
    if not selected:
        return
    st.line_chart(frame.set_index("방문일")[selected], height=280)


def _render_notes(events: JsonList) -> None:
    with_notes = [event for event in events if event.get("canonical_note")]
    if not with_notes:
        return

    st.markdown("**자유서술 기록**")
    for event in with_notes:
        with st.expander(
            f"{event.get('encounter_date')} · {event.get('encounter_id')}"
        ):
            st.write(event.get("canonical_note"))
