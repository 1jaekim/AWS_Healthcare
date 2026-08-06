# 실제 구축 아키텍처 (as-built)

이 문서는 **지금 AWS에 실제로 배포되어 있는 것**을 기록한다. 목표 설계는
`ARCHITECTURE_V2.md`, 판정 모델 설계는 `MATCHING_MODEL_V2.md`에 있고 이 문서는
그것들과 다를 수 있다. 다른 부분은 마지막 절에 모아 두었다.

- 확인 시점: 2026-08-06
- 계정: `869515803312`
- 확인 방법: CloudFormation·DynamoDB·S3·Lambda·Bedrock·Cognito API 직접 조회

## 리전 구성

| 리전 | 역할 | 스택 |
|------|------|------|
| 서울 `ap-northeast-2` | 서비스 본체 | Api, Main, Frontend, Guardrail, Observability, S3, Auth |
| 버지니아 `us-east-1` | **GraphRAG (운영)** | GraphRag |
| 오리건 `us-west-2` | GraphRAG (구버전, 정리 대상) | GraphRag |

GraphRAG만 서울 밖에 있다. 선택이 아니라 제약이다 — Bedrock Knowledge Bases의
GraphRAG(`NEPTUNE_ANALYTICS` 스토리지)는 서울에서 제공되지 않는다.
[지원 리전](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-build-graphs.html)은
프랑크푸르트·런던·아일랜드·오리건·버지니아·도쿄·싱가포르뿐이다.

따라서 서울 → 버지니아 교차 리전 호출이 하나 있다. 그 경계는 두 환경 변수로
명시한다.

```
KNOWLEDGE_BASE_REGION = us-east-1
S3_RAG_REGION         = us-east-1
```

## 전체 구성

```mermaid
flowchart TD
    subgraph SEOUL["서울 ap-northeast-2"]
        subgraph EDGE["진입"]
            CF["CloudFront + S3<br/>프론트엔드 (React SPA)"]
            COG["Cognito<br/>ap-northeast-2_M9srfkUaM"]
            APIFN["Lambda Function URL<br/>healthcare-matching-api"]
        end

        subgraph INGEST["공고 수집·기준 추출"]
            CRAWL["Lambda<br/>healthcare-trial-crawler<br/>12시간 스케줄"]
            S3RAW["S3 데이터 버킷<br/>raw/ · trials/"]
            PARSER["Lambda<br/>healthcare-protocol-parser"]
            SAN["Lambda<br/>healthcare-sanitizer"]
            SYNC["Lambda<br/>healthcare-graphrag-ingestion"]
            SFN["Step Functions<br/>EMR · Trials 파이프라인"]
        end

        subgraph STORE["저장"]
            CRIT["DynamoDB<br/>CriteriaStore"]
            APPS["DynamoDB<br/>ApplicationStore (TTL)"]
            MEM["인메모리<br/>RunStore · AuditTrail"]
        end

        GUARD["Bedrock Guardrail<br/>8u7lmyrfbox7 v1"]
        SNS["SNS 알림 · CloudWatch"]
    end

    subgraph VA["버지니아 us-east-1"]
        RAGS3["S3 GraphRAG 소스<br/>rag/"]
        KB["Bedrock Knowledge Base<br/>VZIL9VWWKP"]
        NEP["Neptune Analytics<br/>그래프 + 벡터"]
    end

    BR["Bedrock Converse<br/>Claude Sonnet 4.5 (global)"]

    CF --> COG --> APIFN
    APIFN --> CRIT
    APIFN --> APPS
    APIFN --> MEM
    APIFN --> BR
    APIFN --> KB
    BR -.- GUARD

    CRAWL --> S3RAW
    S3RAW --> PARSER --> CRIT
    SFN --> PARSER
    SFN --> SAN
    SAN --> RAGS3
    SYNC --> KB
    RAGS3 --> KB --> NEP
    SFN --> SNS
```

## 저장소별 데이터

### DynamoDB `CriteriaStore` (서울)

공고에서 추출한 선정·제외 기준. 판정의 기준 원본이다.

| 항목 | 값 |
|------|-----|
| 파티션 키 | `trial_id` |
| 정렬 키 | `source_key` (S3 원본 경로) |
| GSI | `status-index` (`status` + `created_at`) |
| 현재 항목 수 | 18 |

필드: `trial_id`, `source_key`, `status`, `trial_title`, `phase`, `condition`,
`intervention`, `inclusion_criteria`(JSON 문자열), `exclusion_criteria`,
`inclusion_count`, `exclusion_count`, `created_at`, `updated_at`

