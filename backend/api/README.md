# 임상시험 스크리닝 백엔드 v0.3

9계층 스크리닝 오케스트레이터입니다. 기준별로 근거를 수집·검증한 뒤 다섯 가지 상태로
판정하고, 근거 패킷·확인 질문·대상별 설명을 함께 반환합니다.

- v0.1 — 데이터셋에 저장된 판정 스냅샷을 그대로 반환
- v0.2 — 판정을 직접 계산하는 결정론적 오케스트레이터
- v0.3 — Bedrock FM 에이전트 계층 추가 (근거 수집 판단, NLI 검증, 설명·질문 생성)

AWS 서비스 연결 전이므로 Knowledge Bases / Neptune / DynamoDB 자리는 로컬 어댑터가
동일한 계약으로 채우고 있습니다.

> Bedrock 실제 호출은 이 환경에서 검증하지 못했습니다. Converse API 요청 형식과
> 응답 파싱은 가짜 클라이언트로 테스트했고, 자격 증명이 없으면 결정론적 스텁으로
> 내려앉습니다.

## 위치

이 서비스는 `backend/api/` 에 있습니다. 같은 저장소의 `backend/` 최상단에는 CDK 인프라
코드(`app.py`, `infra/`, `lambdas/`, `step_functions/`)가 별도로 있으며, 서로 의존하지
않습니다. 데이터 파이프라인이 만든 결과를 이 API가 읽는 관계입니다.

## 실행

저장소 루트에서 실행합니다.

```powershell
python -m venv backend/.venv
backend/.venv/Scripts/python -m pip install -r backend/api/requirements.txt
backend/.venv/Scripts/python -m uvicorn app.main:app --app-dir backend/api --reload --port 8000
```

- Swagger: `http://127.0.0.1:8000/docs`
- 상태 확인: `http://127.0.0.1:8000/health`

`python -m uvicorn` 을 저장소 루트에서 실행해야 합니다. `-m` 이 루트를 import 경로에
넣어 `agent` 패키지를 찾고, `--app-dir` 이 `backend/api` 를 넣어 `app` 패키지를 찾습니다.

테스트:

```powershell
cd backend/api
../.venv/Scripts/python -m pytest tests -q
```

경로 설정은 `backend/api/conftest.py` 가 처리하므로 어느 디렉터리에서 띄워도 됩니다.

데이터 경로는 기본값이 저장소 루트의 `outputs/longitudinal_emr_v2/` 이며,
`EMR_DATA_DIR` 환경 변수로 바꿀 수 있습니다.

## 계층 구조

| 계층 | 모듈 | 역할 |
|---|---|---|
| 2. API 진입 | `app/main.py` | 요청 검증, 응답 조립 |
| 3. 오케스트레이션 | `app/orchestration/` | Runtime, Criterion Router, Tool Gateway |
| 4. 전문 Tool | `app/tools/` | Criteria, Evidence Retrieval, Timeline Graph, Rule Evaluator |
| 5. 데이터 | `app/repository.py` | 로컬 CSV/JSONL 어댑터 |
| 6. 근거 검증·취합 | `app/reasoning/` | Evidence Bundle, Verifier, Deterministic Aggregator |
| 7. 상태 모델 | `app/domain/states.py` | 기준별 5가지 상태 |
| 8. 결과 생성 | `app/actions/` | Cohort Selector, Evidence Packet, Next-Best-Evidence, Explanation |
| 9. 저장·응답 | `app/persistence/` | Run Store, 감사 로그 |
| 횡단 | `app/safety/` | Guardrails, Observability |
| 에이전트 | `agent/` (저장소 루트) | 모델 클라이언트, 도구 스키마, tool-use 루프, FM 검증·설명 |

조립은 `app/container.py` 한 곳에서 이뤄집니다. AWS 어댑터로 바꿀 때 이 파일만 수정하면 됩니다.

에이전트 계층은 이 패키지 밖(`agent/`)에 있고 `app` 을 import 하지 않습니다.
필요한 동작은 `agent/contracts.py` 의 Protocol 로 선언되어 있고, `app/container.py`
가 그 계약을 만족하는 구현(폴백 검증기·설명 생성기·Guardrail·Tracer·Gateway)을
주입합니다. 의존성은 `app → agent` 한 방향입니다. 자세한 내용은
[agent/README.md](../../agent/README.md) 를 참고하세요.

## 에이전트 계층

### FM에게 맡기는 것과 맡기지 않는 것

재현성을 지키기 위해 역할을 나눴습니다.

