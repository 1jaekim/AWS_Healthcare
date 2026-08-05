# 임상시험 스크리닝 백엔드 v0.3

9계층 스크리닝 오케스트레이터입니다. 기준별로 근거를 수집·검증한 뒤 다섯 가지 상태로
판정하고, 근거 패킷·확인 질문·대상별 설명을 함께 반환합니다.

- v0.1 — 데이터셋에 저장된 판정 스냅샷을 그대로 반환
- v0.2 — 판정을 직접 계산하는 결정론적 오케스트레이터
- v0.3 — Bedrock FM 에이전트 계층 추가 (근거 수집 판단, 기준별 판단, 설명·질문 생성)

AWS 서비스 연결 전이므로 Knowledge Bases / Neptune / DynamoDB 자리는 로컬 어댑터가
동일한 계약으로 채우고 있습니다.

> 서울 리전에서 Global Claude Sonnet 4.5 inference profile의 Converse API 호출과
> 지원서 Schema 생성·누락 질문·추가 답변 병합을 실제로 검증했습니다. 자격 증명이 없으면
> 결정론적 스텁으로 내려앉습니다.

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

## 인증 (Amazon Cognito)

프론트엔드는 로그인 후 받은 Cognito ID 토큰을 `Authorization: Bearer <token>` 으로
보내고, API 는 그 토큰을 검증합니다 (`app/auth/`). User Pool 은
`backend/infra/auth_stack.py` (`cdk deploy HealthcareAuthStack`) 가 만듭니다.

```bash
COGNITO_USER_POOL_ID=ap-northeast-2_xxxxxxxxx
COGNITO_CLIENT_ID=xxxxxxxxxxxxxxxxxxxxxxxxxx
AUTH_REQUIRED=true
```

2026-08-04 기준 배포된 값은 `ap-northeast-2_M9srfkUaM` / 클라이언트
`12tvibdc3s1lc7f5ep4c5h0dko` 입니다 (서울 리전). 둘 다 비밀이 아니며 프론트 번들에도
포함됩니다. 로컬 실행용 값은 `backend/api/.env` 에 있고 이 파일은 git 에 올리지
않습니다.

검증 항목은 서명(User Pool JWKS, RS256), `iss`, `exp`/`iat`, `token_use`
(`id` 또는 `access`), 그리고 대상입니다. 대상은 ID 토큰이면 `aud`, Access 토큰이면
`client_id` 를 봅니다. Cognito 가 토큰 종류에 따라 대상을 다른 클레임에 넣기 때문에
직접 확인합니다. JWKS 는 첫 요청에서 받아 캐시합니다.

### 세 가지 모드

| 모드 | 조건 | 동작 |
|---|---|---|
| `cognito` | User Pool 설정 있음 | 토큰 검증, 없거나 틀리면 401 |
| `open` | 설정 없음, `AUTH_REQUIRED=false` | 검증 없이 통과 (로컬 개발) |
| `blocked` | 설정 없음, `AUTH_REQUIRED=true` | 전부 503 |

현재 모드는 `GET /health` 의 `auth_mode` 로 확인합니다. `open` 모드는 API 를
무인증으로 열어두므로 기동 로그에 경고를 남깁니다. 배포 환경에서는
`AUTH_REQUIRED=true` 를 설정해 설정 누락이 조용한 무인증 배포가 되지 않게 합니다.

| 환경 변수 | 기본값 | 설명 |
|---|---|---|
| `COGNITO_USER_POOL_ID` | 없음 | User Pool ID |
| `COGNITO_CLIENT_ID` | 없음 | 앱 클라이언트 ID (쉼표로 여러 개) |
| `COGNITO_REGION` | `AWS_REGION` | User Pool 리전 |
| `COGNITO_ADMIN_GROUP` | `admin` | 관리자로 인정할 Cognito 그룹 |
| `AUTH_REQUIRED` | `false` | 설정 누락 시 개방 모드로 내려앉지 않음 |
| `COGNITO_JWKS_CACHE_SECONDS` | `3600` | JWKS 캐시 수명 |
| `COGNITO_CLOCK_LEEWAY_SECONDS` | `30` | 시계 오차 허용치 |

### 접근 범위

