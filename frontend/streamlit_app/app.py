"""임상시험 스크리닝 콘솔 (Streamlit).

`backend/api` 의 FastAPI 오케스트레이터를 그대로 호출하는 운영자용 UI 다.
판정 로직은 전부 백엔드에 있고, 이 앱은 입력과 표시만 담당한다.

실행:
    streamlit run streamlit_app/app.py
"""

from __future__ import annotations

import streamlit as st

from api_client import ApiError
from config import settings
from views import audit, cohort, patients, review, screening, trials
from views.common import SS_RUN_ID, load_health

st.set_page_config(
    page_title="임상시험 스크리닝 콘솔",
    page_icon="🔬",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _render_sidebar() -> None:
    with st.sidebar:
        st.markdown("### 백엔드 연결")
        st.caption(f"`{settings.api_base_url}`")

        try:
            health = load_health(settings.api_base_url)
        except ApiError as exc:
            st.error("연결 실패")
            st.caption(exc.message)
            with st.expander("해결 방법"):
                st.markdown(
                    "1. 백엔드를 실행하세요.\n"
                    "```bash\n"
                    "backend/.venv/bin/python -m uvicorn app.main:app \\\n"
                    "  --app-dir backend/api --port 8000\n"
                    "```\n"
                    "2. `outputs/longitudinal_emr_v2/` 데이터셋이 있어야 서버가 기동합니다. "
                    "다른 경로면 `EMR_DATA_DIR` 을 설정하세요.\n"
                    "3. 주소가 다르면 `SCREENING_API_BASE_URL` 을 설정하세요."
                )
        else:
            st.success(f"연결됨 · v{health.get('version', '?')}")
            counts = health.get("data_counts") or {}
            st.caption(
                f"환자 {counts.get('patients', 0)} · 방문 {counts.get('encounters', 0)}"
                f" · 시험 {counts.get('trials', 0)} · 기준 {counts.get('criteria', 0)}"
            )
            st.caption(
                f"RAG {health.get('rag_status', '?')} · Graph {health.get('graph_status', '?')}"
            )

        st.divider()
        run_id = st.session_state.get(SS_RUN_ID)
        if run_id:
            st.markdown("### 현재 실행")
            st.code(str(run_id), language=None)
            if st.button("선택 해제", width="stretch"):
                st.session_state.pop(SS_RUN_ID, None)
                st.rerun()

        st.divider()
        if st.button("캐시 비우기", width="stretch"):
            st.cache_data.clear()
            st.rerun()
        st.caption("합성 데이터 기반 사전 스크리닝이며 의료적 판단이 아닙니다.")


def main() -> None:
    _render_sidebar()

    # 모든 화면의 진입 함수 이름이 render 라서 URL 경로가 충돌한다.
    # st.navigation 은 콜러블 이름으로 경로를 유추하므로 명시적으로 지정한다.
    navigation = st.navigation(
        [
            st.Page(
                screening.render,
                title="스크리닝 실행",
                icon=":material/biotech:",
                url_path="screening",
                default=True,
            ),
            st.Page(
                cohort.render,
                title="코호트 현황",
                icon=":material/groups:",
                url_path="cohort",
            ),
            st.Page(
                review.render,
                title="검토 큐",
                icon=":material/fact_check:",
                url_path="review",
            ),
            st.Page(
                patients.render,
                title="환자",
                icon=":material/personal_injury:",
                url_path="patients",
            ),
            st.Page(
                trials.render,
                title="임상시험",
                icon=":material/science:",
                url_path="trials",
            ),
            st.Page(
                audit.render,
                title="감사 · 아키텍처",
                icon=":material/receipt_long:",
                url_path="audit",
            ),
        ]
    )
    navigation.run()


main()
