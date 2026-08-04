# 임상시험 매칭 모델 v2

## 목적

이 모델은 Medi25에서 가져온 임상시험 모집공고와 사용자의 기본 정보, EHR/EMR, 추가 설문 데이터를 종합해 사용자에게 가장 적합한 임상시험을 추천한다.

최종 결과는 기준별로 `OK`, `NOT_OK`, `UNKNOWN` 세 가지 상태를 반환한다. `UNKNOWN`은 정보가 부족하거나 근거가 애매한 상태이며, LLM 질문 생성과 A2A 토론을 통해 다시 판단한다.

## 전체 모델 흐름

```mermaid
flowchart TD
    START{"워크플로우 시작점"}
    START -->|관리자/운영자 Refresh| REF["최신 공고 가져오기"]
    START -->|관리자 직접 추가| ADMINADD["모집공고 수동 추가"]
    START -->|자동 스케줄러| AUTO["정기 공고 수집"]
    START -->|사용자 직접 입력| U["사용자 로그인"]

    REF --> A["Medi25 모집공고 수집"]
    ADMINADD --> MANUAL["관리자 입력 공고 검증"]
    MANUAL --> B
    AUTO --> A
    A --> CHECK["이미 수집한 데이터인지 확인"]
    CHECK --> B["공고 정규화 JSON 생성"]
    B --> C["선정/제외 기준 추출"]
    C --> D["임상시험 기준 JSON"]
    D --> DBT["공고/기준 DB 저장"]

    U --> P["기본 정보 입력"]
    P --> E["EHR/EMR 또는 직접 입력"]
    E --> S["비식별화 및 정규화"]
    S --> CACHE["ElastiCache 임시 저장<br/>TTL 기반 개인정보 보호"]
    CACHE --> J["환자 임상 JSON"]
    J --> DBP["환자 임상 데이터 DB 저장"]

    DBT --> R["Graph RAG"]
    DBP --> R
    J --> R
    STD["표준문서/용어집/코드체계"] --> R

    R --> AG["Agent Orchestrator"]
    AG --> M["LLM 판단"]
    M --> V["Verifier / Rule Aggregator"]
    V --> O["OK / NOT_OK / UNKNOWN"]

    O -->|UNKNOWN| Q["LLM 질문 생성"]
    Q --> QA["추가 설문"]
    QA --> J

    O -->|판단 애매| A2A["A2A 에이전트 토론"]
    A2A --> V

    O --> REC["최적 임상시험 추천"]
    REC --> REP["보고서 저장"]
```

## 시작점과 자동화 관점

전체 워크플로우는 반드시 명확한 시작점이 있어야 한다. 시작점은 사용자가 직접 정보를 입력하는 경우와, 운영자가 최신 모집공고를 갱신하는 경우로 나뉜다.

| 시작 방식 | 트리거 | 목적 |
|-----------|--------|------|
| 사용자 시작 | 로그인 후 프로필/EMR 입력 | 특정 사용자에게 맞는 임상시험 추천 |
| 운영자 시작 | `Refresh` 버튼 클릭 | Medi25 최신 모집공고 수동 갱신 |
| 관리자 시작 | 공고 직접 추가/수정 | Medi25에 없거나 누락된 모집공고 등록 |
| 자동 시작 | EventBridge/Scheduler | 매일 또는 일정 주기로 최신 공고 자동 수집 |
| 재판정 시작 | 추가 설문 제출 | `UNKNOWN` 기준을 다시 판단 |

초기 화면에서 중요한 버튼은 `Refresh 모집공고`, `공고 추가`, `매칭 시작`이다. `Refresh 모집공고`는 최신 Medi25 데이터를 가져와 DB에 반영하고, `공고 추가`는 관리자가 직접 모집공고를 등록하거나 수정하는 기능이다. `매칭 시작`은 현재 저장된 공고와 사용자 데이터를 기반으로 전체 에이전트 워크플로우를 실행한다.

## 관리자 공고 추가 모델

관리자로 로그인한 사용자는 Medi25 자동 수집 결과와 별개로 모집공고를 직접 추가할 수 있어야 한다. 병원에서 전달받은 공고, 아직 Medi25에 반영되지 않은 공고, PDF/이미지/문서 형태의 모집공고를 등록하기 위한 기능이다.