인증은 앱 전역 의존성입니다. 새 엔드포인트는 별도 선언 없이 보호되며, 공개로 둘
경로만 `app/auth/dependencies.py` 의 `PUBLIC_PATHS` 에 적습니다.

| 범위 | 대상 |
|---|---|
| 공개 | `/`, `/health`, `/docs`, `/openapi.json` |
| 로그인 사용자 | 공고 조회, 지원서 수집, 스크리닝·추천 실행과 재조회, 확인 질문·답변, Intake 정규화 |
| 관리자 그룹 | 환자 목록, 근거 패킷, Trace, 코호트, 검토 큐, 감사 로그, `/api/v1/architecture` |

환자 단위 데이터는 본인 것만 볼 수 있습니다. 계정과 환자를 잇는 값은 Cognito 커스텀
속성 `custom:person_id` 이며, 이 값이 없는 계정은 환자 데이터에 접근할 수 없습니다
(`403`). 임상 정보는 한 번 잘못 열리면 되돌릴 수 없어서, 모르는 경우는 막는 쪽으로
둡니다. 관리자 그룹은 이 제한을 받지 않습니다.

`GET /api/v1/auth/me` 로 토큰이 어떤 주체로 인식되는지 확인할 수 있습니다.

```json
{
  "subject": "8f2c...",
  "email": "user@example.com",
  "groups": [],
  "person_id": 1,
  "token_use": "id",
  "is_admin": false,
  "anonymous": false
}
```

관리자 그룹은 셀프 가입으로 들어올 수 없고 운영자가 부여합니다.

```bash
aws cognito-idp admin-add-user-to-group \
  --user-pool-id <pool-id> --username <email> --group-name admin
```

### 아직 아닌 것

- 공고 등록·수정(`Refresh 모집공고`, `공고 추가`) API 가 없어서 관리자 전용 공고
  경로도 없습니다. 사용자 흐름은 기준에서 스키마를 파생하는
  `POST /api/v1/trials/{trial_id}/application-schema` 를 쓰고, 공고문에서 LLM 으로
  필드를 만드는 `POST /api/v1/application-schemas` 는 공고 관리 API 가 생기면 관리자
  범위로 옮깁니다.
- 감사 로그의 `actor` 는 여전히 요청 본문 값입니다. 토큰 주체(`cognito:<sub>`)로
  고정하는 것은 다음 단계입니다.
- 사용자 프로필은 Cognito 커스텀 속성에 있습니다. 아키텍처의 목표 저장소는
  DynamoDB `UserProfileTable` 입니다.

## 계층 구조

| 계층 | 모듈 | 역할 |
|---|---|---|
| 2. API 진입 | `app/main.py` | 요청 검증, 응답 조립 |
| 2. 인증 | `app/auth/` | Cognito 토큰 검증, 관리자·본인 범위 통제 |
| 3. 오케스트레이션 | `app/orchestration/` | Runtime, Criterion Router, Tool Gateway |
| 4. 전문 Tool | `app/tools/` | Criteria, Evidence Retrieval, Timeline Graph, Rule Evaluator |
| 5. 데이터 | `app/repository.py` | 로컬 CSV/JSONL 어댑터 |
| 6. 근거 검증·취합 | `app/reasoning/` | Evidence Bundle, Verifier, Judgment Verifier, Rule Aggregator, Deterministic Aggregator |
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
| 기준별 `OK`·`NOT_OK`·`UNKNOWN` 제안 | **FM** (제안만) |
| 판단 검증 7항목, 최종 상태 확정 | 규칙 (`JudgmentVerifier` + `RuleAggregator`) |
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

### 기준별 판단 → 검증 → 확정

기준 한 건은 `LLM 판단 → Judgment Verifier → Rule Aggregator` 순서로 지나갑니다
(`agent/judge.py`, `app/reasoning/judgment.py`, `app/reasoning/rule_aggregator.py`).

모델에게 넘기는 입력은 `evaluate_trial_criterion` 페이로드입니다. 기준의 연산자·값·
단위·`time_window_days` 와 `evidence_id` 가 붙은 근거 목록만 넣고, 직접 식별자는
넣지 않습니다. 모델은 `OK`·`NOT_OK`·`UNKNOWN` 중 하나를 제안하고 사용한
`used_evidence_ids` 를 함께 반환합니다.

