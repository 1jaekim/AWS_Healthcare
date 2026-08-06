# 배포 현황

`doc/ARCHITECTURE_V2.md` 의 아키텍처를 AWS 계정 `869515803312` 에 올린 결과다.
최초 배포일: 2026-08-05.

> **최종 데이터 원칙 (2026-08-05)**
> 서비스는 병원 EHR/EMR 연동이나 기존 환자 데이터셋을 운영 전제로 사용하지 않는다.
> 매칭 근거는 사용자가 선택한 공고의 승인된 선정·제외 기준과, 사용자가 직접 제출한
> 지원서·추가 답변이다. 아래의 EMR 파이프라인, 시드 환자, 환자 GraphRAG 관련 항목은
> 현재 배포에 남아 있는 레거시 호환 자원이며 신규 워크플로우의 필수 구성요소가 아니다.

## 접속 주소

| 대상 | 주소 |
|------|------|
| 프론트엔드 (CloudFront) | https://d2sajombqek5ru.cloudfront.net |
| 프론트엔드 (Amplify) | https://main.d6z9y2cn01rt.amplifyapp.com |
| Matching API | https://4ycaav2kgzzlcjro24coxhcrk40hhcge.lambda-url.ap-northeast-2.on.aws |
| 헬스체크 | 위 주소 + `/health` |

프론트엔드를 두 벌 둔 이유는 배포 경로를 둘 다 살려두기 위해서다. CloudFront
쪽은 CDK 로 인프라까지 관리되고, Amplify 쪽은 zip 수동 배포다. 둘 다 같은
번들을 보고 같은 API 를 부른다.

## 리전 구성

서비스 본체는 서울 `ap-northeast-2`에 있다. Matching API,
DynamoDB, Cognito, Bedrock Guardrail이 모두 여기 있다.

GraphRAG 계층만 버지니아 `us-east-1`에 있다. 선택이 아니라 제약이다 — Bedrock
Knowledge Bases의 GraphRAG(`NEPTUNE_ANALYTICS` 스토리지)는 서울에서 제공되지
않는다. [지원 리전](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-build-graphs.html)은
프랑크푸르트·런던·아일랜드·오리건·버지니아·도쿄·싱가포르뿐이다.

따라서 서울↔버지니아 교차 리전 호출이 하나 있다. 그 경계는 두 환경 변수로
명시적으로 관리한다.

| 변수 | 값 |
|------|-----|
| `KNOWLEDGE_BASE_REGION` | `us-east-1` |
| `S3_RAG_BUCKET_NAME` | 버지니아 GraphRAG 스택의 소스 버킷 |

빠뜨리면 API가 서울에서 KB를 찾다가 실패한다. 기본값은 서비스 리전이므로 KB를
켤 때는 반드시 함께 넣는다.

리전이 갈리면 CloudFormation의 `Export`/`ImportValue`로 스택을 엮을 수 없다.
그래서 배포가 두 단계다. GraphRAG를 먼저 올리고 그 출력값을 CDK 컨텍스트로 넘겨
서울 스택들을 올린다.

## 스택 구성

### 서울 (ap-northeast-2)

| 스택 | 내용 |
|------|------|
| `HealthcareAuthStack` | Cognito User Pool `ap-northeast-2_M9srfkUaM`, SPA 클라이언트, `admin` 그룹 |
| `HealthcareS3Stack` | KMS 암호화 데이터 레이크 (`raw/`, `rag/`, `trials/`) |
| `HealthcareObservabilityStack` | CloudWatch 대시보드, SNS 알림 토픽 |
| `HealthcareMainStack` | DynamoDB `CriteriaStore`·`IntakeStore`, protocol-parser Lambda, Trials Step Functions, EventBridge 규칙, DLQ. sanitizer·EMR Step Functions는 레거시 호환 자원 |
| `HealthcareGuardrailStack` | Bedrock Guardrail `8u7lmyrfbox7` v1 (PII 차단, 의료 조언 억제) |
| `HealthcareA2AStack` | 서울의 독립 Reviewer·Challenger Lambda. 기본 활성화이며 장애 격리·지연 비교 시 `-c a2a_enabled=false`로 제외 |
| `HealthcareApiStack` | FastAPI on Lambda + Function URL |
| `HealthcareFrontendStack` | S3 + CloudFront (OAC, SPA 403/404 → index.html) |

### 버지니아 (us-east-1)

| 스택 | 내용 |
|------|------|
| `HealthcareGraphRagStack` | Neptune Analytics 그래프 `g-auaz36nmm8` (16 m-NCU), Bedrock KB `VZIL9VWWKP`, 데이터소스 `VKQED58WTX`, 전용 KMS + S3 |