```mermaid
flowchart TD
    A["관리자 로그인"] --> B["공고 추가 화면"]
    B --> C{"입력 방식"}
    C -->|URL| U["공고 URL 입력"]
    C -->|파일| F["PDF/이미지/문서 업로드"]
    C -->|수동 입력| M["제목/질환/병원/조건 직접 입력"]
    U --> X["공고 원문 수집"]
    F --> X
    M --> V["스키마 검증"]
    X --> P["LLM 기준 추출"]
    P --> V
    V --> R{"검증 통과?"}
    R -->|예| S["TrialNotice/Criteria DB 저장"]
    R -->|아니오| E["수정 요청"]
    S --> I["RAG/Graph 인덱스 갱신"]
    I --> H["관리자 감사 로그 저장"]
```

관리자 입력 필드:

| 필드 | 필수 | 설명 |
|------|------|------|
| `title` | Y | 모집공고 제목 |
| `disease` | Y | 대상 질환 |
| `hospital` | N | 실시 기관 |
| `location` | N | 지역 |
| `recruitment_status` | Y | 모집중/마감/검토중 |
| `age` | N | 나이 조건 |
| `sex` | N | 성별 조건 |
| `inclusion_text` | Y | 선정 기준 원문 |
| `exclusion_text` | N | 제외 기준 원문 |
| `source_type` | Y | `MEDI25`, `ADMIN_URL`, `ADMIN_FILE`, `ADMIN_MANUAL` |
| `source_url` | N | 원문 URL |
| `source_file_id` | N | 업로드 파일 ID |

관리자 추가 공고 상태:

| 상태 | 의미 |
|------|------|
| `DRAFT` | 입력 중 |
| `PENDING_REVIEW` | LLM 추출 결과 검토 대기 |
| `ACTIVE` | 매칭에 사용할 수 있음 |
| `NEEDS_FIX` | 필수값 또는 기준 파싱 오류 |
| `ARCHIVED` | 사용 중지 |

관리자 공고 추가 원칙:

- 관리자가 추가한 공고도 Medi25 공고와 같은 `TrialNoticeTable`, `TrialCriteriaTable`에 저장한다.
- 자동 수집 공고와 구분하기 위해 `source_type`과 `created_by`를 반드시 남긴다.
- LLM이 추출한 선정/제외 기준은 바로 활성화하지 않고 관리자 확인 후 `ACTIVE`로 전환한다.
- 공고 수정 시 기존 기준을 덮어쓰지 않고 `criteria_version`을 증가시킨다.
- 모든 추가/수정/활성화/비활성화 이벤트는 감사 로그에 남긴다.

## 데이터 최신성 확인 모델

자동화 관점에서는 단순히 데이터를 긁어오는 것보다, 이미 수집한 데이터인지, 변경됐는지, 최신인지 확인하는 단계가 필요하다.

```mermaid
flowchart TD
    R["Refresh 요청"] --> M["Medi25 목록 조회"]
    M --> H["공고 URL/제목/상태 해시 생성"]
    H --> C{"DB에 동일 공고 존재?"}
    C -->|없음| N["신규 공고 저장"]
    C -->|있음| U{"내용 변경됨?"}
    U -->|예| V["공고 버전 증가 후 저장"]
    U -->|아니오| S["마지막 확인 시각만 갱신"]
    N --> P["기준 추출"]
    V --> P
    P --> K["RAG/Graph 인덱스 갱신"]
```

공고 수집 시 확인할 값:

- `detail_url`
- `trial_id`
- `title`
- `recruitment_status`
- `content_hash`
- `collected_at`
- `last_checked_at`
- `criteria_version`

## 저장 DB 모델

보고서와 모집공고는 별도 저장소가 필요하다. 공고 원문, 정규화 JSON, 기준 JSON, 매칭 결과 보고서를 분리해서 저장해야 추적과 재판정이 가능하다.