Verifier는 그 제안을 일곱 항목으로 검사합니다. 검사 결과는 실행 응답의
`judgment.items[].verification.checks` 에 그대로 남습니다.

| 검사 | ID | 실패하면 |
|---|---|---|
| 근거 존재 | `V-EVIDENCE` | 인용한 출처가 근거 목록에 없거나, 인용 없이 확정하려 함 |
| 시간 범위 | `V-WINDOW` | 관찰 시점이 없거나 기준 기간을 벗어남 |
| 단위 | `V-UNIT` | 기준 단위와 관찰 단위가 다름 |
| 연산자 | `V-OPERATOR` | 규칙 계산과 판단 방향이 반대 |
| 기준 유형 | `V-TYPE` | 선정·제외 기준의 의미가 뒤집힘 |
| 개인정보 | `V-PII` | 출력에 직접 식별자 형식이 섞임 |
| 신뢰도 | `V-CONFIDENCE` | 확신도 0.55 미만으로 확정하려 함 |

Rule Aggregator가 최종 상태와 후속 경로를 정합니다.

| 상황 | 상태 | 경로 |
|---|---|---|
| 판단과 규칙이 일치하고 검증 통과 | `OK` / `NOT_OK` | `DECIDED` |
| 판단과 규칙이 정면 충돌 | `UNKNOWN` | `A2A` |
| 근거·출처·개인정보 검증 실패 | `UNKNOWN` | `HUMAN_REVIEW` |
| 제외 기준인데 근거 부족 | `UNKNOWN` | `HUMAN_REVIEW` |
| 선정 기준의 날짜·단위·확신도 부족 | `UNKNOWN` | `DECIDED` (확인 질문) |
| 규칙은 판정했으나 모델이 보류 | 규칙 판정 유지 | `DECIDED` (확신도 하향) |
| 규칙이 판정 못했는데 모델이 확정 주장 | `UNKNOWN` | `A2A` |

규칙 판정이 판정 원본입니다. 모델이 규칙보다 강한 결론을 내려도 상태를 올리지 않고,
충돌이면 교차 검토나 사람 검토로 넘깁니다. 놓친 제외 기준은 되돌릴 수 없으므로
제외 기준의 근거 부족은 확인 질문으로 미루지 않고 사람 검토로 보냅니다.

`CRITERION_JUDGE_ENABLED=false` 로 끄면 기존 FM NLI 검증기(`agent/verifier.py`)
경로를 사용합니다. 그때는 모델이 규칙과 반대 결론을 내면 `REVIEW_REQUIRED` 로
올립니다. 두 경로 모두 상태 확정은 결정론적 계층이 하므로 재현성은 같습니다.

기준별 판단 결과는 실행 응답의 `judgment`, 감사 로그의 `CRITERION_JUDGED`,
Trace의 `reason:judge` · `model:criterion_judge` 스팬에 남습니다.

### 폴백

모델 호출 실패, 형식에 맞지 않는 응답, 알 수 없는 판정값은 모두 규칙 판정으로 내려앉고
그 사실을 `notes` 에 남깁니다. 판정이 조용히 비는 상황을 만들지 않습니다.

출처 ID가 없으면 모델을 아예 호출하지 않습니다 (Contextual Grounding 선결 조건).

### Intake (자유 문장 정규화)

참여자 답변 같은 자유 문장을 측정값·약물·이상반응·상태 이벤트로 바꿉니다.
FM 은 조각을 추출만 하고, 날짜 계산·필드 정교화·단위·범위 검증은 규칙이 합니다.

```
POST /api/v1/intake/normalize
{"text": "3개월 전 HbA1c 7.8% 였고 혈압 150/95 mmHg 입니다.", "reference_date": "2024-06-15"}
```

근거 구간(`span`)이 원문에 없는 항목은 통과시키지 않고 `dropped` 에 이유를 남깁니다.
규칙 추출기가 항상 동작하므로 `BEDROCK_ENABLED` 가 꺼져 있어도 결과가 나옵니다.

`POST /api/v1/patients/{id}/answers` 는 원문을 그대로 보관하고 정규화 결과를
`intake` 로 덧붙입니다. 상대 시점의 기준일은 실행의 인덱스 방문일입니다.