GraphRAG를 서울로 합치는 것은 Bedrock Knowledge Bases가 서울에서 GraphRAG를
지원하는 시점까지 보류한다. 그때는 `graphrag_region` 컨텍스트만 서울로 바꾸고
`KNOWLEDGE_BASE_REGION`·`S3_RAG_BUCKET_NAME`을 걷어내면 된다.

## 배포 방법

서울에 이미 배포된 스택을 갱신하는 절차다. bootstrap은 서울에 되어 있다
(`CDKToolkit`, BootstrapVersion 30).

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 1단계(선택) — GraphRAG. 버지니아에 만든다. 이미 있으면 건너뛴다
cdk bootstrap aws://869515803312/us-east-1      # 버지니아 최초 1회
cdk deploy HealthcareGraphRagStack

# 2단계 — API Lambda 패키지 빌드 (Docker 불필요)
scripts/build_api_asset.sh

# 3단계 — 서울 스택 배포. GraphRAG를 쓸 때만 1단계 출력값을 넘긴다
cdk deploy \
  HealthcareS3Stack HealthcareObservabilityStack HealthcareMainStack \
  HealthcareAuthStack HealthcareGuardrailStack HealthcareFrontendStack \
  HealthcareApiStack \
  --require-approval never \
  -c knowledge_base_id=<KnowledgeBaseId> \
  -c data_source_id=<DataSourceId> \
  -c graphrag_bucket=<GraphRagBucketName>
```

`~/.aws/config`의 기본 리전이 서울이 아니면 `AWS_REGION`·`CDK_DEFAULT_REGION`을
서울로 지정해야 한다. `app.py`의 `PRIMARY_REGION`이 그 환경 변수를 먼저 보므로,
값이 어긋나면 의도와 다른 리전에 배포된다.

### 연구용 A2A 통신 테스트 — 운영 미사용

지연 시간 때문에 A2A는 현재 운영 아키텍처와 배포 절차에서 제외했다. 아래 절차와
코드는 과거 실험을 재현할 때만 사용하며, 운영 배포에서는 실행하지 않는다. 재현 시에는
별도 자산을 먼저 빌드한다. A2A는 기본 활성화이므로 비활성화할 때만 CDK에 `-c a2a_enabled=false`를 명시한다.

AWS 자격증명이 설정된 환경에서 A2A 스택 배포 후 실행한다. 합성 지원자 데이터만
사용하며 EHR/EMR 또는 실제 개인정보를 보내지 않는다.

```bash
cd backend
.venv/bin/python scripts/smoke_a2a_deployment.py \
  --region ap-northeast-2 \
  --stack-name HealthcareA2AStack
```

이 검사는 두 함수의 ARN과 실행 역할이 서로 다른지 확인하고, IAM SigV4로 각 Agent
Card를 조회한 다음 Reviewer 1라운드와 Challenger 2라운드를 실제 JSON-RPC로 호출한다.
두 응답에 `rationale`과 허용된 `source_ids`가 있어야 성공한다.

통신 검사가 끝나면 TrialGPT 전문의 라벨 샘플로 품질 평가를 실행한다.

```bash
cd backend
python scripts/prepare_trialgpt_eval.py --per-label 5
python scripts/evaluate_trialgpt_a2a.py --region ap-northeast-2 --limit 12
```

결과에는 합의 최종 정확도, 두 에이전트 합의율, 출처 그라운딩 비율, 이유 작성률과
케이스별 전문의 기대값·Reviewer·Challenger·최종 합의가 포함된다.

프론트엔드:

```bash
cd frontend
# 빌드 값은 .env.production 에 있다. 셸 환경 변수로만 넘기면 배포 스크립트가
# 내부에서 다시 빌드할 때 전달되지 않아 목업 번들이 나간다.
S3_BUCKET=healthcarefrontendstack-frontendbucketefe2e19c-rdqdnrhrfknq \
CLOUDFRONT_DISTRIBUTION_ID=E3KUG6MVH0Z13Z \
  scripts/deploy-s3.sh