| 데이터 | 저장소 후보 | 설명 |
|--------|-------------|------|
| 공고 원문 HTML/PDF | S3 `trials/raw/` | 원본 보존, 재파싱 가능 |
| 공고 정규화 JSON | DynamoDB `TrialNoticeTable` | 목록 조회, 검색, 상태 관리 |
| 선정/제외 기준 JSON | DynamoDB `TrialCriteriaTable` | 기준 버전 관리 |
| 기준/공고 임베딩 문서 | S3 `rag/trials/` + Bedrock KB | RAG 검색용 |
| 수신 개인정보 JSON | ElastiCache for Redis | 워크플로우 처리용 임시 저장, 짧은 TTL 적용 |
| 환자 원본 EMR | S3 `emr/raw/` | 암호화 저장, LLM 직접 전달 금지 |
| 비식별 임상 이벤트 | DynamoDB 또는 Neptune | 매칭 판단용 이벤트 |
| Graph RAG 관계 | Amazon Neptune | 환자-이벤트-기준-표준문서 연결 |
| 매칭 실행 결과 | DynamoDB `MatchingRunTable` | 실행 이력, 상태, 재실행 |
| 최종 보고서 | S3 `reports/` + DynamoDB 메타데이터 | 사용자/관리자 조회용 |
| A2A 토론 로그 | DynamoDB 또는 S3 append-only | `UNKNOWN` 판단 근거 보존 |

최소 DB 테이블:

| 테이블 | 키 | 역할 |
|--------|----|------|
| `TrialNoticeTable` | `trial_id` | Medi25 공고 목록과 최신 상태 |
| `TrialCriteriaTable` | `trial_id`, `criteria_version` | 선정/제외 기준 버전 |
| `AdminAuditLogTable` | `event_id` | 관리자 공고 추가/수정/활성화 이력 |
| `UserProfileTable` | `user_id` | 사용자 기본 정보와 관심 임상 |
| `PatientClinicalEventTable` | `patient_key`, `observed_at` | 정규화된 임상 이벤트 |
| `MatchingRunTable` | `run_id` | 매칭 실행 단위 |
| `MatchingReportTable` | `report_id` | 최종 보고서 메타데이터 |

## 개인정보 JSON 임시 처리 모델

사용자에게서 받은 JSON에는 이름, 성별, 관심 임상, EHR/EMR 연결 정보, 추가 설문 답변처럼 민감한 정보가 섞일 수 있다. 이 데이터는 전체 워크플로우가 빠르게 돌아가도록 Amazon ElastiCache for Redis에 먼저 넘긴다.

ElastiCache는 임시 처리 계층으로 사용한다. 영구 보관이 필요한 값은 비식별화와 정규화를 거친 뒤 DynamoDB, S3, Neptune에 목적별로 나누어 저장한다.

```mermaid
flowchart TD
    IN["사용자/EMR JSON 수신"] --> VALID["스키마 검증"]
    VALID --> ENC["전송 구간 암호화"]
    ENC --> REDIS["ElastiCache Redis<br/>session:{run_id}"]
    REDIS --> NORM["비식별화 및 임상 이벤트 정규화"]
    NORM --> PII["PII Vault 또는 계정 DB<br/>직접 식별자 분리"]
    NORM --> CLIN["PatientClinicalEventTable"]
    NORM --> GRAPH["Neptune Graph"]
    NORM --> RAG["Bedrock KB용 비식별 문서"]
    REDIS --> TTL["TTL 만료 후 자동 삭제"]
```

ElastiCache 사용 원칙:

| 항목 | 정책 |
|------|------|
| 용도 | 매칭 실행 중 임시 데이터 전달과 빠른 조회 |
| 키 형식 | `matching:{run_id}:input`, `matching:{run_id}:normalized` |
| TTL | 기본 15분에서 60분 사이, 재판정 중이면 연장 |
| 저장 범위 | 원본 JSON은 최소 시간만 보관 |
| 암호화 | in-transit encryption, at-rest encryption 활성화 |
| 접근 | VPC 내부 API/Agent 워커만 접근 |
| 로그 | 원본 JSON 전체를 로그에 남기지 않음 |
| 영구 저장 | 비식별화 후 목적별 DB/S3/Neptune에 저장 |

수신 JSON 예시:

```json
{
  "run_id": "run_20260804_001",
  "user_id": "user_123",
  "profile": {
    "name": "홍길동",
    "sex": "FEMALE",
    "birth_year": 1990,
    "interests": ["당뇨병", "디지털치료제"]
  },
  "emr_payload_ref": "upload://emr/session_abc",
  "answers": [
    {
      "question_id": "q_pregnancy_001",
      "value": "아니오"
    }
  ]
}
```

ElastiCache에 들어가는 값은 다음 단계에서 바로 분리된다.

- `name`, 연락처, 계정 식별자: PII 저장소 또는 계정 DB
- `sex`, `birth_year`, 관심 임상: 사용자 프로필 DB
- EHR/EMR 본문: S3 raw 영역에 암호화 저장
- 비식별 임상 이벤트: DynamoDB/Neptune/RAG
- 추가 설문 답변: `SURVEY_ANSWER` 이벤트로 정규화

## 입력 데이터

### 1. Medi25 공고 데이터

Medi25에서 임상시험 모집공고를 가져온 뒤 표준 JSON으로 변환한다.

```json
{
  "source": "Medi25",
  "trial_id": "medi25-10713",
  "title": "당뇨병 대상 임상시험",
  "disease": ["당뇨병"],
  "status": "RECRUITING",
  "age": "만 19세 이상",
  "sex": "ALL",
  "location": "서울",
  "hospital": "OO병원",
  "visit_schedule": "총 8회 방문",
  "detail_url": "https://www.medi25.com/...",
  "collected_at": "2026-08-04T00:00:00+09:00"
}
```

### 2. 사용자 기본 정보

사용자 로그인 이후 최소한의 정보를 받는다.

```json
{
  "user_id": "user_123",
  "patient_key": "pt_7f3a",
  "name_ref": "pii:user_123:name",
  "sex": "FEMALE",
  "birth_year": 1990,
  "interests": {
    "diseases": ["당뇨병"],
    "trial_types": ["약물", "디지털치료제"],
    "regions": ["서울", "경기"]
  },
  "ehr_emr_connected": true
}
```

이름 같은 직접 식별자는 LLM이나 RAG에 넣지 않는다. LLM에는 `patient_key`처럼 비식별 키만 전달한다.

### 3. EHR/EMR 정규화 데이터

EHR/EMR, 사용자가 직접 입력한 건강 정보, 추가 설문 답변은 모두 같은 임상 이벤트 JSON으로 정규화한다.

```json
{
  "patient_key": "pt_7f3a",
  "event_type": "MEASUREMENT",
  "field": "hba1c",
  "value": 7.8,
  "unit": "%",
  "observed_at": "2026-07-20",
  "source": {
    "type": "EMR",
    "document_id": "note_20260720_01",
    "span": "HbA1c 7.8%"
  },
  "confidence": 0.93
}
```

## 모델 구성

## 1. 공고 수집 모델

역할:

- Medi25에서 모집 중인 임상시험 공고를 가져온다.
- 마감, 예약, 홍보성 페이지는 제외한다.
- 공고의 핵심 정보를 표준 JSON으로 만든다.
- 원문 URL과 수집 시점을 보존한다.

출력:

- `trial_notice.json`
- `trial_raw_snapshot`

## 2. 공고 기준 추출 모델

역할:

- Medi25 공고 원문에서 선정 기준과 제외 기준을 추출한다.
- 나이, 성별, 질환, 검사값, 약물, 방문 조건 등을 구조화한다.
- 표준문서와 용어집을 참조해 기준 필드를 정규화한다.

출력:

```json
{
  "trial_id": "medi25-10713",
  "criteria_version": "2026-08-04.1",
  "inclusion": [
    {
      "criterion_id": "inc_hba1c_001",
      "raw_text": "최근 HbA1c 7.0% 이상",
      "field": "hba1c",
      "operator": ">=",
      "value": 7.0,
      "unit": "%",
      "time_window_days": 180
    }
  ],
  "exclusion": [
    {
      "criterion_id": "exc_pregnancy_001",
      "raw_text": "임신 중인 대상자 제외",
      "field": "active_pregnancy",
      "operator": "!=",
      "value": true
    }
  ]
}
```

