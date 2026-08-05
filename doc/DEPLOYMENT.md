# 배포 현황

`doc/ARCHITECTURE_V2.md` 의 아키텍처를 AWS 계정 `869515803312` 에 올린 결과다.
최초 배포일: 2026-08-05.

## 접속 주소

| 대상 | 주소 |
|------|------|
| 프론트엔드 (CloudFront) | https://d2sajombqek5ru.cloudfront.net |
| 프론트엔드 (Amplify) | https://main.d6z9y2cn01rt.amplifyapp.com |
| Matching API | https://3fotb3ilzmnopf6gvflmequl440rmaxi.lambda-url.ap-northeast-2.on.aws |
| 헬스체크 | 위 주소 + `/health` |

프론트엔드를 두 벌 둔 이유는 배포 경로를 둘 다 살려두기 위해서다. CloudFront
쪽은 CDK 로 인프라까지 관리되고, Amplify 쪽은 zip 수동 배포다. 둘 다 같은
번들을 보고 같은 API 를 부른다.

## 리전 구성

기본은 서울(`ap-northeast-2`)이고, **GraphRAG 계층만 `us-west-2`** 에 있다.

선택이 아니라 제약이다. Neptune Analytics 는 2026년 1월부터 서울에서 쓸 수
있지만, Bedrock Knowledge Bases 가 서울에서 아직 `NEPTUNE_ANALYTICS` 스토리지
타입을 받지 않는다. 서울에 그대로 배포하면 그래프는 만들어지고 KB 생성만
실패한다.

```
KnowledgeBase storage type NEPTUNE_ANALYTICS is not supported.
(Service: BedrockAgent, Status Code: 400)
```

리전이 갈리면서 생긴 연결 세 가지 — 이걸 빠뜨리면 배포는 되는데 런타임에 조용히
깨진다.

| 연결 | 값 | 없으면 |
|------|-----|--------|
| Sanitizer → KB 소스 버킷 | `S3_RAG_BUCKET_NAME`, `S3_RAG_REGION` | 문서가 서울에 쌓이고 KB 가 못 읽는다 |
| API → KB Retrieve | `KNOWLEDGE_BASE_REGION` | 서울에서 KB 를 찾다가 실패한다 |
| Step Functions → 색인 시작 | 리소스 ARN 의 리전 자리 | 색인 단계가 죽는다 |

리전을 넘어가는 것은 **비식별화를 마친 문서뿐**이다. 직접 식별자는
`lambdas/sanitizer/graphrag_documents.py` 이전 단계에서 떨어져 나가고 서울
`raw/` 에만 남는다. sanitizer 를 고칠 때 이 순서를 바꾸면 리전 분리가 곧
개인정보 국외 이전이 된다.

서울에서 KB 가 열리면 `backend/app.py` 의 `GRAPHRAG_REGION` 을 되돌리고 위 세
환경 변수를 걷어내면 된다.

## 스택 구성

### 서울 (ap-northeast-2)

| 스택 | 내용 |
|------|------|
| `HealthcareAuthStack` | Cognito User Pool `ap-northeast-2_M9srfkUaM`, SPA 클라이언트, `admin` 그룹 |
| `HealthcareS3Stack` | KMS 암호화 데이터 레이크 (`raw/`, `rag/`, `trials/`) |
| `HealthcareObservabilityStack` | CloudWatch 대시보드, SNS 알림 토픽 |
| `HealthcareMainStack` | DynamoDB `CriteriaStore`, sanitizer/protocol-parser Lambda, EMR·Trials Step Functions, EventBridge 규칙, DLQ |
| `HealthcareGuardrailStack` | Bedrock Guardrail `8u7lmyrfbox7` v1 (PII 차단, 의료 조언 억제) |
| `HealthcareApiStack` | FastAPI on Lambda + Function URL |
| `HealthcareFrontendStack` | S3 + CloudFront (OAC, SPA 403/404 → index.html) |

### us-west-2

| 스택 | 내용 |
|------|------|
| `HealthcareGraphRagStack` | Neptune Analytics 그래프 `g-6wg5ngdvv8` (16 m-NCU), Bedrock KB `JEGPQXQUUG`, 데이터소스 `IYNGIUDBRW`, 전용 KMS + S3 |

## 배포 방법

두 단계다. 리전이 갈려서 CloudFormation 의 Export/ImportValue 로 스택을 잇지
못하기 때문에, GraphRAG 를 먼저 올리고 그 출력값을 컨텍스트로 넘긴다.

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 최초 1회 — 두 리전 모두 bootstrap
cdk bootstrap aws://869515803312/ap-northeast-2
cdk bootstrap aws://869515803312/us-west-2

# 1단계 — GraphRAG (us-west-2). 그래프 생성에 5분쯤 걸린다
cdk deploy HealthcareGraphRagStack

# 2단계 — API Lambda 패키지 빌드 (Docker 불필요)
scripts/build_api_asset.sh