용어 → 기준 필드 연결은 `app/domain/intake_vocabulary.py` 가 담당합니다.
카탈로그에 없는 용어는 `field: null` 로 통과해 기록만 남습니다.

자세한 규칙은 [../../agent/README.md](../../agent/README.md) 를 참고하세요.

### 설정

기본은 비활성입니다. 자격 증명 없는 환경에서도 동작해야 하므로 명시적으로 켭니다.

| 환경 변수 | 기본값 | 설명 |
|---|---|---|
| `BEDROCK_ENABLED` | `false` | 에이전트 모드 활성화 |
| `AWS_REGION` | `ap-northeast-2` | Bedrock 호출 리전 |
| `BEDROCK_MODEL_ID` | `global.anthropic.claude-sonnet-4-5-20250929-v1:0` | 검증된 Claude Sonnet inference profile |
| `BEDROCK_MAX_TOKENS` | `1024` | 응답 상한 |
| `AGENT_MAX_ITERATIONS` | `4` | tool-use 루프 상한 |
| `CRITERION_JUDGE_ENABLED` | `true` | 기준별 판단 → Verifier → Rule Aggregator 경로 사용 |
| `A2A_MAX_CRITERIA` | `5` | 2라운드 교차 검토 대상 기준 수 상한 |
| `RECOMMENDATION_MAX_WORKERS` | `2` | 추천 시 동시에 판정할 공고 수(1~5, `1`은 순차 실행) |
| `BEDROCK_GUARDRAIL_ID` | 없음 | Bedrock Guardrails 연결 |
| `KNOWLEDGE_BASE_ID` | 없음 | 설정하면 `evidence_retrieval_tool`이 Bedrock GraphRAG Retrieve 사용 |
| `PATIENT_PSEUDONYM_SECRET` | 없음 | 로컬 개발용 HMAC 키 |
| `PATIENT_PSEUDONYM_SECRET_ARN` | 없음 | 운영 환경 Secrets Manager HMAC 키 ARN |

`temperature` 는 0으로 고정되어 있습니다.

Bedrock을 켰지만 클라이언트 생성이 실패하면 스텁으로 내려앉고, 이유가
`/api/v1/architecture` 의 `agent.fallback_reason` 에 남습니다.

로컬에서 실제 Sonnet을 사용할 때는 `.env.example`을 `.env`로 복사한 뒤 다음처럼 실행할 수
있습니다. AWS access key는 파일에 넣지 말고 AWS CLI profile 또는 IAM role을 사용합니다.

```bash
cp backend/api/.env.example backend/api/.env
backend/.venv/bin/python -m uvicorn app.main:app --app-dir backend/api \
  --env-file backend/api/.env --reload --port 8000
```

실행 응답의 `mode` 필드로 어느 경로를 탔는지 확인할 수 있습니다
(`deterministic` / `agent:bedrock` / `agent:stub`).

`KNOWLEDGE_BASE_ID`를 설정하면 `EvidenceGatheringAgent`의
`evidence_retrieval_tool` 호출이 Bedrock Knowledge Base `Retrieve`로 연결됩니다.
이 모드에서는 HMAC 키 설정이 반드시 필요하며 모든 요청에 비식별 `patient_key`와
`document_type=patient_evidence` 필터를 강제합니다. 설정이 빠지면 로컬 검색으로
조용히 폴백하지 않고 컨테이너 구성을 실패시킵니다.

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
API의 `screening_decision`은 이를 `OK`, `NOT_OK`, `UNKNOWN`으로 단순화합니다.

`UNKNOWN`, `CONFLICTING`, `REVIEW_REQUIRED` 기준은 Bedrock이 켜진 경우 검토자와
반론자가 각 한 번씩 교차 검토합니다. 두 역할이 같은 결론을 내고 실제 입력의
`source_id`를 인용한 경우에만 `OK` 또는 `NOT_OK` 추천을 기록합니다. 이 추천은
결정론적 최종 상태를 덮어쓰지 않으며 불일치·근거 부재는 항상 `UNKNOWN`으로 끝납니다.
교차 검토는 공고당 최대 5개 기준, 정확히 2라운드에서 종료합니다. 종료 후에도
`UNKNOWN`이면 다시 Verifier로 순환하지 않고 Human Review Queue에 기록합니다.