## 3. 사용자 임상 프로필 모델

역할:

- 로그인한 사용자의 기본 정보와 관심 임상을 저장한다.
- EHR/EMR 연결 여부를 관리한다.
- 직접 식별 정보와 임상 판단용 정보를 분리한다.

핵심 원칙:

- LLM에는 이름, 연락처, 주민번호 같은 직접 식별자를 넣지 않는다.
- 성별, 나이대, 관심 질환처럼 매칭에 필요한 정보만 전달한다.
- 동의 범위에 따라 데이터 사용 범위를 제한한다.

## 4. 임상 이벤트 정규화 모델

역할:

- EHR/EMR 기록을 진단, 검사, 약물, 시술, 상태, 이상반응 이벤트로 변환한다.
- 사용자의 직접 입력과 추가 설문 답변도 같은 이벤트 모델로 합친다.
- 날짜, 단위, 코드, 부정 표현은 규칙 기반으로 검증한다.
- 자유서술에서 필요한 후보 정보는 LLM이 추출한다.

이벤트 타입:

| 타입 | 예시 |
|------|------|
| `MEASUREMENT` | HbA1c, 혈압, BMI, eGFR |
| `CONDITION` | 당뇨, 임신 여부, 고혈압 |
| `MEDICATION` | metformin 복용, insulin 중단 |
| `PROCEDURE` | 수술, 검사 |
| `ADVERSE_EVENT` | 저혈당, 어지러움, 두통 |
| `SURVEY_ANSWER` | 추가 설문 답변 |

## 5. Graph RAG 모델

RAG는 그래프 기반으로 구축한다. 단순 문서 검색이 아니라 환자, 임상 이벤트, 공고 기준, 표준문서를 그래프로 연결한다.

그래프 노드:

| 노드 | 설명 |
|------|------|
| `Patient` | 비식별 환자 |
| `UserProfile` | 관심 임상, 지역, 기본 정보 |
| `ClinicalEvent` | 검사, 진단, 약물, 설문 답변 |
| `Trial` | Medi25 임상시험 공고 |
| `Criterion` | 선정/제외 기준 |
| `StandardConcept` | ICD, LOINC, ATC, 내부 용어 |
| `EvidenceDocument` | EMR 문장, 공고 원문, 표준문서 chunk |

그래프 관계:

| 관계 | 설명 |
|------|------|
| `HAS_PROFILE` | 사용자가 프로필을 가짐 |
| `HAS_EVENT` | 환자가 임상 이벤트를 가짐 |
| `MATCHES_CONCEPT` | 이벤트가 표준 개념에 매핑됨 |
| `HAS_CRITERION` | 임상시험이 기준을 가짐 |
| `REQUIRES` | 기준이 특정 임상 조건을 요구 |
| `SUPPORTED_BY` | 판정이 근거에 의해 지지됨 |
| `CONTRADICTED_BY` | 판정이 근거와 충돌함 |

사용 AWS:

- Amazon Neptune: 환자 타임라인 및 기준 그래프
- Bedrock Knowledge Bases: 비식별 문서 검색
- OpenSearch Serverless: 벡터/키워드 검색
- S3: 원문 및 정규화 데이터 저장

## 6. 에이전트 모델

에이전트는 RAG, 공고 API, 진료기록 판단을 조합한다.

| 에이전트 | 역할 |
|----------|------|
| `OrchestratorAgent` | 전체 실행 순서 제어 |
| `NoticeAgent` | Medi25 공고 API/크롤러 결과 조회 |
| `CriteriaAgent` | 공고 기준 해석 |
| `RagAgent` | Graph RAG와 표준문서 검색 |
| `MedicalRecordAgent` | EHR/EMR 근거 판단 |
| `QuestionAgent` | 부족한 정보에 대한 질문 생성 |
| `DebateAgent` | `UNKNOWN` 기준에 대해 A2A 토론 |
| `VerifierAgent` | 모델 판단 검증 및 최종 상태 확정 |

### 에이전트 오케스트레이션 상세

