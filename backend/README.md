# AWS Healthcare Backend

임상시험 매칭 플랫폼의 백엔드입니다.  
**데이터 파이프라인**(전처리·검색·그래프)과 **스크리닝 API**(환자↔임상시험 매칭)로 구성됩니다.

## 전체 아키텍처

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         데이터 파이프라인 (infra/ + lambdas/)                  │
│                                                                             │
│  clinical_notes.jsonl ──▶ Sanitizer ──▶ 환자별 Markdown + metadata            │
│                                  └────▶ Bedrock KB GraphRAG + Neptune Analytics│
│  임상시험 공고 PDF ──────▶ Protocol Parser ──▶ DynamoDB Criteria Store        │
│                                                                             │
└────────────────────────────────────┬────────────────────────────────────────┘
                                     │ 데이터 읽기
                                     ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                      스크리닝 오케스트레이터 API (api/)                        │
│                                                                             │
│  환자 x 시험 매칭 요청                                                       │
│    → 근거 수집 (Bedrock Knowledge Bases GraphRAG)                            │
│    → 기준별 판정 (규칙 + Bedrock FM)                                         │
│    → 종합 적격성 확정                                                        │
│    → 근거 패킷 · 확인 질문 · 설명 반환                                        │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## 프로젝트 구조

```
backend/
├── app.py                            # CDK 엔트리포인트
├── cdk.json                          # CDK 설정
├── requirements.txt                  # CDK 의존성
│
├── config/
│   └── settings.py                   # 전역 환경변수 설정
│
├── infra/                            # AWS CDK 인프라 코드
│   ├── s3_stack.py                   # S3 데이터 레이크 (KMS 암호화)
│   ├── bedrock_stack.py              # Bedrock KB GraphRAG + Neptune Analytics
│   ├── observability_stack.py        # CloudWatch 대시보드/알람/SNS
│   └── main_stack.py                # Lambda, DynamoDB, Step Functions
│
├── lambdas/                          # 데이터 파이프라인 Lambda
│   ├── sanitizer/                    # PHI 마스킹 + 판정 문장 제거
│   └── protocol_parser/              # 공고 PDF → 선정/제외 기준 JSON
│
├── step_functions/                   # 파이프라인 오케스트레이션
│   ├── emr_pipeline.json             # Sanitizer → Bedrock GraphRAG ingestion
│   └── trials_pipeline.json          # Protocol Parser → SNS 검토 알림
│
├── api/                              # 스크리닝 오케스트레이터 API (FastAPI)
│   ├── app/
│   │   ├── main.py                   # API 진입점 (FastAPI)
│   │   ├── container.py              # 의존성 조립 (어댑터 교체 지점)
│   │   ├── orchestration/            # Runtime, Router, Gateway
│   │   ├── tools/                    # Criteria, Evidence, Timeline, Rule
│   │   ├── reasoning/                # 근거 검증, 취합, 상태 확정
│   │   ├── actions/                  # Cohort, Packet, NextBest, Explanation
│   │   ├── intake/                   # 기본+공고 스키마, 자연어 지원서 반복 수집
│   │   ├── domain/                   # 상태 모델, 기준 정의
│   │   ├── persistence/              # Run Store, 감사 로그
│   │   ├── safety/                   # Guardrails, Observability
│   │   ├── repository.py             # 데이터 어댑터 (CSV → AWS 전환 예정)
│   │   └── schemas.py                # Pydantic 스키마
│   ├── conftest.py                   # 테스트 import 경로 설정
│   ├── tests/                        # API 테스트
│   └── requirements.txt              # API 의존성
│
├── tests/
└── scripts/
```

에이전트 계층은 저장소 루트의 `agent/` 에 있습니다. `backend` 를 import 하지 않고
`agent/contracts.py` 의 Protocol 로만 외부와 연결되며, `api/app/container.py` 가
구현을 주입합니다. 자세한 내용은 [../agent/README.md](../agent/README.md) 참고.

## 데이터 파이프라인 (infra/ + lambdas/)

### Lambda 함수

| Lambda | 역할 | 입력 → 출력 |
|--------|------|-------------|
| Sanitizer | PHI 마스킹 + HMAC 비식별 키 + 환자별 문서 집계 | `raw/clinical_notes.jsonl` → `rag/patients/*.md` + metadata |
| Protocol Parser | 공고 구조화 | `trials/*.pdf` → DynamoDB Criteria Store |

GraphRAG 검색은 FastAPI의 `api/app/tools/evidence_retrieval.py`가 Bedrock KB
Retrieve API를 직접 호출한다.

### Step Functions 워크플로우

**EMR Pipeline:**
```
Sanitizer → Bedrock GraphRAG ingestion → (상태 폴링) → 완료
```

**Trials Pipeline:**
```
EventBridge (trials/ 업로드) → Protocol Parser → SNS 검토 알림
```

- 모든 단계 재시도 3회 (BackoffRate 2.0) + DLQ
- 실패 시 SNS 알림

### 관측성