`status`는 `pending_review` → `approved` 로 넘어간다. 매칭은 `approved` 만 쓴다.

### DynamoDB `ApplicationStore` (서울)

지원서 스키마와 작성 세션. `record_id` 접두어로 세 종류를 구분한다.

| `record_id` 형식 | 내용 |
|------|------|
| `NOTICE-FIELDS-<공고>-<해시>` | 공고에서 뽑은 동적 질문 필드 |
| `APP-SCHEMA-<공고>-<해시>` | 확정된 JSON Schema (내용 해시로 버전 고정) |
| `APP-<uuid>` | 지원자 작성 세션과 답변 |

필드: `record_id`, `record_type`, `payload`, `updated_at`, `expires_at`

**`expires_at` 으로 TTL이 걸려 있다.** 목표 설계가 ElastiCache에 맡기려던 개인정보
임시 보관을 DynamoDB TTL로 대신하고 있다. ElastiCache는 배포되지 않았다.

### S3

| 버킷 | 리전 | 경로 | 내용 |
|------|------|------|------|
| `healthcares3stack-healthcaredatabucket...` | 서울 | `raw/` | 원본 입력 |
| 같은 버킷 | 서울 | `trials/documents/*.txt` | 공고 텍스트 (기준 추출 입력) |
| 같은 버킷 | 서울 | `raw/trials/html/*.html` | 공고 감사 원본 (파싱한 바이트 그대로) |
| 같은 버킷 | 서울 | `trials/screenshots/*.png` | 구 스크린샷 (아래 결함 참고, 신규 생성 중단) |
| `healthcaregraphragstack-graphragsourcebucket...96pg` | **버지니아** | `rag/` | GraphRAG 소스 문서 |
| `healthcarefrontendstack-frontendbucket...` | 서울 | `assets/` | SPA 빌드 산출물 |

KMS 암호화. `raw/` 는 Glacier 수명주기 규칙이 걸려 있다.

### Bedrock Knowledge Base (버지니아)

| 항목 | 값 |
|------|-----|
| KB ID | `VZIL9VWWKP` |
| 이름 | `healthcare-public-reference-graphrag` |
| 스토리지 | `NEPTUNE_ANALYTICS` |
| 상태 | ACTIVE |

이름이 `public-reference` 인 것이 중요하다. **공개 공고·표준문서 검색용이고 환자
개인 근거 저장소가 아니다.** 그래서 API의 검색 호출에 환자 가명 키 필터나
Secrets Manager 조회가 필요하지 않다.

### 인메모리 (영속화 안 됨)

| 대상 | 구현 |
|------|------|
| 매칭 실행 이력 | `app/persistence/run_store.py` `RunStore` |
| 판정 보고서·근거 패킷 | 같은 클래스 |
| 검토 큐 (Human Review) | 같은 클래스 |
| 감사 로그 | `app/persistence/audit.py` `AuditTrail` |

`container.py` 에 `run_store = RunStore()` 로 고정되어 있어 환경 변수로 교체할 수
없다. Lambda 컨테이너가 재활용되는 동안만 남고 콜드 스타트되면 사라진다.

### Cognito (서울)

| 항목 | 값 |
|------|-----|
| User Pool | `ap-northeast-2_M9srfkUaM` (`trial-matching-users`) |
| 클라이언트 | `12tvibdc3s1lc7f5ep4c5h0dko` |
| 관리자 그룹 | `admin` |
| 사용자 수 | 3 |

`AUTH_REQUIRED=true` 이므로 `/api/v1/*` 는 ID 토큰을 요구한다.

## Lambda 구성

| 함수 | 런타임 | 메모리 | 타임아웃 | 역할 |
|------|--------|--------|----------|------|
| `healthcare-matching-api` | python3.12 | 2048 MB | 300s | FastAPI. 판정·지원서·추천 API |
| `healthcare-protocol-parser` | python3.12 | 512 MB | 300s | 공고 → 기준 JSON (Textract + Bedrock) |
| `healthcare-sanitizer` | python3.12 | 1024 MB | 300s | 비식별화, GraphRAG 문서 생성 |
| `healthcare-graphrag-ingestion` | python3.12 | 256 MB | 120s | KB 동기화 작업 실행 |
| `healthcare-trial-crawler` | python3.12 | 512 MB | 600s | 공개 공고 수집 (12시간 스케줄) |
| `healthcare-a2a-evidence-reviewer` | python3.12 | 1024 MB | 120s | A2A 1라운드 근거 검토 |
| `healthcare-a2a-challenge-reviewer` | python3.12 | 1024 MB | 120s | A2A 2라운드 반론 검토 |