오케스트레이터는 한 번에 LLM에게 모든 판단을 맡기지 않는다. 먼저 어떤 데이터가 최신인지 확인하고, 후보 공고를 좁힌 뒤, 기준별로 필요한 도구를 호출한다.

```mermaid
sequenceDiagram
    participant UI as User/Admin UI
    participant API as Matching API
    participant ORCH as OrchestratorAgent
    participant NOTICE as NoticeAgent
    participant RAG as RagAgent
    participant EMR as MedicalRecordAgent
    participant LLM as Bedrock LLM
    participant VERIFY as VerifierAgent
    participant Q as QuestionAgent
    participant DB as DB/S3/Neptune

    UI->>API: Refresh 또는 매칭 시작
    API->>ORCH: run_id 생성 및 실행 요청
    ORCH->>NOTICE: 최신 공고/기준 조회
    NOTICE->>DB: TrialNotice/Criteria 조회
    ORCH->>EMR: 환자 임상 이벤트 조회
    EMR->>DB: PatientClinicalEvent 조회
    ORCH->>RAG: 기준별 근거 검색
    RAG->>DB: Bedrock KB/OpenSearch/Neptune 조회
    ORCH->>LLM: 기준별 판단 요청
    LLM-->>ORCH: OK/NOT_OK/UNKNOWN 제안 JSON
    ORCH->>VERIFY: 근거/규칙 검증
    VERIFY-->>ORCH: 최종 기준별 상태
    ORCH->>Q: UNKNOWN 기준 질문 생성 요청
    Q-->>ORCH: 추가 설문 JSON
    ORCH->>DB: 결과/보고서/A2A 로그 저장
    ORCH-->>API: 추천 결과 반환
```

오케스트레이션 단계:

1. `run_id`를 만들고 실행 이력을 저장한다.
2. Medi25 공고 DB에서 최신 모집공고와 기준 버전을 가져온다.
3. 사용자의 관심 질환, 지역, 성별, 나이로 1차 후보를 좁힌다.
4. 후보 임상시험별 선정/제외 기준을 불러온다.
5. 환자 임상 이벤트와 EMR 근거를 조회한다.
6. Graph RAG에서 기준별 관련 근거를 가져온다.
7. Bedrock LLM이 기준별 상태를 제안한다.
8. Verifier가 근거 출처, 날짜, 단위, 기준 연산자를 검증한다.
9. `UNKNOWN`은 질문 생성 또는 A2A 토론으로 넘긴다.
10. 결과 JSON과 보고서를 저장한다.

### 에이전트 Tool 계약

LLM이 직접 DB를 읽는 것이 아니라, 허용된 Tool을 통해서만 근거를 가져온다.

| Tool | 입력 | 출력 |
|------|------|------|
| `refresh_trial_notices` | `keyword`, `force_refresh` | 신규/변경 공고 목록 |
| `get_trial_notice` | `trial_id` | 공고 정규화 JSON |
| `get_trial_criteria` | `trial_id`, `criteria_version` | 선정/제외 기준 JSON |
| `search_rag_evidence` | `patient_key`, `criterion_id`, `query` | 근거 문장 목록 |
| `query_timeline_graph` | `patient_key`, `field`, `time_window` | 구조화 임상 이벤트 |
| `evaluate_rule` | `criterion`, `observations` | 규칙 기반 판정 |
| `save_matching_report` | `run_id`, `result_json` | 보고서 저장 위치 |

Tool 호출 권한:

- `NoticeAgent`: 공고 API와 공고 DB만 접근한다.
- `RagAgent`: RAG/Graph 검색만 접근한다.
- `MedicalRecordAgent`: 비식별 환자 임상 이벤트만 접근한다.
- `QuestionAgent`: 기준별 `UNKNOWN` 결과와 사용자 프로필 일부만 접근한다.
- `VerifierAgent`: 모든 근거 번들을 읽을 수 있지만 원본 개인정보는 읽지 않는다.

## 7. 판단 모델

각 임상시험 기준은 다음 세 가지 상태 중 하나로 판정한다.

| 상태 | 의미 |
|------|------|
| `OK` | 기준을 충족한다 |
| `NOT_OK` | 기준을 충족하지 못하거나 제외 기준에 걸린다 |
| `UNKNOWN` | 정보가 부족하거나 판단이 애매하다 |