여러 공고 추천은 각 공고의 스크리닝을 독립 실행한 뒤 결정론적으로 정렬합니다.
확정 `NOT_OK`는 추천에서 제외하고, 나머지는 `OK` 우선, `UNKNOWN` 차순으로 정렬합니다.
A2A 합의는 그라운딩된 경우에만 추천 점수에 반영하며 원래 판정은 보존합니다.
응답은 보존된 `screening_decision`과 추천 전용 `recommendation_decision`을 함께
내보내므로 A2A가 추천 포함·제외에 미친 영향을 구분할 수 있습니다.

### 재현성

상태 확정은 `DeterministicAggregator` 가 담당하며 FM을 호출하지 않습니다. Verifier는 상태를
'제안'만 하고 확정하지 않습니다. Bedrock FM이 붙어도 동일 입력에 동일 판정이 나오도록
설계한 분리입니다. 기준 버전(`criteria_version`)은 기준 내용 해시로 산출되므로, 기준이
바뀌면 재현 조건이 달라진 것을 식별할 수 있습니다.

## 주요 API

### 자연어 임상시험 지원서

지원서 수집은 스크리닝 판정과 분리되어 있습니다. 이 단계는 값을 수집할 뿐 적격 여부를
판단하지 않습니다.

```text
기본 스키마 v1
  + 공고 기준에서 파생된 필드 (또는 공고문에서 LLM이 추출한 필드)
  → 버전 고정 JSON Schema
  → 자연어 지원서 추출
  → 누락 필드 질문
  → 추가 자연어 답변 병합 (반복)
  → COMPLETE + 완성 JSON
  → 환자 기록 연결 + 지원서 필드 보충 근거 변환
  → Screening Orchestrator가 GraphRAG/Timeline Tool 호출
  → 근거 기반 스크리닝 결과
```

기본 스키마는 `age`, `sex`, `diagnosed_conditions`, `current_medications`,
`allergies`, `prior_trial_participation`을 필수로 포함합니다. 이름과 연락처 같은 직접
식별정보는 지원 자격 JSON에 포함하지 않고 사용자 계정 영역에서 별도로 관리합니다.
공고별 필드는 기본 필드를 덮어쓸 수 없으며 모두 `x-source: trial_notice`로 표시됩니다.

| Method | Path | 설명 |
|---|---|---|
| `POST` | `/api/v1/trials/{trial_id}/application-schema` | 공고 기준에서 지원서 스키마 파생 (LLM 미사용) |
| `GET` | `/api/v1/application-schemas/base` | 고정 기본 JSON Schema 조회 |
| `POST` | `/api/v1/application-schemas` | 공고문 자유 텍스트에서 LLM이 필드를 추가 |
| `GET` | `/api/v1/application-schemas/{schema_id}` | 버전이 고정된 스키마 조회 |
| `POST` | `/api/v1/applications` | 첫 자연어 지원서 제출 |
| `POST` | `/api/v1/applications/{application_id}/responses` | 누락 항목 추가 답변 |
| `GET` | `/api/v1/applications/{application_id}` | 현재 작성 상태 또는 완성 JSON 조회 |
| `POST` | `/api/v1/applications/{application_id}/screening` | 완성 JSON을 GraphRAG 오케스트레이터에 연결 |

### 공고 기준에서 스키마 파생 (권장 경로)

`POST /api/v1/trials/{trial_id}/application-schema` 는 그 공고의 선정·제외 기준
(`trial_criteria`)을 읽어 지원서 필드를 만듭니다 (`app/intake/trial_schema.py`).
화면에서 추천 공고를 클릭하면 이 호출 하나로 챗을 시작할 수 있습니다.

기준을 원본으로 쓰는 이유는 두 가지입니다. 공고문을 LLM에 넣어 필드를 상상하게 하면
같은 공고에서 실행마다 다른 질문이 나오고, 물어본 값이 어떤 기준에도 연결되지 않을 수
있습니다. 기준에서 파생하면 모든 필드에 `x-criterion-field`가 붙어 수집한 값이 반드시
어떤 기준의 입력이 됩니다. 모델을 부르지 않으므로 `BEDROCK_ENABLED=false`에서도 동작하고,
같은 기준이면 같은 `schema_id`가 나와 여러 번 호출해도 스키마가 늘어나지 않습니다.