A2A 두 함수는 **IAM 역할이 서로 다르다.** 같은 에셋을 쓰지만 런타임과 권한이
독립이다. Function URL 은 `AWS_IAM` 인증이므로 API Lambda 역할만 호출할 수 있고
공개 엔드포인트가 아니다.

`ApiStack` 은 `-c a2a_enabled=true` 로 배포해야 `A2A_REVIEWER_URL`·
`A2A_CHALLENGER_URL` 이 채워진다. 플래그 없이 배포하면 두 값이 빈 문자열로
덮여 **에러 없이 A2A 가 꺼지고** 인프로세스 경로로 되돌아간다.

## 모델과 안전장치

| 항목 | 값 |
|------|-----|
| 모델 | `global.anthropic.claude-sonnet-4-5-20250929-v1:0` |
| 호출 리전 | 서울 (Converse API 실호출 검증 완료) |
| 최대 토큰 | 1536 |
| Guardrail | `8u7lmyrfbox7` v1 |
| Guardrail 정책 | PII(NAME·EMAIL·PHONE·ADDRESS·카드·계좌) BLOCK, 주민번호 정규식 BLOCK, 의료 조언 주제 DENY, PROMPT_ATTACK 입력 HIGH |

`AGE` 는 PII 정책에서 뺐다. 나이는 가려야 할 식별자가 아니라 판정에 쓰는 기준
필드다. `AGE: ANONYMIZE` 를 켜 두면 근거 서술이 이렇게 나온다.

```
"근거에 명시된 나이 {AGE}세는 {AGE}세 이상 조건을 충족함"
```

배포된 A2A Reviewer 응답에서 실제로 재현됐다. 판정은 OK 로 맞고 `source_ids` 도
인용돼서 `V-EVIDENCE` 는 통과하지만, 사람이 검토할 때 이 문장으로는 아무것도
확인할 수 없다.

Guardrail은 현재 `shared_model` 하나에 붙어 판단·검증·토론·설명·지원서 추출에
모두 적용된다. 측정 결과 판정 계층에서는 손실만 발생했다(아래 참고).

## 가입 정보가 판정에 닿는 경로

가입할 때 받은 값을 지원서에서 다시 묻지 않는다. 다만 `Principal` 에 임상 정보를
담지 않는다는 규칙은 유지한다. 검증을 통과한 클레임에서 그때그때 읽어 지원서
`data` 초기값으로만 넣으므로, 판정 사실의 출처는 계속 `APPLICATION` 이다.

| Cognito 속성 | 값 예시 | 지원서 필드 |
|---|---|---|
| `birthdate` | `2002-06-16` | `age` = 24 (요청 시점 계산) |
| `gender` | `M` / `F` | `sex` = `male` / `female` |
| `custom:survey_medication` | `없음` | `current_medications` = `[]` |
| | `있음` · `잘 모름` | **쓰지 않음** |
| `custom:survey_allergy` | `페니실린 알레르기` | `allergies` = `["페니실린 알레르기"]` |
| `custom:survey_purpose` | `이전에 임상시험에 참여한 적이 있습니다` | `prior_trial_participation` = `True` |
| | 나머지 3개 선택지 | **쓰지 않음** |
| `custom:interest_areas` | `당뇨 · 내분비,피부` | **판정 사실 아님.** 추천 동점 정렬에만 |

쓰지 않는 것들에 이유가 있다. `있음` 은 약이 있다는 것만 알려주고 어떤 약인지는
모르므로 목록을 만들 수 없다. `잘 모름` 은 모른다는 뜻이고 없다는 뜻이 아니다.
`처음 임상시험을 찾아봅니다` 는 처음 찾아본다는 뜻이지 참여한 적이 없다는 진술이
아니라서, `False` 로 단정하면 `과거 참여자 제외` 기준에서 방향이 틀린다.

관심 분야를 `diagnosed_conditions` 로 옮기지 않는 이유는 관심이 진단이 아니기
때문이다. `당뇨 · 내분비` 를 골랐다는 것이 당뇨 진단을 뜻하지 않는다. 추천
정렬에서도 후보를 걸러내지 않고 **동점만 가른다.** 정렬 키에서 `trial_id`
알파벳순 앞자리에 들어가므로 판정·적합도·검토 필요 여부가 모두 같을 때에만 순서가
바뀐다. 관심 목록에 없다고 빼버리면 실제로 적격인 공고가 사용자에게 보이지 않고,
임상시험 매칭에서는 그 방향의 실수가 더 나쁘다.

