"""검토 큐 화면.

자동 판정이 확정하지 못한 항목을 사람이 승인·반려·재실행 요청으로 처리한다.
"""

from __future__ import annotations

import streamlit as st

from api_client import REVIEW_DECISIONS, TICKET_STATUSES, ApiError, JsonList
from config import settings
from views.common import (
    DECISION_LABELS,
    TICKET_STATUS_META,
    actor_value,
    client,
    load_trials,
    set_active_run,
    show_api_error,
    status_label,
    ticket_status_badge,
)


def render() -> None:
    st.title("검토 큐")
    st.caption(
        "자동 판정 신뢰도가 낮거나 기록이 어긋난 건입니다. 판정을 사람이 확정합니다."
    )

    try:
        trials = load_trials(settings.api_base_url)
    except ApiError as exc:
        show_api_error(exc, context="임상시험 목록을 불러오지 못했습니다.")
        trials = []

    trial_ids = [str(trial["trial_id"]) for trial in trials]

    with st.container(border=True):
        columns = st.columns(2)
        with columns[0]:
            selected_status = st.selectbox(
                "상태",
                options=("전체", *TICKET_STATUSES),
                format_func=lambda value: (
                    "전체"
                    if value == "전체"
                    else status_label(value, TICKET_STATUS_META)
                ),
                key="review_status_filter",
            )
        with columns[1]:
            selected_trial = st.selectbox(
                "임상시험",
                options=("전체", *trial_ids),
                key="review_trial_filter",
            )

    try:
        tickets: JsonList = client().list_review_queue(
            status=None if selected_status == "전체" else str(selected_status),
            trial_id=None if selected_trial == "전체" else str(selected_trial),
        )
    except ApiError as exc:
        show_api_error(exc, context="검토 큐를 불러오지 못했습니다.")
        return

    st.metric("검토 항목", len(tickets))
    if not tickets:
        st.info("조건에 해당하는 검토 항목이 없습니다.")
        return

    for ticket in tickets:
        _render_ticket(ticket)


def _render_ticket(ticket: dict[str, object]) -> None:
    ticket_id = str(ticket.get("ticket_id") or "")
    ticket_status = str(ticket.get("status") or "")
    run_id = str(ticket.get("run_id") or "")

    with st.container(border=True):
        st.markdown(
            f"{ticket_status_badge(ticket_status)} `{ticket_id}` · "
            f"person {ticket.get('person_id')} · {ticket.get('trial_id')}"
        )
        criterion_ids = list(ticket.get("criterion_ids") or [])  # type: ignore[arg-type]
        if criterion_ids:
            st.caption(
                "대상 기준: " + ", ".join(f"`{item}`" for item in criterion_ids)
            )
        st.caption(f"생성 {ticket.get('created_at')} · run_id `{run_id}`")

        if ticket.get("decided_at"):
            st.caption(
                f"처리 {ticket.get('decided_at')} · {ticket.get('decided_by')}"
                + (f" · {ticket.get('note')}" if ticket.get("note") else "")
            )

        if st.button("근거 열기", key=f"review_open_{ticket_id}"):
            set_active_run(
                run_id,
                person_id=int(ticket.get("person_id") or 0),
                trial_id=str(ticket.get("trial_id") or ""),
            )
            st.success("스크리닝 실행 화면에서 확인하세요.")

        if ticket_status != "PENDING":
            return

        with st.form(key=f"review_form_{ticket_id}"):
            decision = st.radio(
                "결정",
                options=REVIEW_DECISIONS,
                format_func=lambda value: DECISION_LABELS.get(value, value),
                horizontal=True,
                key=f"review_decision_{ticket_id}",
            )
            decided_by = st.text_input(
                "처리자", value=actor_value(), key=f"review_by_{ticket_id}"
            )
            note = st.text_area("메모", key=f"review_note_{ticket_id}")
            if st.form_submit_button("처리", type="primary"):
                if not decided_by.strip():
                    st.warning("처리자를 입력하세요.")
                    return
                try:
                    client().decide_review(
                        ticket_id=ticket_id,
                        decision=str(decision),
                        decided_by=decided_by.strip(),
                        note=note.strip() or None,
                    )
                except ApiError as exc:
                    show_api_error(exc, context="검토 처리가 실패했습니다.")
                    return
                st.success(
                    f"{DECISION_LABELS.get(str(decision), str(decision))} 처리했습니다."
                )
                st.rerun()