기준이 있다고 모두 묻지는 않습니다.

| 조건 종류 | 지원자에게 질문 | 이유 |
|---|---|---|
| `NUMERIC_POINT` (HbA1c, eGFR) | O | 최근 검사 수치는 본인이 아는 값 |
| `CATEGORICAL` (당뇨 상태) | O | 현재 상태로 답할 수 있음 |
| `DERIVED_BOOLEAN` (임신 여부) | O | 해당 여부로 답할 수 있음 |
| `TEMPORAL_WINDOW` (진단 기간, 최근 365일 측정 횟수) | X | 기록에서 계산하는 값. Timeline Tool이 셈 |
| `NARRATIVE` | X | 자유서술 검색으로 확인 |

물어봐야 답이 나오지 않는 항목을 필수로 넣으면 재질문 5회를 그것으로 소진하고
`MAX_FOLLOW_UPS_REACHED`로 끝납니다. 제외된 조건도 `notice_text` 공고 요약에는 그대로
남습니다.

기준의 임계값은 질문 문구에 넣지 않습니다. "HbA1c 7.0 이상이신가요?"처럼 물으면 답이
조건 쪽으로 끌려갑니다. 값만 묻고 판정은 뒤 계층이 합니다. 단위는 기준이 선언한 값을
그대로 `x-unit`에 넣어 판정 단계의 단위 검증과 어긋나지 않게 합니다.

### 공고문에서 LLM으로 스키마 생성

`POST /api/v1/application-schemas` 는 공고 담당 팀이 `notice_text`만 전달하면 LLM이
확장 필드를 생성합니다. 아직 기준이 구조화되지 않은 공고를 받을 때 쓰는 경로입니다.
이미 구조화된 필드를 가지고 있다면 `additional_fields`로 직접 전달할 수도 있어 팀 간
연결 시 LLM 처리를 중복하지 않습니다. `BEDROCK_ENABLED=false`인 로컬 환경에서는 공고
내용을 추측하지 않으며, `additional_fields`를 명시적으로 전달해야 합니다.

지원서 응답의 `status`는 `NEEDS_MORE_INFO`, `COMPLETE`, 또는
`MAX_FOLLOW_UPS_REACHED`입니다. 미완성 응답에는 원문 공고,
누락 필드 목록과 `follow_up_prompt`가 함께 포함됩니다. 빈 배열(`알레르기 없음`)과 `false`
(`과거 참여 없음`)는 유효한 답변이며 누락으로 처리하지 않습니다. 배열 필드는 추가 답변을
누적하고 같은 값을 중복 제거합니다. 명시적인 `없음`은 기존 배열을 비우는 정정으로 처리하며,
나이·성별·수치·불리언 같은 단일값은 가장 최근의 명확한 답변으로 교체합니다.

재질문은 실제로 발행한 횟수를 기준으로 최대 5회입니다. 다섯 번째 추가 답변 이후에도 누락이
남으면 `MAX_FOLLOW_UPS_REACHED`로 종료하고 `follow_up_prompt`를 더 이상 반환하지 않습니다.
종료된 세션에 답변을 추가하면 `409 Conflict`를 반환합니다.

`POST /api/v1/applications/{application_id}/screening`은 `COMPLETE` 상태에서만 실행됩니다.
요청의 `person_id`로 기존 임상 기록을 연결하고, 지원서의 스칼라 값을
`PATIENT_REPORTED` 보충 관찰값으로 변환합니다. 공고별 필드에 `criterion_field`와 `unit`을
지정하면 각각 JSON Schema의 `x-criterion-field`, `x-unit`으로 고정되어 오케스트레이터 기준
필드에 정확히 연결됩니다. 목록 값은 단일 관찰값으로 추측하지 않고 제외 내역에 남깁니다.

오케스트레이터는 기존 환자 기록을 우선하며 지원서 값으로 덮어쓰지 않습니다. 자유서술 근거가
필요한 기준은 `evidence_retrieval_tool`을 호출합니다. Knowledge Base 설정 환경에서는
`person_id`를 가명 `patient_key`로 변환한 뒤 환자 격리 필터가 적용된 GraphRAG 검색을 수행하고,
로컬 환경에서는 동일 계약의 키워드 검색을 사용합니다. 응답의 `supplements`에는 적용·제외
필드, `source_application_id`, 실제 `retrieval_mode`가 포함됩니다.