지원자가 답변에 명시하면 그 값이 계정 값을 덮어쓴다. 계정 정보가 낡았을 수 있고
정정할 길을 막으면 안 된다. 어긋난 곳은 `profile_conflicts` 로 응답과 기록에
남는다.

나이를 자연어에서 재추출하던 경로에는 결함이 있었다. 첫 번째 `\d+세` 를 집어서
`5세 아이를 키우는 32세 여성` 의 나이를 5로 확정했고, 그 값이 모델 추출 결과를
덮어썼다. 나이가 5로 굳으면 `18세 이상` 기준에서 방향이 틀린 부적격 판정이 난다.
지금은 제3자 지칭(`아이`·`어머니`·`반려견` 등) 옆의 나이를 후보에서 빼고, 남은
후보가 여럿이면 본인 지칭 단서가 있는 하나만 고르고, 그래도 모호하면 비워 둔다.
비면 계정 생년월일이 채운다.

## 판정 흐름

```
승인된 공고 기준 (CriteriaStore, status=approved)
        +
계정 프로필 (Cognito 클레임) → 지원서 기본값
        +
지원서 JSON (ApplicationStore) → APPLICATION 관찰값으로 승격
        +
GraphRAG 검색 (버지니아 KB, 공개 문서)
        ↓
rule_evaluator          수치·단위·범위 결정론적 계산
        ↓
LLM 판단 (criterion_judge)   OK / NOT_OK / UNKNOWN 제안
        ↓
JudgmentVerifier        7항목 검증
   V-EVIDENCE 근거 존재 · V-WINDOW 시간 범위 · V-UNIT 단위
   V-OPERATOR 연산자 · V-TYPE 기준 유형 · V-PII 개인정보 · V-CONFIDENCE 신뢰도
        ↓
RuleAggregator          최종 상태 확정
   일치+검증통과 → OK/NOT_OK (DECIDED)
   판단↔규칙 충돌 → UNKNOWN (A2A)
   검증 실패·제외기준 근거부족 → UNKNOWN (HUMAN_REVIEW)
        ↓
A2A 토론 (미해소 기준 최대 5개, 2라운드)
        ↓
OK / NOT_OK / UNKNOWN + 근거 패킷
```

모든 Tool 호출은 `ToolGateway`(`app/orchestration/gateway.py`)를 경유한다. Tool이
선언한 권한 밖을 정책으로 허용하려 하면 등록 단계에서 거부된다.

| Tool | 권한 | 모델 노출 |
|------|------|-----------|
| `criteria_tool` | `READ:dynamodb:trial_definitions` | X |
| `evidence_retrieval_tool` | `READ:bedrock:knowledge_base` | O |
| `timeline_graph_tool` | `READ:neptune:patient_timeline` | O |
| `rule_evaluator` | 없음 (계산 전용) | X |

## 측정된 성능

실제 Bedrock 호출로 측정했다. 스크립트는
`backend/scripts/evaluate_adversarial_a2a.py`.

### TrialGPT 전문가 라벨 70건 (2회 실행)

| 지표 | 프롬프트 v1.0 | v1.1 |
|------|---------------|------|
| 정확도 (확정 가능 46건) | 52.2% | 43.5% |
| 합의율 | 87.1% | 72.9% |
| 근거 인용률 | 67.1% | 70.0% |
| 이유 생성률 | 100% | 100% |
| 유효 합의율 | 54.3% | 42.9% |
| 과잉보류 | 22 | 26 |
| 잘못된 확정 | 4 | **0** |

혼동행렬에서 방향 오류(OK↔NOT_OK)는 0건이다. 오류는 전부 보류 쪽으로 샌다.
v1.1은 정확도를 8.7%p 잃고 잘못된 확정을 4건 → 0건으로 줄였다.

### 자체 적대적 세트 44건 (3회 실행)

| 지표 | 값 |
|------|-----|
| 정확도 (확정 가능 24건) | 평균 93.1% (87.5~100%, 폭 12.5%p) |
| 보류율 (보류 정답 20건) | 80% |
| 실행 간 흔들린 케이스 | 4/44 (9.1%) |

`temperature=0` 이라도 실행마다 결과가 바뀐다. 흔들리는 방향은 항상 확정 → 보류다.

