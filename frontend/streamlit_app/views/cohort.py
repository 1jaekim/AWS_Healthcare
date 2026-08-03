"""코호트 화면.

여러 환자를 배치 실행한 뒤 퍼널·병목 기준·검토 우선순위를 집계해 보여준다.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from api_client import ApiError, JsonDict, JsonList
from config import settings
from views.common import (
    ELIGIBILITY_META,
    actor_value,
    client,
    clear_caches,
    load_patients,
    load_trials,
    set_active_run,
    show_api_error,
    show_table,
    status_label,
    trial_selectbox,
)


def render() -> None:
    st.title("코호트 현황")
    st.caption("임상시험 한 건에 대해 여러 환자를 실행하고 모집 병목을 찾습니다.")

    try:
        trials = load_trials(settings.api_base_url)
        patients_page = load_patients(settings.api_base_url, 100, 0)
    except ApiError as exc:
        show_api_error(exc, context="목록을 불러오지 못했습니다.")
        return

    patients: list[dict[str, Any]] = list(patients_page.get("items") or [])
    all_person_ids = [int(patient["person_id"]) for patient in patients]

    with st.container(border=True):
        trial_id = trial_selectbox(trials, key="cohort_trial")
        if trial_id is None:
            return

        mode = st.radio(
            "대상 선택",
            options=("자동", "직접 선택"),
            horizontal=True,
            key="cohort_mode",
            help="자동은 백엔드가 환자 목록 앞쪽에서 지정한 수만큼 고릅니다.",
        )

        person_ids: list[int] | None = None
        limit = 20
        if mode == "자동":
            limit = int(
                st.slider("실행 인원", min_value=1, max_value=200, value=20, step=1)
            )
        else:
            person_ids = list(
                st.multiselect(
                    "환자 선택",
                    options=all_person_ids,
                    default=all_person_ids[:10],
                    format_func=lambda value: f"person {value}",
                    key="cohort_person_ids",
                )
            )

        columns = st.columns(2)
        run_clicked = columns[0].button("배치 실행", type="primary")
        load_clicked = columns[1].button("기존 결과만 조회")

    view: JsonDict | None = None
    if run_clicked:
        if mode == "직접 선택" and not person_ids:
            st.warning("환자를 한 명 이상 선택하세요.")
        else:
            with st.spinner("배치 실행 중입니다. 인원이 많으면 시간이 걸립니다."):
                try:
                    view = client().run_cohort(
                        trial_id=str(trial_id),
                        person_ids=person_ids,
                        limit=limit,
                        actor=actor_value(),
                    )
                except ApiError as exc:
                    show_api_error(exc, context="배치 실행이 실패했습니다.")
                    return
            clear_caches()
    elif load_clicked:
        try:
            view = client().get_cohort(str(trial_id))
        except ApiError as exc:
            show_api_error(exc, context="코호트 현황을 불러오지 못했습니다.")
            return

    if view is None:
        st.info("배치를 실행하거나 기존 결과를 조회하세요.")
        return

    _render_funnel(view)
    _render_bottlenecks(view.get("bottlenecks") or [])
    _render_review_priority(view.get("review_priority") or [], str(trial_id))


def _render_funnel(view: JsonDict) -> None:
    st.subheader("판정 퍼널")
    st.metric("스크리닝 인원", view.get("total_screened", 0))

    funnel: JsonList = list(view.get("funnel") or [])
    if not funnel:
        st.caption("집계할 실행 결과가 없습니다.")
        return

    frame = pd.DataFrame(
        [
            {
                "판정": status_label(str(stage.get("status")), ELIGIBILITY_META),
                "인원": int(stage.get("count") or 0),
            }
            for stage in funnel
        ]
    ).set_index("판정")
    st.bar_chart(frame, height=260)

    show_table(
        [
            {
                "판정": status_label(str(stage.get("status")), ELIGIBILITY_META),
                "라벨": stage.get("label"),
                "인원": stage.get("count"),
                "비율": f"{float(stage.get('share') or 0) * 100:.1f}%",
            }
            for stage in funnel
        ]
    )


def _render_bottlenecks(bottlenecks: JsonList) -> None:
    st.subheader("병목 기준")
    if not bottlenecks:
        st.caption("병목으로 집계된 기준이 없습니다.")
        return

    st.caption("차단율이 높은 기준이 모집을 가장 많이 막고 있습니다.")
    show_table(
        [
            {
                "기준": entry.get("criterion_id"),
                "항목": entry.get("label"),
                "필드": entry.get("field"),
                "평가": entry.get("evaluated"),
                "충돌": entry.get("contradicted"),
                "정보 없음": entry.get("unknown"),
                "불일치": entry.get("conflicting"),
                "검토 필요": entry.get("review_required"),
                "미해결": entry.get("unresolved"),
                "차단율": f"{float(entry.get('block_rate') or 0) * 100:.1f}%",
            }
            for entry in bottlenecks
        ]
    )


def _render_review_priority(items: JsonList, trial_id: str) -> None:
    st.subheader("검토 우선순위")
    if not items:
        st.caption("검토가 필요한 실행이 없습니다.")
        return

    for entry in items:
        run_id = str(entry.get("run_id") or "")
        with st.container(border=True):
            columns = st.columns([3, 1])
            with columns[0]:
                st.markdown(
                    f"**person {entry.get('person_id')}** · "
                    f"{status_label(str(entry.get('eligibility_status')), ELIGIBILITY_META)} · "
                    f"충족 {entry.get('criteria_met')}/{entry.get('criteria_total')} · "
                    f"완성도 {float(entry.get('completion') or 0) * 100:.0f}%"
                )
                unresolved = list(entry.get("unresolved_criteria") or [])
                if unresolved:
                    st.caption(
                        f"미해결 {entry.get('unresolved_count')}건: "
                        + ", ".join(f"`{item}`" for item in unresolved)
                    )
                st.caption(f"run_id `{run_id}`")
            with columns[1]:
                if st.button("근거 열기", key=f"open_run_{run_id}"):
                    set_active_run(
                        run_id,
                        person_id=int(entry.get("person_id") or 0),
                        trial_id=trial_id,
                    )
                    st.success("스크리닝 실행 화면에서 확인하세요.")