현재 `IntakeStore`는 로컬 개발용 메모리 구현입니다. 공개 메서드 계약을 유지한 채 DynamoDB
어댑터로 교체할 수 있으며, 다중 인스턴스 배포 전에는 반드시 영속 저장소로 교체해야 합니다.

### 스크리닝

| Method | Path | 설명 |
|---|---|---|
| `POST` | `/api/v1/screening/run` | 환자 x 시험 한 건 실행 |
| `GET` | `/api/v1/screening/{run_id}` | 실행 결과 재조회 |
| `GET` | `/api/v1/screening/{run_id}/evidence` | 기준별 근거 패킷 (관리자) |
| `GET` | `/api/v1/screening/{run_id}/trace` | Agent·Tool·Model 스팬 (관리자) |

요청 예시:

```json
{ "person_id": 3, "trial_id": "SYN-T2D-INTENSIFY-01" }
```

### 임상시험 추천

| Method | Path | 설명 |
|---|---|---|
| `POST` | `/api/v1/recommendations/run` | 후보 공고 전체 판정, A2A 반영, 최적 후보 정렬 |
| `GET` | `/api/v1/recommendations/{recommendation_id}` | 저장된 추천 결과 재조회 |

```json
{
  "person_id": 3,
  "trial_ids": ["SYN-T2D-INTENSIFY-01", "SYN-T2D-CARDIO-01"],
  "top_k": 3
}
```

추천 점수는 모델 생성값이 아니다. 확정 `OK=1.0`, 미해소 `UNKNOWN=0.4`,
그라운딩된 A2A `OK` 합의는 `0.8`처럼 코드에 고정된 보수적 점수를 사용한다.

### 코호트

관리자 그룹만 호출할 수 있습니다.

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

관리자 그룹만 호출할 수 있습니다.

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
`/api/v1/patients` 목록은 관리자 그룹만, 환자 단위 조회는 본인 또는 관리자만
호출할 수 있습니다.

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
- **직접 식별자 검사** (`app/safety/pii.py`) — 모델 판단 출력에 주민번호·연락처·이메일·
  내부 환자번호·호칭이 붙은 이름 형식이 섞이면 그 기준을 사람 검토로 보냅니다.
  형식이 뚜렷한 유출을 막는 1차 방어선이며, 애초에 LLM 에는 `patient_key` 만 넘깁니다.
- **감사 로그** — append-only 입니다. 상태가 바뀌면 기존 이벤트를 수정하지 않고 새 이벤트를
  추가합니다. `export_ndjson()` 은 Firehose 페이로드와 동일한 형태입니다.
- **Observability** — 모든 Agent·Tool·Model 호출이 스팬으로 기록되고 지연시간이 집계됩니다.

## 알려진 제약

- 판정은 인덱스 방문(최신 방문) 시점의 관찰값을 기준으로 계산됩니다.
- Bedrock 실제 검증은 합성 공고와 합성 지원서로 수행했습니다. 실제 환자 정보나 운영 공고를
  전송하지 않았으며, 운영 전에는 IAM·데이터 처리 정책과 공고별 Schema 승인을 확인해야 합니다.
- 실호출로 확인한 범위는 지원서 수집 경로입니다. 기준별 판단(`criterion_judge`)과
  A2A 교차 검토는 스텁·스크립트 모델로만 검증했습니다. 실제 모델을 붙이면 판단이
  규칙과 갈리는 빈도를 다시 봐야 합니다.
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
| `app/tools/evidence_retrieval.py` | 로컬 키워드 또는 Bedrock KB Retrieve 어댑터 | Bedrock Knowledge Bases GraphRAG |
| `app/tools/timeline_graph.py` | CSV | Amazon Neptune |
| `agent/model.py` | 스텁 | Bedrock Converse (Global Claude Sonnet 4.5 검증) |
| `app/safety/guardrails.py` | 정규식 | Bedrock Guardrails (연결부 구현) |
| `app/persistence/run_store.py` | 메모리 | DynamoDB |
| `app/persistence/audit.py` | 메모리 | DDB Streams → Firehose → S3 |
| `app/safety/observability.py` | 메모리 | AgentCore Observability → CloudWatch |