### Guardrail을 판정 계층에 붙였을 때

| | 없음 | 전체 평가 | 근거만 평가 |
|---|---|---|---|
| 정확도 | 95.8% | 75.0% | 79.2% |
| 막아낸 위험 | — | 0건 | 0건 |

`guardContent` 로 검사 범위를 근거 서술로 좁혀도 이득이 없었다. **판정 계층에는
Guardrail을 붙이지 않는 것이 측정 결과에 부합한다.** 입력·출력 경계에서는 계속
사용한다.

## 목표 설계와 다른 점

`ARCHITECTURE_V2.md` 대비 미구현·상이 항목이다.

| 목표 | 현재 |
|------|------|
| `TrialNoticeTable` | 없음. `CriteriaStore` 가 공고 메타까지 겸한다 |
| `TrialCriteriaTable` (`trial_id` + `criteria_version`) | `CriteriaStore` 정렬 키가 `source_key` 다. 재파싱이 버전 증가가 아니라 새 항목으로 쌓인다 |
| `UserProfileTable` | 없음. 관심 질환·지역 기반 1차 후보 좁히기도 없다 |
| `PatientClinicalEventTable` | 없음. `timeline_graph_tool` 은 로컬 CSV를 읽으므로 배포 환경에서는 동작하지 않는다 |
| `MatchingRunTable` · `MatchingReportTable` | 없음. 인메모리 |
| `AdminAuditLogTable` | 없음. 인메모리 |
| ElastiCache 임시 처리 계층 | 없음. `ApplicationStore` TTL이 대신한다 |
| `Refresh 모집공고` API | API 는 없지만 `healthcare-trial-crawler` Lambda 가 12시간 스케줄로 대신한다 |
| 공고 최신성 확인 (`content_hash`, `last_checked_at`) | 없음. S3 키를 URL 해시로 고정해 덮어쓰는 방식이라 중복은 막지만 변경 감지는 못 한다 |
| 독립 A2A Lambda | 서울 Reviewer·Challenger 런타임으로 운영 배포 |
| — | `ApplicationStore` 는 목표 설계에 없던 추가 구성이다 |

## 알려진 결함

### 1. 스크린샷 기준 추출이 전부 실패한다

`protocol_parser` 가 `textract.detect_document_text` 를 쓰는데 **이 API는 한국어를
지원하지 않는다.** 한글 공고 스크린샷을 넣으면 글자를 라틴 문자로 오인식한다.

| 입력 | 건수 | 기준 0건 | 평균 기준 수 |
|------|------|----------|--------------|
| `trials/documents/*.txt` | 7 | 0 | 18.3 |
| `trials/screenshots/*.png` | 11 | 5 | 0.8 |

같은 공고를 두 경로로 넣은 결과가 이렇게 갈린다.

```
documents/kct_695402a17ea5.txt        4,629자  기준 26건  한글 정상
screenshots/kct_695402a17ea5_...png    514자  기준  0건  "YYXOB", "Polly olo" 등
```

2차 문제가 더 위험하다. 핸들러의 차단 조건이 `len(text) < 50` 이라 514자는
통과하고, 빈 기준이 `status=pending_review` 로 저장되어 **정상 항목처럼 보인다.**
같은 `trial_id` 에 정상 항목과 망가진 항목이 나란히 존재하므로 매칭이 후자를
집으면 판정 불가가 된다.

**조치 (완료)**: 신규 수집은 스크린샷을 만들지 않는다. 감사 원본을 PNG 대신
HTML(`raw/trials/html/`)로 남긴다. 우리가 실제로 파싱한 바이트라서 검색·비교가
되고 OCR 문제와 무관하다.

같이 제거한 것이 이 결함의 2차 문제를 만든 장치였다. 예전 크롤러는
`CriteriaStore` 전체를 스캔해 `canonical-trial-id` S3 메타데이터를 만들어
넘겼는데, 그 값이 **스크린샷 경로**에서 나왔다.

```
크롤러: canonical-trial-id = SRC-sha256("trials/screenshots/kct_xxx.png")
파서:   trial_id = canonical or stable_source_trial_id(source_key)
```

그래서 기준 0건인 스크린샷 행과 기준 26건인 문서 행이 **같은 `trial_id` 를
공유했다.** 지금은 파서가 실제로 파싱한 `source_key` 로 ID 를 만든다. 전체 테이블
스캔도 함께 사라졌다.

