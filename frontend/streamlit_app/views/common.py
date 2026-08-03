"""화면 전반에서 재사용하는 헬퍼.

상태 라벨, 캐시된 데이터 로더, 오류 표시, 선택 위젯을 모아둔다.
"""

from __future__ import annotations

from typing import Any, Final

import pandas as pd
import streamlit as st

from api_client import ApiError, JsonDict, JsonList, ScreeningApiClient
from config import settings

# ---------------------------------------------------------------------------
# 세션 상태 키
# ---------------------------------------------------------------------------

SS_RUN_ID: Final[str] = "active_run_id"
SS_PERSON_ID: Final[str] = "active_person_id"
SS_TRIAL_ID: Final[str] = "active_trial_id"
SS_ACTOR: Final[str] = "actor"

# ---------------------------------------------------------------------------
# 상태 표시 메타데이터
# ---------------------------------------------------------------------------

# (한글 라벨, 배지 색상) — 색상은 Streamlit 배지 마크다운에서 지원하는 값만 쓴다.
CRITERION_STATUS_META: Final[dict[str, tuple[str, str]]] = {
    "EVIDENCE_FOUND": ("충족", "green"),
    "CONTRADICTED": ("조건 충돌", "red"),
    "UNKNOWN": ("정보 없음", "gray"),
    "CONFLICTING": ("기록 불일치", "orange"),
    "REVIEW_REQUIRED": ("검토 필요", "violet"),
}

ELIGIBILITY_META: Final[dict[str, tuple[str, str]]] = {
    "ELIGIBLE": ("사전 적합", "green"),
    "INELIGIBLE": ("사전 부적합", "red"),
    "NEEDS_MORE_EVIDENCE": ("추가 정보 필요", "orange"),
    "REVIEW_REQUIRED": ("검토 필요", "violet"),
}

TICKET_STATUS_META: Final[dict[str, tuple[str, str]]] = {
    "PENDING": ("검토 대기", "orange"),
    "APPROVED": ("승인", "green"),
    "REJECTED": ("반려", "red"),
    "RERUN_REQUESTED": ("재실행 요청", "violet"),
}

DECISION_LABELS: Final[dict[str, str]] = {
    "APPROVED": "승인",
    "REJECTED": "반려",
    "RERUN_REQUESTED": "재실행 요청",
}

CRITERION_TYPE_LABELS: Final[dict[str, str]] = {
    "INCLUSION": "선정",
    "EXCLUSION": "제외",
}


def badge(value: str | None, meta: dict[str, tuple[str, str]]) -> str:
    """상태 문자열을 색상 배지 마크다운으로 바꾼다."""
    if not value:
        return ":gray-badge[미정]"
    label, color = meta.get(value, (value, "gray"))
    return f":{color}-badge[{label}]"


def criterion_status_badge(status: str | None) -> str:
    return badge(status, CRITERION_STATUS_META)


def eligibility_badge(status: str | None) -> str:
    return badge(status, ELIGIBILITY_META)


def ticket_status_badge(status: str | None) -> str:
    return badge(status, TICKET_STATUS_META)


def status_label(value: str | None, meta: dict[str, tuple[str, str]]) -> str:
    """배지 없이 한글 라벨만 필요할 때 (표 안에서 사용)."""
    if not value:
        return "미정"
    return meta.get(value, (value, ""))[0]


# ---------------------------------------------------------------------------
# 클라이언트 · 캐시된 로더
# ---------------------------------------------------------------------------


@st.cache_resource(show_spinner=False)
def get_client(base_url: str, timeout: float) -> ScreeningApiClient:
    """base_url 당 하나의 세션을 재사용한다."""
    return ScreeningApiClient(base_url, timeout=timeout)


def client() -> ScreeningApiClient:
    return get_client(settings.api_base_url, settings.request_timeout)


@st.cache_data(ttl=settings.cache_ttl_seconds, show_spinner=False)
def load_health(base_url: str) -> JsonDict:
    return get_client(base_url, settings.request_timeout).health()


@st.cache_data(ttl=settings.cache_ttl_seconds, show_spinner=False)
def load_trials(base_url: str) -> JsonList:
    return get_client(base_url, settings.request_timeout).list_trials()


@st.cache_data(ttl=settings.cache_ttl_seconds, show_spinner=False)
def load_patients(base_url: str, limit: int, offset: int) -> JsonDict:
    return get_client(base_url, settings.request_timeout).list_patients(
        limit=limit, offset=offset
    )


@st.cache_data(ttl=settings.cache_ttl_seconds, show_spinner=False)
def load_timeline(base_url: str, person_id: int) -> JsonDict:
    return get_client(base_url, settings.request_timeout).get_patient_timeline(
        person_id
    )


