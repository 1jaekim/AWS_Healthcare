"""스크리닝 실행 화면.

환자 x 시험 한 건을 실행하고, 기준별 근거·확인 질문·설명·트레이스를 보여준다.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from api_client import ApiError, JsonDict, JsonList
from config import settings
from views.common import (
    SS_ACTOR,
    SS_RUN_ID,
    actor_value,
    client,
    criterion_status_badge,
    eligibility_badge,
    load_patients,
    load_trials,
    patient_selectbox,
    set_active_run,
    show_api_error,
    show_table,
    status_label,
    trial_selectbox,
    CRITERION_STATUS_META,
    CRITERION_TYPE_LABELS,
)

AUDIENCE_LABELS: dict[str, str] = {
    "admin": "연구 담당자용",
    "patient": "참여자용",
    "participant": "참여자용",
    "researcher": "연구자용",
    "coordinator": "코디네이터용",
    "clinician": "임상의용",
}

MODE_LABELS: dict[str, str] = {
    "deterministic": "규칙 기반",
    "agent:bedrock": "에이전트 (Bedrock)",
    "agent:stub": "에이전트 (스텁)",
}


def render() -> None:
    st.title("스크리닝 실행")
    st.caption(
        "환자 한 명과 임상시험 한 건을 대조해 기준별 근거를 수집하고 적격성을 판정합니다."
    )

    _render_run_form()

    run_id = st.session_state.get(SS_RUN_ID)
    if not run_id:
        st.info("위에서 환자와 임상시험을 고르고 실행하세요.")
        return

    try:
        result = client().get_screening(str(run_id))
    except ApiError as exc:
        show_api_error(exc, context="실행 결과를 불러오지 못했습니다.")
        return

    _render_summary(result)
    _render_uncertainty(result)
    _render_packet(result.get("packet") or {})
    _render_explanations(result.get("explanations") or {})
    _render_questions(result)
    _render_trace(result)
    _render_limitations(result.get("limitations") or [])


# ---------------------------------------------------------------------------
# 실행 폼
# ---------------------------------------------------------------------------


def _render_run_form() -> None:
    try:
        trials = load_trials(settings.api_base_url)
        patients_page = load_patients(settings.api_base_url, 100, 0)
    except ApiError as exc:
        show_api_error(exc, context="목록을 불러오지 못했습니다.")
        return

    patients: list[dict[str, Any]] = list(patients_page.get("items") or [])

    with st.container(border=True):
        left, right = st.columns(2)
        with left:
            person_id = patient_selectbox(patients, key="screening_person")
        with right:
            trial_id = trial_selectbox(trials, key="screening_trial")

        st.text_input(
            "실행 주체 (actor)",
            value=actor_value(),
            key=SS_ACTOR,
            help="감사 로그에 기록됩니다.",
        )

        run_clicked = st.button(
            "스크리닝 실행", type="primary", disabled=not (person_id and trial_id)
        )

        with st.expander("run_id 로 기존 결과 불러오기"):
            manual_run_id = st.text_input("run_id", key="manual_run_id")
            if st.button("불러오기", disabled=not manual_run_id.strip()):
                set_active_run(manual_run_id.strip())
                st.rerun()

    if run_clicked and person_id and trial_id:
        with st.spinner("근거를 수집하고 판정하는 중입니다."):
            try:
                output = client().run_screening(
                    person_id=int(person_id),
                    trial_id=str(trial_id),
                    actor=actor_value(),
                )
            except ApiError as exc:
                show_api_error(exc, context="스크리닝 실행이 실패했습니다.")
                return
        set_active_run(
            str(output["run_id"]), person_id=int(person_id), trial_id=str(trial_id)
        )
        st.rerun()


# ---------------------------------------------------------------------------
# 결과 요약
# ---------------------------------------------------------------------------


def _render_summary(result: JsonDict) -> None:
    status = str(result.get("eligibility_status") or "")
    mode = str(result.get("mode") or "deterministic")

    st.subheader("판정 결과")
    st.markdown(
        f"{eligibility_badge(status)} **{result.get('decision_label', '')}**"
        f" · 판정 경로 {MODE_LABELS.get(mode, mode)}"
    )

    total = int(result.get("criteria_total") or 0)
    met = int(result.get("criteria_met") or 0)
    columns = st.columns(4)
    columns[0].metric("충족 기준", f"{met} / {total}")
    columns[1].metric("차단 기준", len(result.get("blocking_criteria") or []))
    columns[2].metric("미확인 기준", len(result.get("open_criteria") or []))
    columns[3].metric("검토 필요", len(result.get("review_criteria") or []))

    meta_rows = {
        "run_id": result.get("run_id"),
        "person_id": result.get("person_id"),
        "trial_id": result.get("trial_id"),
        "기준 버전": result.get("criteria_version"),
        "인덱스 방문": result.get("index_encounter_id"),
        "인덱스 일자": result.get("index_date"),
    }
    with st.expander("실행 메타데이터"):
        show_table([{"항목": key, "값": value} for key, value in meta_rows.items()])
        agent = result.get("agent") or {}
        if agent:
            st.markdown("**에이전트 상태**")
            st.json(agent, expanded=False)

    ticket_id = result.get("review_ticket_id")
    if ticket_id:
        st.warning(f"검토 티켓이 생성되었습니다: `{ticket_id}` — 검토 큐 화면에서 처리하세요.")


def _render_uncertainty(result: JsonDict) -> None:
    groups: list[tuple[str, list[str]]] = [
        ("차단 기준 (부적합 확정)", list(result.get("blocking_criteria") or [])),
        ("미확인 기준 (확인 질문 대상)", list(result.get("open_criteria") or [])),
        ("검토 필요 기준", list(result.get("review_criteria") or [])),
    ]
    active = [(title, items) for title, items in groups if items]
    if not active:
        return

    st.subheader("미해결 기준")
    for title, items in active:
        st.markdown(f"**{title}** — {', '.join(f'`{item}`' for item in items)}")


# ---------------------------------------------------------------------------
# 근거 패킷
# ---------------------------------------------------------------------------


def _render_packet(packet: JsonDict) -> None:
    items: JsonList = list(packet.get("items") or [])
    st.subheader("기준별 근거")
    if not items:
        st.caption("근거 항목이 없습니다.")
        return

    status_options = sorted({str(item.get("status") or "") for item in items})
    selected = st.multiselect(
        "상태 필터",
        options=status_options,
        default=status_options,
        format_func=lambda value: status_label(value, CRITERION_STATUS_META),
        key="packet_status_filter",
    )
    visible = [item for item in items if str(item.get("status")) in selected]

    show_table(
        [
            {
                "기준": item.get("criterion_id"),
                "구분": CRITERION_TYPE_LABELS.get(
                    str(item.get("criterion_type")), item.get("criterion_type")
                ),
                "항목": item.get("label"),
                "상태": status_label(
                    str(item.get("status")), CRITERION_STATUS_META
                ),
                "관측값": _with_unit(item.get("observed_value"), item.get("unit")),
                "기대 조건": item.get("expected_condition"),
                "신뢰도": item.get("confidence"),
                "관측 시점": item.get("observed_at"),
            }
            for item in visible
        ],
        empty_message="필터에 해당하는 기준이 없습니다.",
    )

    for item in visible:
        _render_packet_item(item)


def _render_packet_item(item: JsonDict) -> None:
    label = str(item.get("label") or item.get("criterion_id"))
    header = (
        f"{status_label(str(item.get('status')), CRITERION_STATUS_META)} · "
        f"{item.get('criterion_id')} — {label}"
    )
    with st.expander(header):
        st.markdown(
            f"{criterion_status_badge(str(item.get('status')))} "
            f"신뢰도 {item.get('confidence')} ({item.get('confidence_band')})"
        )
        st.markdown(f"**판정 근거** {item.get('explanation') or '설명이 없습니다.'}")

        detail = {
            "필드": item.get("field"),
            "종류": item.get("kind"),
            "관측값": _with_unit(item.get("observed_value"), item.get("unit")),
            "기대 조건": item.get("expected_condition"),
            "관측 시점": item.get("observed_at"),
        }
        show_table([{"항목": key, "값": value} for key, value in detail.items()])

        source_ids = list(item.get("source_ids") or [])
        if source_ids:
            st.caption("출처: " + ", ".join(f"`{sid}`" for sid in source_ids))
        else:
            st.caption("출처 ID 가 없습니다. 이 경우 모델 설명은 생성되지 않습니다.")

        conflicts = list(item.get("conflicts") or [])
        if conflicts:
            st.warning("기록 불일치: " + ", ".join(str(entry) for entry in conflicts))

        narrative: JsonList = list(item.get("narrative") or [])
        if narrative:
            st.markdown("**자유서술 근거**")
            for note in narrative:
                terms = ", ".join(note.get("matched_terms") or [])
                st.markdown(
                    f"- `{note.get('note_id')}` ({note.get('note_date')}, "
                    f"score {note.get('score')}) — {note.get('snippet')}"
                    + (f"  \n  일치어: {terms}" if terms else "")
                )


def _with_unit(value: Any, unit: Any) -> str:
    if value in (None, ""):
        return "—"
    if unit in (None, ""):
        return str(value)
    return f"{value} {unit}"


# ---------------------------------------------------------------------------
# 설명
# ---------------------------------------------------------------------------


def _render_explanations(explanations: dict[str, Any]) -> None:
    if not explanations:
        return

    st.subheader("대상별 설명")
    tab_keys = list(explanations)
    tabs = st.tabs([AUDIENCE_LABELS.get(key, key) for key in tab_keys])
    for tab, key in zip(tabs, tab_keys):
        payload = explanations[key] or {}
        with tab:
            if payload.get("blocked"):
                st.error("가드레일이 이 설명의 생성을 차단했습니다.")
            st.markdown(payload.get("summary") or "요약이 없습니다.")
            highlights = list(payload.get("highlights") or [])
            if highlights:
                for line in highlights:
                    st.markdown(f"- {line}")
            findings = list(payload.get("guardrail_findings") or [])
            if findings:
                with st.expander("가드레일 검출 내역"):
                    st.json(findings, expanded=False)


# ---------------------------------------------------------------------------
# 확인 질문 · 답변
# ---------------------------------------------------------------------------


def _render_questions(result: JsonDict) -> None:
    requests_list: JsonList = list(result.get("requests") or [])
    st.subheader("확인 질문")
    if not requests_list:
        st.caption("추가로 확인할 항목이 없습니다.")
        return

    st.caption("정보 가치가 높은 순서입니다. 순위는 규칙이 계산합니다.")
    show_table(
        [
            {
                "우선순위": entry.get("priority"),
                "대상": "참여자" if entry.get("target") == "participant" else "연구자",
                "기준": entry.get("criterion_id"),
                "질문": entry.get("question"),
                "정보 가치": entry.get("information_value"),
                "확인 부담": entry.get("effort"),
            }
            for entry in requests_list
        ]
    )

    person_id = int(result.get("person_id") or 0)
    run_id = str(result.get("run_id") or "")
    answerable = [
        entry for entry in requests_list if entry.get("target") == "participant"
    ]
    if not answerable:
        return

    st.markdown("**답변 제출**")
    for entry in answerable:
        request_id = str(entry.get("request_id") or entry.get("criterion_id"))
        with st.form(key=f"answer_form_{run_id}_{request_id}"):
            st.markdown(f"`{entry.get('criterion_id')}` {entry.get('question')}")
            if entry.get("reason"):
                st.caption(str(entry.get("reason")))
            value = st.text_input("답변", key=f"answer_value_{request_id}")
            submitted_by = st.text_input(
                "제출자", value="participant", key=f"answer_by_{request_id}"
            )
            if st.form_submit_button("제출"):
                if not value.strip():
                    st.warning("답변을 입력하세요.")
                else:
                    _submit_answer(
                        person_id=person_id,
                        run_id=run_id,
                        criterion_id=str(entry.get("criterion_id")),
                        value=value.strip(),
                        submitted_by=submitted_by.strip() or "participant",
                        request_id=str(entry.get("request_id") or "") or None,
                    )


def _submit_answer(
    *,
    person_id: int,
    run_id: str,
    criterion_id: str,
    value: str,
    submitted_by: str,
    request_id: str | None,
) -> None:
    try:
        answer = client().submit_answer(
            person_id=person_id,
            run_id=run_id,
            criterion_id=criterion_id,
            value=value,
            submitted_by=submitted_by,
            request_id=request_id,
        )
    except ApiError as exc:
        show_api_error(exc, context="답변 제출이 실패했습니다.")
        return
    st.success(f"답변을 저장했습니다. answer_id `{answer.get('answer_id')}`")


# ---------------------------------------------------------------------------
# 트레이스 · 제약
# ---------------------------------------------------------------------------


def _render_trace(result: JsonDict) -> None:
    trace = result.get("trace") or {}
    run_id = str(result.get("run_id") or "")

    with st.expander("실행 트레이스 · 감사 로그"):
        columns = st.columns(3)
        columns[0].metric("스팬 수", trace.get("span_count", 0))
        columns[1].metric("총 소요(ms)", round(float(trace.get("total_duration_ms") or 0), 1))
        columns[2].metric("오류", trace.get("error_count", 0))

        by_kind = trace.get("by_kind") or {}
        if by_kind:
            st.markdown("**종류별 집계**")
            st.json(by_kind, expanded=False)

        if st.button("상세 스팬 · 도구 호출 불러오기", key=f"trace_{run_id}"):
            try:
                detail = client().get_screening_trace(run_id)
            except ApiError as exc:
                show_api_error(exc, context="트레이스를 불러오지 못했습니다.")
            else:
                st.markdown("**도구 호출**")
                show_table(
                    [
                        {
                            "도구": call.get("tool_name"),
                            "권한": ", ".join(call.get("granted") or []),
                            "성공": call.get("ok"),
                            "상세": call.get("detail"),
                        }
                        for call in detail.get("tool_calls") or []
                    ]
                )
                st.markdown("**스팬**")
                st.json(detail.get("spans") or [], expanded=False)

        if st.button("감사 이벤트 불러오기", key=f"audit_{run_id}"):
            try:
                events = client().get_run_audit(run_id)
            except ApiError as exc:
                show_api_error(exc, context="감사 이벤트를 불러오지 못했습니다.")
            else:
                show_table(
                    [
                        {
                            "순번": event.get("sequence_no"),
                            "시각": event.get("occurred_at"),
                            "동작": event.get("action"),
                            "주체": event.get("actor"),
                            "기준": event.get("criterion_id"),
                        }
                        for event in events
                    ]
                )


def _render_limitations(limitations: list[str]) -> None:
    if not limitations:
        return
    st.divider()
    for line in limitations:
        st.caption(f"※ {line}")
