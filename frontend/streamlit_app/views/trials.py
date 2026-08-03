"""임상시험 화면.

시험 목록과 선정·제외 기준을 보여준다.
"""

from __future__ import annotations

import streamlit as st

from api_client import ApiError, JsonList
from config import settings
from views.common import (
    CRITERION_TYPE_LABELS,
    load_trial_detail,
    load_trials,
    show_api_error,
    show_table,
    trial_selectbox,
)


def render() -> None:
    st.title("임상시험")
    st.caption("모집 기준은 Criteria Store 에서 읽습니다. 기준이 바뀌면 기준 버전도 바뀝니다.")

    try:
        trials = load_trials(settings.api_base_url)
    except ApiError as exc:
        show_api_error(exc, context="임상시험 목록을 불러오지 못했습니다.")
        return

    show_table(
        [
            {
                "trial_id": trial.get("trial_id"),
                "시험명": trial.get("trial_name"),
                "목적": trial.get("purpose"),
                "기준 수": trial.get("criteria_count"),
                "합성 시험": trial.get("synthetic_trial"),
            }
            for trial in trials
        ],
        empty_message="등록된 임상시험이 없습니다.",
    )

    if not trials:
        return

    st.divider()
    trial_id = trial_selectbox(trials, key="trials_detail_select", label="상세 조회")
    if trial_id is None:
        return

    try:
        detail = load_trial_detail(settings.api_base_url, str(trial_id))
    except ApiError as exc:
        show_api_error(exc, context="임상시험 상세를 불러오지 못했습니다.")
        return

    st.subheader(str(detail.get("trial_name") or trial_id))
    st.write(detail.get("description") or "")
    st.caption(f"목적: {detail.get('purpose') or '—'}")

    criteria: JsonList = list(detail.get("criteria") or [])
    inclusion = [item for item in criteria if item.get("criterion_type") == "INCLUSION"]
    exclusion = [item for item in criteria if item.get("criterion_type") == "EXCLUSION"]

    columns = st.columns(2)
    columns[0].metric("선정 기준", len(inclusion))
    columns[1].metric("제외 기준", len(exclusion))

    for label, group in (("선정 기준", inclusion), ("제외 기준", exclusion)):
        st.markdown(f"**{label}**")
        show_table(
            [
                {
                    "기준": item.get("criterion_id"),
                    "구분": CRITERION_TYPE_LABELS.get(
                        str(item.get("criterion_type")), item.get("criterion_type")
                    ),
                    "필드": item.get("field"),
                    "연산자": item.get("operator"),
                    "하한": item.get("value_low"),
                    "상한": item.get("value_high"),
                    "단위": item.get("unit"),
                }
                for item in group
            ],
            empty_message=f"{label}이 없습니다.",
        )