@st.cache_data(ttl=settings.cache_ttl_seconds, show_spinner=False)
def load_trial_detail(base_url: str, trial_id: str) -> JsonDict:
    return get_client(base_url, settings.request_timeout).get_trial(trial_id)


def clear_caches() -> None:
    """실행 후 목록을 다시 읽도록 캐시를 비운다."""
    st.cache_data.clear()


# ---------------------------------------------------------------------------
# 오류 표시
# ---------------------------------------------------------------------------

_SETUP_HINT = """
백엔드가 실행 중인지 확인하세요.

```bash
backend/.venv/bin/python -m uvicorn app.main:app \\
  --app-dir backend/api --reload --port 8000
```

`outputs/longitudinal_emr_v2/` 데이터셋이 없으면 서버가 기동 단계에서 실패합니다.
다른 위치에 있다면 `EMR_DATA_DIR` 로 지정하세요.
다른 주소를 쓴다면 `SCREENING_API_BASE_URL` 을 설정하세요.
"""


def show_api_error(exc: ApiError, *, context: str = "") -> None:
    """ApiError 를 화면에 표시한다. 연결 실패면 설정 안내를 덧붙인다."""
    prefix = f"{context} " if context else ""
    st.error(f"{prefix}{exc.message}")
    if exc.unreachable:
        st.info(_SETUP_HINT)


# ---------------------------------------------------------------------------
# 표 · 선택 위젯
# ---------------------------------------------------------------------------


def _arrow_safe(frame: pd.DataFrame) -> pd.DataFrame:
    """Arrow 로 직렬화되지 않는 혼합 타입 컬럼을 문자열로 정규화한다.

    항목/값 형태의 표는 한 컬럼에 문자열과 숫자가 섞인다. 그대로 넘기면 pyarrow
    변환이 실패하고 Streamlit 이 traceback 을 남긴 뒤 자동 보정한다. 미리 맞춘다.
    """
    for column in frame.columns:
        if frame[column].dtype != object:
            continue
        kinds = {type(value) for value in frame[column] if value is not None}
        if len(kinds) > 1:
            frame[column] = frame[column].map(
                lambda value: "" if value is None else str(value)
            )
    return frame


def show_table(
    rows: list[dict[str, Any]], *, empty_message: str = "표시할 항목이 없습니다."
) -> None:
    """딕셔너리 목록을 표로 그린다. 비어 있으면 안내 문구를 보여준다."""
    if not rows:
        st.caption(empty_message)
        return
    st.dataframe(
        _arrow_safe(pd.DataFrame(rows)), width="stretch", hide_index=True
    )


def trial_selectbox(
    trials: JsonList, *, key: str, label: str = "임상시험"
) -> str | None:
    """trial_id 를 고르는 selectbox. 세션에 저장된 값을 기본 선택한다."""
    if not trials:
        st.warning("등록된 임상시험이 없습니다.")
        return None

    ids = [str(trial["trial_id"]) for trial in trials]
    names = {
        str(trial["trial_id"]): str(trial.get("trial_name", trial["trial_id"]))
        for trial in trials
    }
    saved = st.session_state.get(SS_TRIAL_ID)
    index = ids.index(saved) if saved in ids else 0

    selected = st.selectbox(
        label,
        options=ids,
        index=index,
        format_func=lambda value: f"{names.get(value, value)} ({value})",
        key=key,
    )
    st.session_state[SS_TRIAL_ID] = selected
    return selected


def patient_selectbox(
    patients: list[dict[str, Any]], *, key: str, label: str = "환자"
) -> int | None:
    """person_id 를 고르는 selectbox."""
    if not patients:
        st.warning("환자 데이터가 없습니다.")
        return None

    ids = [int(patient["person_id"]) for patient in patients]
    detail = {
        int(patient["person_id"]): (
            f"person {patient['person_id']} · {patient.get('sex', '?')} · "
            f"방문 {patient.get('encounter_count', 0)}건"
        )
        for patient in patients
    }
    saved = st.session_state.get(SS_PERSON_ID)
    index = ids.index(saved) if saved in ids else 0

    selected = st.selectbox(
        label,
        options=ids,
        index=index,
        format_func=lambda value: detail.get(value, str(value)),
        key=key,
    )
    st.session_state[SS_PERSON_ID] = int(selected)
    return int(selected)


def actor_value() -> str:
    """감사 로그에 남길 실행 주체."""
    return str(st.session_state.get(SS_ACTOR) or settings.default_actor)


def set_active_run(run_id: str, person_id: int | None = None, trial_id: str | None = None) -> None:
    """다른 화면이 이어받을 수 있도록 실행 컨텍스트를 저장한다."""
    st.session_state[SS_RUN_ID] = run_id
    if person_id is not None:
        st.session_state[SS_PERSON_ID] = int(person_id)
    if trial_id is not None:
        st.session_state[SS_TRIAL_ID] = trial_id