| 담당 | 주체 |
|---|---|
| 어떤 근거를 더 모을지 판단 | **FM** (tool-use 루프) |
| 근거와 조건의 관계 판정 (NLI) | **FM** (제안만) |
| 설명 문장 생성 | **FM** |
| 확인 질문 문장 작성 | **FM** |
| 수치·기간 계산 | 규칙 (`RuleEvaluator`) |
| **기준별 상태 확정** | 규칙 (`DeterministicAggregator`) |
| **종합 적격성 판정** | 규칙 (`DeterministicAggregator`) |
| 질문 우선순위·정보 가치 | 규칙 (`NextBestEvidenceAgent`) |

FM은 적격성을 결정하지 않습니다. 판정을 모델에 맡기면 같은 환자가 실행할 때마다 다른
결과를 받을 수 있고, 그 판정을 감사로 방어하기 어렵습니다.

질문 우선순위도 규칙이 정합니다. 순위가 호출마다 흔들리면 검토 큐 순서가 불안정해집니다.
모델은 표현만 담당합니다.

### 모델이 볼 수 있는 도구

| 도구 | 노출 |
|---|---|
| `evidence_retrieval_tool` | O |
| `timeline_graph_tool` | O |
| `criteria_tool` | X |
| `rule_evaluator` | X |

판정과 저장에 닿는 도구는 노출하지 않습니다. 모델이 호출을 요청해도 화이트리스트에서
걸러지고, 통과한 호출도 Gateway 권한 검사를 다시 받습니다.

### 모델이 규칙과 다른 결론을 내면

`REVIEW_REQUIRED` 로 올리고 사람에게 넘깁니다. 어느 한쪽을 조용히 채택하지 않습니다.
신뢰도가 0.55 아래인 긍정·부정 판정도 검토로 돌립니다.

### 폴백

모델 호출 실패, 형식에 맞지 않는 응답, 알 수 없는 판정값은 모두 규칙 판정으로 내려앉고
그 사실을 `notes` 에 남깁니다. 판정이 조용히 비는 상황을 만들지 않습니다.

출처 ID가 없으면 모델을 아예 호출하지 않습니다 (Contextual Grounding 선결 조건).

### 설정

기본은 비활성입니다. 자격 증명 없는 환경에서도 동작해야 하므로 명시적으로 켭니다.

| 환경 변수 | 기본값 | 설명 |
|---|---|---|
| `BEDROCK_ENABLED` | `false` | 에이전트 모드 활성화 |
| `AWS_REGION` | `us-east-1` | Bedrock 리전 |
| `BEDROCK_MODEL_ID` | `anthropic.claude-3-5-sonnet-20241022-v2:0` | 모델 |
| `BEDROCK_MAX_TOKENS` | `1024` | 응답 상한 |
| `AGENT_MAX_ITERATIONS` | `4` | tool-use 루프 상한 |
| `BEDROCK_GUARDRAIL_ID` | 없음 | Bedrock Guardrails 연결 |

`temperature` 는 0으로 고정되어 있습니다.

Bedrock을 켰지만 클라이언트 생성이 실패하면 스텁으로 내려앉고, 이유가
`/api/v1/architecture` 의 `agent.fallback_reason` 에 남습니다.

실행 응답의 `mode` 필드로 어느 경로를 탔는지 확인할 수 있습니다
(`deterministic` / `agent:bedrock` / `agent:stub`).

## 판정 상태

기준 한 건은 다섯 가지 중 하나로 확정됩니다.

| 상태 | 의미 | 후속 처리 |
|---|---|---|
| `EVIDENCE_FOUND` | 근거 확인, 조건 충족 | 코호트 반영 |
| `CONTRADICTED` | 근거 확인, 조건 충돌 | 종합 판정을 부적합으로 확정 |
| `UNKNOWN` | 관찰값 없음 | 확인 질문 생성 |
| `CONFLICTING` | 기록 간 값이 어긋남 | 검토 큐 + 확인 질문 |
| `REVIEW_REQUIRED` | 자동 판정 신뢰도 낮음 | 검토 큐 |

종합 판정 우선순위는 `INELIGIBLE > REVIEW_REQUIRED > NEEDS_MORE_EVIDENCE > ELIGIBLE` 입니다.

### 재현성

상태 확정은 `DeterministicAggregator` 가 담당하며 FM을 호출하지 않습니다. Verifier는 상태를
'제안'만 하고 확정하지 않습니다. Bedrock FM이 붙어도 동일 입력에 동일 판정이 나오도록
설계한 분리입니다. 기준 버전(`criteria_version`)은 기준 내용 해시로 산출되므로, 기준이
바뀌면 재현 조건이 달라진 것을 식별할 수 있습니다.

## 주요 API

### 스크리닝

| Method | Path | 설명 |
|---|---|---|
| `POST` | `/api/v1/screening/run` | 환자 x 시험 한 건 실행 |
| `GET` | `/api/v1/screening/{run_id}` | 실행 결과 재조회 |
| `GET` | `/api/v1/screening/{run_id}/evidence` | 기준별 근거 패킷 |
| `GET` | `/api/v1/screening/{run_id}/trace` | Agent·Tool·Model 스팬 |

