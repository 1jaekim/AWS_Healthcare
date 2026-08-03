# 스크리닝 콘솔 (Streamlit)

`backend/api` 의 FastAPI 오케스트레이터를 호출하는 운영자용 UI 입니다.
판정 로직은 전부 백엔드에 있고, 이 앱은 **입력과 표시만** 담당합니다.

React + Amplify 프론트는 `frontend/` 에서 따로 진행합니다. 이 앱은 그와 독립입니다.

## 실행

백엔드가 먼저 떠 있어야 합니다.

```bash
# 1) 백엔드
backend/.venv/bin/python -m uvicorn app.main:app \
  --app-dir backend/api --reload --port 8000

# 2) UI
backend/.venv/bin/python -m streamlit run streamlit_app/app.py
```

`http://localhost:8501` 로 접속합니다.

의존성만 따로 설치하려면:

```bash
pip install -r streamlit_app/requirements.txt
```

## 환경 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `SCREENING_API_BASE_URL` | `http://127.0.0.1:8000` | 백엔드 주소 |
| `SCREENING_API_TIMEOUT` | `60` | 요청 타임아웃(초) |
| `SCREENING_ACTOR` | `streamlit-ui` | 감사 로그에 남길 기본 실행 주체 |
| `SCREENING_CACHE_TTL` | `60` | 목록 조회 캐시 유지(초) |

백엔드는 `outputs/longitudinal_emr_v2/` 데이터셋을 기동 시 읽습니다. 없으면 서버가
기동 단계에서 실패합니다. 다른 경로면 백엔드에 `EMR_DATA_DIR` 을 지정하세요.

## 화면

| 화면 | 경로 | 하는 일 |
|---|---|---|
| 스크리닝 실행 | `/screening` | 환자 x 시험 실행, 기준별 근거·설명·확인질문·트레이스 |
| 코호트 현황 | `/cohort` | 배치 실행, 판정 퍼널, 병목 기준, 검토 우선순위 |
| 검토 큐 | `/review` | 검토 대기건 승인·반려·재실행 요청 |
| 환자 | `/patients` | 환자 목록, 방문 타임라인, 측정값 추이, 자유서술 기록 |
| 임상시험 | `/trials` | 시험 목록, 선정·제외 기준 |
| 감사 · 아키텍처 | `/audit` | 감사 로그 조회, Tool 권한, 에이전트 상태 |

`run_id` 는 `st.session_state` 에 보관되어 화면 간에 이어집니다. 코호트나 검토 큐에서
`근거 열기` 를 누르면 스크리닝 실행 화면이 그 실행을 표시합니다.

## 구조

```
streamlit_app/
├── app.py            내비게이션, 사이드바(연결 상태)
├── config.py         환경 변수 설정
├── api_client.py     백엔드 HTTP 클라이언트 (엔드포인트와 1:1)
└── views/
    ├── common.py     상태 라벨, 캐시 로더, 표·선택 위젯, 오류 표시
    ├── screening.py
    ├── cohort.py
    ├── review.py
    ├── patients.py
    ├── trials.py
    └── audit.py
```

HTTP 세부사항은 `api_client.py` 에만 있습니다. 화면 코드는 URL 을 직접 다루지 않습니다.

## 프론트가 백엔드에 보내는 값

이 앱이 새로 입력받는 값은 사실상 `person_id` 와 `trial_id` 뿐입니다. 나머지는 GET 으로
받은 목록에서 선택합니다. 환자 EMR 을 UI 에서 입력하는 구조가 아닙니다.

| 동작 | 필수 입력 |
|---|---|
| 스크리닝 실행 | `person_id`, `trial_id` (+ `actor`) |
| 코호트 실행 | `trial_id` (+ `person_ids` 또는 `limit`) |
| 답변 제출 | `run_id`, `criterion_id`, `value` |
| 검토 처리 | `ticket_id`, `decision`, `decided_by` |

## 판정 상태 표기

| 기준 상태 | 표기 |
|---|---|
| `EVIDENCE_FOUND` | 충족 |
| `CONTRADICTED` | 조건 충돌 |
| `UNKNOWN` | 정보 없음 |
| `CONFLICTING` | 기록 불일치 |
| `REVIEW_REQUIRED` | 검토 필요 |

제외(EXCLUSION) 기준은 백엔드 규약상 **'위험 없음'** 조건으로 기술됩니다
(예: `active_pregnancy = false`). 따라서 규칙 충족이 곧 통과이고, 상태 매핑은
선정 기준과 동일합니다.

## 주의

- **인증이 없습니다.** 환자 기록을 그대로 표시하므로 로컬 개발용으로만 쓰세요.
  공유 네트워크에 노출하려면 인증을 먼저 붙여야 합니다. Streamlit 기본 실행은
  LAN 에 열립니다. 외부 접근을 막으려면 `--server.address 127.0.0.1` 을 붙이세요.
- 합성 데이터 기반 사전 스크리닝이며 의료적 판단이 아닙니다.