# 3단계 — 나머지. 1단계 출력값을 컨텍스트로 넘긴다
cdk deploy --all --require-approval never \
  -c knowledge_base_id=JEGPQXQUUG \
  -c data_source_id=IYNGIUDBRW \
  -c graphrag_bucket=healthcaregraphragstack-graphragsourcebucket3586a3-ldn0ob6jwaw4 \
  -c guardrail_id=8u7lmyrfbox7 \
  -c guardrail_version=1 \
  -c "frontend_origin=https://d2sajombqek5ru.cloudfront.net,https://main.d6z9y2cn01rt.amplifyapp.com"
```

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
| Neptune Analytics 16 m-NCU (us-west-2) | 시간당 $0.581 · 하루 약 $14 · 월 약 $418 |
| 나머지 (Lambda, DynamoDB, S3, CloudFront, Cognito, Step Functions) | 사용량 기반. 데모 수준이면 월 $1~5 |
| Bedrock | 토큰 사용량 기반 |

그래프는 **중지할 수 없다. 삭제만 가능하다.** 데모가 끝나면 반드시 지운다.

```bash
cd backend && cdk destroy HealthcareGraphRagStack
```

`RemovalPolicy` 를 RETAIN 으로 두지 않은 이유가 이것이다. 첫 배포에서 롤백이
났을 때 RETAIN 때문에 빈 그래프가 남아 계속 청구된 적이 있다.

## 알려진 제약

### 시드 데이터셋

배포된 API 는 **자리표시자 데이터**를 읽는다. 실제 합성 EMR 데이터셋
(`outputs/longitudinal_emr_v2/`)은 레포 밖에서 공유되고 `.gitignore` 로 빠져
있어 배포 패키지에 넣을 것이 없었다. `DatasetRepository` 는 기동 시점에 CSV
7종을 읽고, 없으면 `FileNotFoundError` 로 콜드 스타트가 통째로 죽는다.

그래서 `backend/scripts/generate_seed_dataset.py` 가 스키마만 같은 최소
데이터를 만든다 — 환자 3명, 방문 9건, 공고 2건. 실제 임상 데이터가 아니다.
모든 행에 `data_label=SEED_PLACEHOLDER` 가 붙어 있다.

진짜 데이터셋이 준비되면 `EMR_DATA_DIR` 를 그쪽으로 돌리거나
`backend/build/api_lambda/dataset/` 을 덮어쓰고 다시 배포한다.

`backend/api/tests/` 는 실제 데이터셋(환자 348명)을 전제로 쓰여 있어 시드로는
통과하지 않는다.

### GraphRAG 색인이 비어 있다

KB 는 만들어졌고 API 도 붙었지만(`rag_status: bedrock_graphrag`), 아직 문서를
넣지 않았다. `rag/patients/` 에 비식별 문서가 올라가고 EMR 파이프라인이 색인을
돌려야 실제 근거 검색이 동작한다. 그전까지 매칭은 구조화 데이터만 보고
`NEEDS_MORE_EVIDENCE` 를 자주 낸다.

### 아직 없는 것

`doc/ARCHITECTURE_V2.md` 에 있으나 이번 배포에 포함하지 않은 것들이다.

| 항목 | 사정 |
|------|------|
| ElastiCache for Redis | VPC + NAT 구성이 필요해 범위에서 뺐다. 현재 지원서 세션은 Lambda 메모리(`IntakeStore`)에 있어 콜드 스타트마다 사라진다 |
| `TrialNoticeTable` 등 DynamoDB 6종 | `CriteriaStore` 하나만 있다. 나머지는 스키마 확정 전 |
| EventBridge Scheduler (크롤러 정기 실행) | 크롤러 Lambda 패키징이 먼저 필요하다 |

## 검증 결과 (2026-08-05)

| 항목 | 결과 |
|------|------|
| `/health` | `status: ok`, `rag_status: bedrock_graphrag`, `graph_status: configured`, `auth_mode: cognito` |
| 미인증 `/api/v1/*` | 401 |
| Cognito SRP 로그인 → 인증 API | 200 |
| 관리자 전용 라우트 (일반 계정) | 403 |
| `POST /screening/run` (Bedrock 실호출) | 200, 54초 |
| CORS 프리플라이트 (CloudFront·Amplify) | 두 출처 모두 허용 |
| SPA 딥링크 (`/results/...`) | 양쪽 200 |
| Lambda 로그 오류 | 없음 |

## 운영 메모

- **관리자 권한 부여**는 Cognito 그룹으로 한다. 셀프 가입으로는 들어올 수 없다.

  ```bash
  aws cognito-idp admin-add-user-to-group \
    --user-pool-id ap-northeast-2_M9srfkUaM --username <email> --group-name admin
  ```

- **환자 데이터 접근**은 계정의 `custom:person_id` 로 묶인다. 이 속성이 없으면
  본인 데이터에도 403 이 난다.

- **API 로그**: `/aws/lambda/healthcare-matching-api` (30일 보존)

- **`AUTH_REQUIRED=true`** 가 이 배포의 유일한 인증 방어선이다. Function URL 의
  AuthType 이 NONE 이라(브라우저가 SigV4 서명을 못 한다) 이 값을 끄면 API 가
  그대로 공개된다.