- CloudWatch Dashboard: Lambda 호출/에러/실행시간, Bedrock 토큰/비용
- 알람: 에러(≥3회/5분), 쓰로틀(≥5회), Duration(p95 > 240s)
- SNS 알림: 파이프라인 실패, 검토 대기건

## 스크리닝 API (api/)

9계층 스크리닝 오케스트레이터입니다. 기준별로 근거를 수집·검증한 뒤 판정하고, 근거 패킷·확인 질문·설명을 반환합니다.

### 주요 API 엔드포인트

| Method | Path | 설명 |
|--------|------|------|
| `POST` | `/api/v1/screening/run` | 환자 x 시험 스크리닝 실행 |
| `GET` | `/api/v1/screening/{run_id}` | 실행 결과 조회 |
| `GET` | `/api/v1/screening/{run_id}/evidence` | 기준별 근거 패킷 |
| `POST` | `/api/v1/cohort/run` | 배치 코호트 분석 |
| `GET` | `/api/v1/patients/{person_id}/questions` | 확인 질문 (정보 가치 순) |
| `POST` | `/api/v1/patients/{person_id}/answers` | 답변 제출 (자유 문장 정규화 포함) |
| `POST` | `/api/v1/intake/normalize` | 자유 문장 → 구조화 이벤트 정규화 |
| `GET` | `/api/v1/review-queue` | 검토 대기 목록 |

### 판정 상태

| 상태 | 의미 |
|------|------|
| `EVIDENCE_FOUND` | 근거 확인, 조건 충족 |
| `CONTRADICTED` | 근거 확인, 조건 충돌 (부적합) |
| `UNKNOWN` | 관찰값 없음 → 확인 질문 생성 |
| `CONFLICTING` | 기록 간 값 불일치 → 검토 큐 |
| `REVIEW_REQUIRED` | 자동 판정 신뢰도 낮음 → 검토 큐 |

### FM vs 규칙 역할 분리

| 담당 | 주체 |
|------|------|
| 근거 수집 판단, NLI 검증, 설명/질문 생성 | Bedrock FM |
| 수치·기간 계산, **기준별 상태 확정**, **종합 적격성 판정** | 규칙 (결정론적) |

FM은 적격성을 결정하지 않습니다. 동일 입력 → 동일 판정 보장을 위한 설계입니다.

## 파이프라인 ↔ API 연결 지점

파이프라인이 생성한 데이터를 API가 읽어서 사용합니다:

| API Tool | 현재 (로컬 어댑터) | 파이프라인 연결 후 |
|----------|-------------------|-------------------|
| `criteria_tool` | CSV | DynamoDB Criteria Store |
| `evidence_retrieval_tool` | 키워드 검색 | Bedrock Knowledge Bases GraphRAG |
| `timeline_graph_tool` | CSV | DynamoDB `PatientClinicalEventTable` |
| `agent/model` | 스텁 | Bedrock Converse API |
| `persistence/run_store` | 메모리 | DynamoDB |

교체 지점: `api/app/container.py` 한 곳에서 어댑터를 바꾸면 됩니다.

## 실행 방법

### 인프라 배포 (CDK)

```bash
cd backend
pip install -r requirements.txt
export ALERT_EMAIL="your-email@example.com"
cdk bootstrap    # 최초 1회
cdk deploy --all
```

### API 로컬 실행

```bash
python -m venv backend/.venv
source backend/.venv/bin/activate
pip install -r backend/api/requirements.txt
uvicorn app.main:app --app-dir backend/api --reload --port 8000
```

- Swagger: http://127.0.0.1:8000/docs
- 상태 확인: http://127.0.0.1:8000/health

### API 테스트

```bash
cd backend/api
pytest tests -q
```

## S3 버킷 구조

| 경로 | 용도 | 보안 |
|------|------|------|
| `raw/` | 원본 데이터 보존 | KMS 암호화, 90일 후 Glacier |
| `rag/patients/` | 환자별 비식별 Markdown + metadata | KMS 암호화 |
| `trials/` | 임상시험 공고 원문 | KMS 암호화 |

## 배포 스택 순서

1. `HealthcareS3Stack` - S3 데이터 레이크
2. `HealthcareBedrockStack` - Bedrock KB GraphRAG + Neptune Analytics
3. `HealthcareObservabilityStack` - 모니터링
4. `HealthcareMainStack` - Lambda, DynamoDB, Step Functions

## 담당 구분

| 영역 | 담당 | 상태 |
|------|------|------|
| 데이터 전처리 (Sanitizer, 환자별 GraphRAG 문서) | 파이프라인 | ✅ 완료 |
| 검색·그래프 (Bedrock KB GraphRAG, Neptune Analytics) | 파이프라인 | ✅ 완료 |
| 공고 구조화 (Protocol Parser, DynamoDB) | 파이프라인 | ✅ 완료 |
| 파이프라인 오케스트레이션 (Step Functions) | 파이프라인 | ✅ 완료 |
| 관측성 (CloudWatch) | 파이프라인 | ✅ 완료 |
| 스크리닝 오케스트레이터 API | GGeunGGeun | ✅ 완료 |
| 로컬 어댑터 → AWS 연결 | 공동 | 🔄 진행 예정 |