판정 규칙:

- 선정 기준이 `OK`이면 통과 후보로 본다.
- 선정 기준이 `NOT_OK`이면 추천 점수를 크게 낮추거나 제외한다.
- 제외 기준이 `NOT_OK`이면 해당 임상시험은 제외한다.
- `UNKNOWN`은 추가 질문 또는 A2A 토론으로 해소한다.
- A2A 후에도 해결되지 않으면 사람 검토 큐로 보낸다.

### LLM 판단 방식

LLM은 최종 판정을 단독으로 확정하지 않고, 기준별 상태를 제안한다. 최종 확정은 `VerifierAgent`와 규칙 평가기가 담당한다.

LLM 입력:

```json
{
  "task": "evaluate_trial_criterion",
  "patient_key": "pt_7f3a",
  "trial": {
    "trial_id": "medi25-10713",
    "title": "당뇨병 대상 임상시험"
  },
  "criterion": {
    "criterion_id": "inc_hba1c_001",
    "type": "INCLUSION",
    "raw_text": "최근 HbA1c 7.0% 이상",
    "field": "hba1c",
    "operator": ">=",
    "value": 7.0,
    "unit": "%",
    "time_window_days": 180
  },
  "evidence": [
    {
      "evidence_id": "emr.note_20260720_01",
      "source_type": "EMR",
      "observed_at": "2026-07-20",
      "text": "HbA1c 7.8%",
      "structured_value": 7.8,
      "unit": "%"
    }
  ],
  "allowed_status": ["OK", "NOT_OK", "UNKNOWN"]
}
```

LLM 출력:

```json
{
  "criterion_id": "inc_hba1c_001",
  "proposed_status": "OK",
  "confidence": 0.91,
  "reason": "최근 180일 이내 HbA1c 7.8% 근거가 있어 기준을 충족합니다.",
  "used_evidence_ids": ["emr.note_20260720_01"],
  "missing_information": [],
  "needs_a2a": false
}
```

Verifier 검증 항목:

| 검증 항목 | 설명 |
|-----------|------|
| 근거 존재 | `used_evidence_ids`가 실제 검색 결과에 존재하는가 |
| 시간 범위 | 검사일/관찰일이 기준의 기간 조건 안에 있는가 |
| 단위 | HbA1c `%`, 혈당 `mg/dL`처럼 단위가 맞는가 |
| 연산자 | `>=`, `<=`, `between`, `exists` 조건이 맞게 적용됐는가 |
| 기준 유형 | 선정 기준과 제외 기준의 의미가 뒤집히지 않았는가 |
| 개인정보 | 출력에 직접 식별 정보가 포함되지 않았는가 |
| 신뢰도 | 낮은 confidence는 `UNKNOWN` 또는 Human Review로 보낼 것인가 |

최종 상태 확정 규칙:

| 상황 | 최종 상태 |
|------|-----------|
| LLM 제안이 `OK`이고 규칙 검증도 통과 | `OK` |
| LLM 제안이 `NOT_OK`이고 충돌 근거가 명확 | `NOT_OK` |
| 근거가 없거나 날짜/단위가 부족 | `UNKNOWN` |
| LLM 제안과 규칙 결과가 충돌 | `UNKNOWN` 후 A2A |
| A2A 후에도 합의 불가 | `UNKNOWN` 또는 Human Review |

## UNKNOWN 처리 모델

```mermaid
flowchart TD
    U["UNKNOWN 발생"] --> C{"원인"}
    C -->|환자 정보 부족| Q["LLM 질문 생성"]
    C -->|기록 간 충돌| D["A2A 토론"]
    C -->|공고 기준 애매| D
    C -->|표준용어 매핑 불확실| D
    Q --> A["사용자 추가 설문"]
    A --> R["재판정"]
    D --> J{"합의 가능?"}
    J -->|가능| R
    J -->|불가능| H["Human Review"]
```

추가 질문 예시:

```json
{
  "question_id": "q_pregnancy_001",
  "criterion_id": "exc_pregnancy_001",
  "reason": "최근 임신 여부를 확인할 근거가 없습니다.",
  "question": "현재 임신 중이거나 임신 가능성이 있나요?",
  "answer_type": "YES_NO"
}
```

## A2A 토론 모델

A2A는 여러 역할의 에이전트가 같은 `UNKNOWN` 기준을 다른 관점에서 검토하는 구조다.

| 역할 | 관점 |
|------|------|
| `EvidenceAgent` | 환자 근거가 실제로 존재하는가 |
| `CriteriaAgent` | 공고 기준 해석이 맞는가 |
| `StandardAgent` | 표준문서/용어 매핑이 맞는가 |
| `SkepticAgent` | 시간 범위, 단위, 근거 부족 문제가 있는가 |
| `JudgeAgent` | 토론 결과를 정리하고 다음 액션을 결정 |

A2A 결과:

```json
{
  "debate_id": "debate_001",
  "criterion_id": "exc_pregnancy_001",
  "initial_status": "UNKNOWN",
  "final_recommendation": "ASK_USER",
  "agents": [
    {
      "role": "EvidenceAgent",
      "claim": "최근 임신 여부 기록이 없습니다.",
      "confidence": 0.82
    },
    {
      "role": "SkepticAgent",
      "claim": "기록 부재만으로 임신 아님을 확정할 수 없습니다.",
      "confidence": 0.91
    }
  ],
  "question": "현재 임신 중이거나 임신 가능성이 있나요?"
}
```

## 최종 추천 결과

```json
{
  "run_id": "run_20260804_001",
  "patient_key": "pt_7f3a",
  "recommended_trials": [
    {
      "trial_id": "medi25-10713",
      "title": "당뇨병 대상 임상시험",
      "rank_score": 0.78,
      "overall_status": "NEEDS_MORE_INFO",
      "criteria": [
        {
          "criterion_id": "inc_age_001",
          "status": "OK",
          "reason": "나이 기준을 충족합니다.",
          "evidence_ids": ["profile.birth_year"]
        },
        {
          "criterion_id": "inc_hba1c_001",
          "status": "OK",
          "reason": "최근 HbA1c 7.8%가 확인되었습니다.",
          "evidence_ids": ["emr.note_20260720_01"]
        },
        {
          "criterion_id": "exc_pregnancy_001",
          "status": "UNKNOWN",
          "reason": "최근 임신 여부 근거가 없습니다.",
          "next_question": "현재 임신 중이거나 임신 가능성이 있나요?"
        }
      ]
    }
  ]
}
```

## Amazon Bedrock 사용 계획

| 기능 | Bedrock 사용 |
|------|--------------|
| 공고 기준 추출 | Bedrock Converse API |
| EMR 자유서술 해석 | Bedrock Converse API |
| 질문 생성 | Bedrock Converse API |
| RAG 검색 | Bedrock Knowledge Bases |
| 임베딩 | Amazon Titan Embeddings |
| 안전장치 | Bedrock Guardrails |
| 에이전트 오케스트레이션 | Bedrock Agents 또는 자체 Orchestrator |

## 구현 우선순위

1. Medi25 공고 JSON 스키마 확정
2. 사용자 프로필 JSON 스키마 확정
3. EHR/EMR 임상 이벤트 JSON 스키마 확정
4. 임상시험 기준 JSON 스키마 확정
5. `OK`, `NOT_OK`, `UNKNOWN` 판정 규칙 정의
6. Graph RAG 노드/엣지 모델 정의
7. Bedrock tool-use 에이전트 계약 정의
8. `UNKNOWN` 질문 생성 모델 구현
9. A2A 토론 결과 JSON 구현
10. 최종 추천 결과 API 구현

## 설계 원칙

- LLM은 판단을 돕지만, 근거 없는 결론을 내리지 않는다.
- 모든 판정은 JSON 근거와 출처 ID를 남긴다.
- 개인정보와 임상 판단용 데이터는 분리한다.
- `UNKNOWN`은 실패가 아니라 추가 정보 수집 상태다.
- A2A는 애매한 판단을 줄이는 검증 절차다.
- 같은 입력과 같은 기준 버전에서는 같은 결과가 나와야 한다.