# Amplify 쪽 (zip 수동 배포)
npm run build && (cd dist && zip -qr ../dist.zip .)
DEPLOY=$(aws amplify create-deployment --app-id d6z9y2cn01rt --branch-name main --region ap-northeast-2)
# 응답의 zipUploadUrl 에 PUT 후 start-deployment
```

## 비용

**Neptune Analytics 그래프가 유일한 상시 과금 항목이다.**

| 항목 | 요금 |
|------|------|
| Neptune Analytics 16 m-NCU (us-east-1) | 버지니아 리전 최신 요금 확인 필요 |
| 나머지 (Lambda, DynamoDB, S3, CloudFront, Cognito, Step Functions) | 사용량 기반. 데모 수준이면 월 $1~5 |
| Bedrock | 토큰 사용량 기반 |

그래프는 **중지할 수 없다. 삭제만 가능하다.** 데모가 끝나면 반드시 지운다.

```bash
cd backend && cdk destroy HealthcareGraphRagStack
```

`RemovalPolicy` 를 RETAIN 으로 두지 않은 이유가 이것이다. 첫 배포에서 롤백이
났을 때 RETAIN 때문에 빈 그래프가 남아 계속 청구된 적이 있다.

## 알려진 제약

### 레거시 시드 데이터셋

배포된 API에는 기존 `DatasetRepository`와 환자 단위 API가 콜드 스타트에서 죽지
않도록 `SEED_PLACEHOLDER` 데이터가 포함돼 있다. 환자 3명, 방문 9건, 공고 2건으로
구성되며 실제 임상 데이터나 모델 평가용 정답이 아니다.

최종 제품에서는 이 데이터를 실제 EHR/EMR로 교체하지 않는다. 지원서 완료 JSON을
스크리닝 입력으로 직접 사용하도록 런타임을 전환한 뒤 시드 환자, `EMR_DATA_DIR`,
`person_id` 의존성을 제거한다. 전환 전까지 시드 기반 `/patients`, `/screening/run`
응답은 인프라 연결 확인용으로만 취급한다.

### GraphRAG 검증용 색인

KB와 API 연결은 합성 당뇨 환자 문서 1건으로 색인·검색을 검증했다. 이는 연결 시험용
데이터이며 운영 환자 데이터가 아니다. 최종 워크플로우에서 GraphRAG를 유지한다면
공개 공고·표준문서 검색 보조 용도로 색인 범위를 다시 정의하고, 추천 판정의 Source
of Truth로 사용하지 않는다.

### 아직 없는 것

`doc/ARCHITECTURE_V2.md` 에 있으나 이번 배포에 포함하지 않은 것들이다.

| 항목 | 사정 |
|------|------|
| ElastiCache for Redis | VPC + NAT 구성이 필요해 범위에서 뺐다. 지원서 세션은 현재 DynamoDB `IntakeStore`에 저장하므로 필수 구성요소가 아니다 |
| `TrialNoticeTable` 등 DynamoDB 6종 | `CriteriaStore` 하나만 있다. 나머지는 스키마 확정 전 |
| EventBridge Scheduler (크롤러 정기 실행) | 크롤러 Lambda 패키징이 먼저 필요하다 |

## 검증 결과 (2026-08-05)

| 항목 | 결과 |
|------|------|
| `/health` | `status: ok`, `rag_status: bedrock_graphrag`, `graph_status: configured`, `auth_mode: cognito` |
| 미인증 `/api/v1/*` | 401 |
| Cognito SRP 로그인 → 인증 API | 200 |
| 관리자 전용 라우트 (일반 계정) | 403 |
| `POST /screening/run` (Bedrock 실호출) | 200, 54초. 레거시 시드 환자 기반 연결 검증 |
| CORS 프리플라이트 (CloudFront·Amplify) | 두 출처 모두 허용 |
| SPA 딥링크 (`/results/...`) | 양쪽 200 |
| Lambda 로그 오류 | 없음 |

## 운영 메모

- **관리자 권한 부여**는 Cognito 그룹으로 한다. 셀프 가입으로는 들어올 수 없다.

  ```bash
  aws cognito-idp admin-add-user-to-group \
    --user-pool-id ap-northeast-2_M9srfkUaM --username <email> --group-name admin
  ```

- **`custom:person_id`** 는 현재 레거시 환자 API 접근에만 사용한다. 지원서 중심
  워크플로우는 Cognito `sub` 소유권으로 지원서와 실행을 격리하고, 런타임 전환 후
  `person_id` 바인딩을 제거한다.

- **API 로그**: `/aws/lambda/healthcare-matching-api` (30일 보존)

- **`AUTH_REQUIRED=true`** 가 이 배포의 유일한 인증 방어선이다. Function URL 의
  AuthType 이 NONE 이라(브라우저가 SigV4 서명을 못 한다) 이 값을 끄면 API 가
  그대로 공개된다.

## 무 EHR/EMR 전환 체크리스트

현재 배포를 최종 데이터 원칙에 맞추기 위한 남은 작업이다.

1. `POST /applications/{id}/screening`이 `person_id` 없이 완료 지원서 JSON을 직접
   `ScreeningOrchestrator`에 전달하도록 입력 계약을 변경한다.
2. `DatasetRepository`, `TimelineGraphTool`, 환자 GraphRAG를 필수 의존성에서 제거한다.
3. 실행·추천 로그를 DynamoDB에 영속화하고 Cognito `sub`로 소유권을 검증한다.
4. 레거시 `/patients`, EMR Step Functions, sanitizer, 시드 데이터 패키징을 제거한다.
5. 지원서 기반 `OK/NOT_OK/UNKNOWN` 골든셋과 사용자 흐름 E2E 회귀 테스트로
   교체한다.
