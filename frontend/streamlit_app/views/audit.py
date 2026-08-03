"""감사 로그 · 아키텍처 화면.

판정 이력을 조회하고, 등록된 Tool 권한과 에이전트 상태를 확인한다.
"""

from __future__ import annotations

import streamlit as st

from api_client import ApiError, JsonList
from config import settings
from views.common import client, load_trials, show_api_error, show_table

AGENT_MODE_LABELS: dict[str, str] = {
    "deterministic": "규칙 기반",
    "bedrock": "Bedrock FM",
    "stub": "결정론적 스텁",
}


def render() -> None:
    st.title("감사 로그 · 아키텍처")
    st.caption("감사 로그는 append-only 입니다. 상태가 바뀌면 이벤트가 추가됩니다.")

    audit_tab, architecture_tab = st.tabs(["감사 로그", "아키텍처"])
    with audit_tab:
        _render_audit()
    with architecture_tab:
        _render_architecture()


def _render_audit() -> None:
    try:
        trials = load_trials(settings.api_base_url)
    except ApiError as exc:
        show_api_error(exc, context="임상시험 목록을 불러오지 못했습니다.")
        trials = []

    trial_ids = [str(trial["trial_id"]) for trial in trials]

    with st.container(border=True):
        by_run = st.toggle("run_id 로 조회", key="audit_by_run")
        if by_run:
            run_id = st.text_input("run_id", key="audit_run_id")
            if not run_id.strip():
                st.caption("run_id 를 입력하세요.")
                return
            try:
                events: JsonList = client().get_run_audit(run_id.strip())
            except ApiError as exc:
                show_api_error(exc, context="감사 이벤트를 불러오지 못했습니다.")
                return
        else:
            columns = st.columns(3)
            with columns[0]:
                person_raw = st.text_input("person_id", key="audit_person_id")
            with columns[1]:
                trial_id = st.selectbox(
                    "임상시험", options=("전체", *trial_ids), key="audit_trial_id"
                )
            with columns[2]:
                limit = int(
                    st.number_input("건수", 1, 500, 100, 10, key="audit_limit")
                )

            person_id: int | None = None
            if person_raw.strip():
                if not person_raw.strip().isdigit():
                    st.warning("person_id 는 숫자여야 합니다.")
                    return
                person_id = int(person_raw.strip())

            try:
                events = client().query_audit(
                    person_id=person_id,
                    trial_id=None if trial_id == "전체" else str(trial_id),
                    limit=limit,
                )
            except ApiError as exc:
                show_api_error(exc, context="감사 로그를 조회하지 못했습니다.")
                return

    st.metric("이벤트", len(events))
    show_table(
        [
            {
                "순번": event.get("sequence_no"),
                "시각": event.get("occurred_at"),
                "동작": event.get("action"),
                "주체": event.get("actor"),
                "person": event.get("person_id"),
                "trial": event.get("trial_id"),
                "기준": event.get("criterion_id"),
                "run_id": event.get("run_id"),
            }
            for event in events
        ],
        empty_message="조건에 해당하는 이벤트가 없습니다.",
    )

    if events:
        with st.expander("이벤트 상세 (detail)"):
            st.json(
                [
                    {
                        "event_id": event.get("event_id"),
                        "action": event.get("action"),
                        "detail": event.get("detail"),
                    }
                    for event in events
                ],
                expanded=False,
            )


def _render_architecture() -> None:
    try:
        payload = client().architecture()
    except ApiError as exc:
        show_api_error(exc, context="아키텍처 정보를 불러오지 못했습니다.")
        return

    agent = payload.get("agent") or {}
    mode = str(agent.get("mode") or "")

    st.subheader("에이전트")
    columns = st.columns(3)
    columns[0].metric("활성", "예" if agent.get("enabled") else "아니오")
    columns[1].metric("모드", AGENT_MODE_LABELS.get(mode, mode or "—"))
    columns[2].metric("루프 상한", agent.get("max_iterations", "—"))

    st.caption(
        f"모델 {agent.get('model_id') or '—'} · 리전 {agent.get('region') or '—'} · "
        f"가드레일 {'연결' if agent.get('guardrail_attached') else '미연결'}"
    )
    if agent.get("fallback_reason"):
        st.warning(f"폴백 사유: {agent.get('fallback_reason')}")

    exposed = list(agent.get("exposed_tools") or [])
    if exposed:
        st.caption("모델에 노출된 도구: " + ", ".join(f"`{name}`" for name in exposed))

    managed = list(agent.get("managed_agents") or [])
    if managed:
        st.markdown("**등록된 에이전트**")
        show_table(
            [
                {
                    "이름": item.get("name"),
                    "역할": item.get("role"),
                    "활성": item.get("enabled"),
                    "모드": item.get("mode"),
                    "모델 사용": item.get("model_backed"),
                    "노출 도구": ", ".join(item.get("exposed_tools") or []),
                }
                for item in managed
            ]
        )

    st.subheader("Tool 권한")
    st.caption("선언하지 않은 권한은 Gateway 등록 단계에서 거부됩니다.")
    show_table(
        [
            {
                "도구": entry.get("tool_name"),
                "권한": ", ".join(entry.get("permissions") or []) or "없음 (계산 전용)",
            }
            for entry in payload.get("tools") or []
        ]
    )

    st.subheader("저장소")
    stores = payload.get("stores") or {}
    show_table([{"저장소": key, "건수": value} for key, value in stores.items()])
    st.metric("감사 이벤트 총계", payload.get("audit_events", 0))