요청 예시:

```json
{ "person_id": 3, "trial_id": "SYN-T2D-INTENSIFY-01" }
```

### 코호트

| Method | Path | 설명 |
|---|---|---|
| `POST` | `/api/v1/cohort/run` | 배치 실행 후 퍼널·병목 집계 |
| `GET` | `/api/v1/cohort/{trial_id}` | 기존 실행 결과로 현황 조회 |

### 참여자

| Method | Path | 설명 |
|---|---|---|
| `GET` | `/api/v1/patients/{person_id}/questions` | 확인 질문 (정보 가치 순) |
| `POST` | `/api/v1/patients/{person_id}/answers` | 답변 제출 |

### 검토·감사

| Method | Path | 설명 |
|---|---|---|
| `GET` | `/api/v1/review-queue` | 검토 대기 목록 |
| `PATCH` | `/api/v1/review-queue/{ticket_id}` | 승인·반려·재실행 |
| `GET` | `/api/v1/audit/{run_id}` | 실행별 판정 이력 |
| `GET` | `/api/v1/audit` | 조건 검색 |
| `GET` | `/api/v1/architecture` | 등록 Tool과 유효 권한 |

### 조회

`/api/v1/patients`, `/api/v1/patients/{id}`, `/api/v1/patients/{id}/timeline`,
`/api/v1/trials`, `/api/v1/trials/{id}` 는 v0.1과 동일합니다.

`POST /api/v1/screenings` (v0.1 스냅샷 판정)는 호환을 위해 유지하되 deprecated 입니다.

## 권한 통제

각 Tool은 접근할 저장소와 동작을 선언하고, Gateway가 그 선언을 근거로 호출을 허용합니다.
선언하지 않은 권한을 정책에서 허용하려 하면 등록 단계에서 거부됩니다.

| Tool | 권한 |
|---|---|
| `criteria_tool` | `READ:dynamodb:trial_definitions` |
| `evidence_retrieval_tool` | `READ:bedrock:knowledge_base` |
| `timeline_graph_tool` | `READ:neptune:patient_timeline` |
| `rule_evaluator` | 없음 (계산 전용) |

Rule Evaluator는 다른 Tool의 출력을 입력으로 받지만, 직접 호출되지 않고 Gateway를 경유합니다.
권한 검사와 호출 로깅이 빠지지 않도록 한 조치입니다.

## 안전 장치

- **Guardrails** — 의료적 확정 표현을 완화하고 위험 표현을 차단합니다. 출처 ID가 없는
  설명은 생성하지 않습니다 (Contextual Grounding).
- **감사 로그** — append-only 입니다. 상태가 바뀌면 기존 이벤트를 수정하지 않고 새 이벤트를
  추가합니다. `export_ndjson()` 은 Firehose 페이로드와 동일한 형태입니다.
- **Observability** — 모든 Agent·Tool·Model 호출이 스팬으로 기록되고 지연시간이 집계됩니다.

## 알려진 제약

- 판정은 인덱스 방문(최신 방문) 시점의 관찰값을 기준으로 계산됩니다.
- **Bedrock 실제 호출은 검증되지 않았습니다.** 요청 형식과 응답 파싱만 테스트했습니다.
  자격 증명이 있는 환경에서 확인이 필요합니다.
- 자유서술 검색은 벡터 검색이 아닌 키워드 일치입니다. Knowledge Bases 연결 시
  검색 품질이 달라지므로 파생 불리언 판정을 다시 확인해야 합니다.
- 데이터셋 자체에 나이 이상치가 있습니다 (예: person 3의 최신 방문 age=107).
  엔진은 이를 `CONTRADICTED` 로 정상 판정하지만, 데이터 품질 검증 레이어에서
  걸러야 할 사안입니다.

결과는 합성 데이터 기반의 사전 스크리닝이며 실제 의료적 판단이나 최종 등록 결정이 아닙니다.

## AWS 연결 지점

| 교체 대상 | 현재 | 목표 |
|---|---|---|
| `app/tools/criteria_tool.py` | CSV | DynamoDB Criteria Store |
| `app/tools/evidence_retrieval.py` | 키워드 검색 | Bedrock KB + OpenSearch |
| `app/tools/timeline_graph.py` | CSV | Amazon Neptune |
| `agent/model.py` | 스텁 | Bedrock Converse (구현 완료, 미검증) |
| `app/safety/guardrails.py` | 정규식 | Bedrock Guardrails (연결부 구현) |
| `app/persistence/run_store.py` | 메모리 | DynamoDB |
| `app/persistence/audit.py` | 메모리 | DDB Streams → Firehose → S3 |
| `app/safety/observability.py` | 메모리 | AgentCore Observability → CloudWatch |