**남은 작업**: 기존에 오염된 5건 정리. 그리고 기준 0건을 `pending_review` 가
아니라 `NEEDS_FIX` 로 기록하는 처리는 아직 없다.

### 2. GraphRAG 가 오리건에도 남아 있다 (버지니아로 전환 완료)

GraphRAG 운영 리전을 **오리건 `us-west-2` → 버지니아 `us-east-1` 로 바꿨다.**
코드 기본값(`backend/app.py` 의 `GRAPHRAG_REGION`)과 배포된 Lambda 4개의 환경
변수가 모두 버지니아를 가리키는 것을 확인했다. 오리건을 참조하는 값은 없다.

| | 버지니아 `us-east-1` | 오리건 `us-west-2` |
|---|---|---|
| KB | `VZIL9VWWKP` | `JEGPQXQUUG` |
| 이름 | `healthcare-public-reference-graphrag` | `healthcare-patient-evidence-graphrag` |
| Neptune 그래프 | `g-auaz36nmm8` 16 m-NCU | `g-6wg5ngdvv8` 16 m-NCU |
| 수집 작업 | 3건 | 1건 |
| S3 객체 | 4개 / 16,441 B | 2개 / 1,218 B |
| 검색 결과 | 3건 | 1건 |
| 검색 지연 (중앙값, 5회) | 1,532 ms | 1,344 ms |

이름이 다른 것이 핵심이다. 단순 중복 배포가 아니라 **설계가 바뀐 것**이다.
`patient-evidence`(환자 개인 근거) → `public-reference`(공개 참조 문서). 환자
사실은 지원서 JSON 으로 직접 전달하고 리전을 넘는 것은 공개 문서뿐이다.

오리건 내용은 버지니아의 부분집합이다. 오리건에는 합성 환자 파일
(`pt_syn_dm_001.md`, 650 B) 하나뿐이고, 버지니아에는 그것 + 실제 공고 참조 문서
(`rag/references/trials/SRC-30a9f79e59ed9822.md`, 14,322 B)가 있다.

지연시간만 오리건이 188 ms(12%) 빠르다. 서울에서 물리적으로 더 가깝기 때문이다.
다만 이 비교는 공정하지 않다 — 오리건은 문서 1건을 뒤졌고 버지니아는 3건을
돌려줬다. 그리고 판정 한 건은 LLM 호출을 여러 번 하며 각각 수 초가 걸린다. 그
안에서 검색 한 번의 188 ms 는 마이그레이션 비용을 정당화하지 못한다.

**남은 작업**: 오리건 스택 삭제. Neptune Analytics 는 시간당 과금이라 두 리전에서
16 m-NCU 씩, 합계 32 m-NCU 가 계속 돌고 있다. 스택을 지우면 그래프는 삭제되고
(`DeletionPolicy: Delete`) KMS 키와 S3 버킷은 남는다(`Retain`).

### 3. 판정 결과가 영속화되지 않는다

실행 이력·보고서·검토 큐·감사 로그가 인메모리다. 재실행 추적과 사람 검토가
콜드 스타트에서 끊긴다. `ApplicationStore` 가 이미 "같은 계약 + 환경 변수로 교체"
패턴을 쓰고 있으므로 `RunStore` 도 같은 방식으로 DynamoDB 어댑터를 붙일 수 있다.

### 4. 환자 임상 데이터 경로가 비어 있다

`PatientClinicalEventTable` 도 `patient/raw/` 도 없다. 현재 판정 근거는 지원서에서
받은 값과 공개 문서 검색뿐이다.

## 재현 방법

이 문서의 수치는 아래로 다시 확인할 수 있다.

```powershell
# 배포 상태
aws cloudformation list-stacks --region ap-northeast-2
aws dynamodb describe-table --table-name CriteriaStore --region ap-northeast-2
aws lambda get-function-configuration --function-name healthcare-matching-api --region ap-northeast-2

# Bedrock 실호출 점검 (읽기 전용, 모델 호출 비용 발생)
$env:AWS_REGION = "ap-northeast-2"
backend/.venv/Scripts/python agent/scripts/verify_bedrock.py `
  --model-id global.anthropic.claude-sonnet-4-5-20250929-v1:0

# 성능 측정
cd backend
.venv/Scripts/python scripts/evaluate_adversarial_a2a.py --mode local --repeat 3
.venv/Scripts/python scripts/evaluate_adversarial_a2a.py --mode local `
  --source trialgpt --dataset evaluation_data/trialgpt_criterion_sample.jsonl --repeat 2
```
