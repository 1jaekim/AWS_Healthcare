# 임상시험 매칭 프론트엔드

Claude Design 프로젝트 [의료 포털 로그인 및 가입 흐름](https://claude.ai/design/p/45fe6342-a7bb-4d76-913e-0364a4c12b55)
의 `로그인 회원가입 UI.dc.html` 시안을 구현한 SPA 입니다. Amplify Hosting 또는
S3 + CloudFront 에 정적으로 배포하는 것을 전제로 구성했습니다.

- Vite + React 18 + TypeScript
- 스타일은 시안의 디자인 시스템(`ds/styles.css`)을 그대로 옮긴 순수 CSS. UI 라이브러리 없음
- 백엔드는 `backend/api` FastAPI (`app/main.py`)

## 실행

```bash
cd frontend
cp .env.example .env
npm install
npm run dev        # http://localhost:3000
```

포트는 3000 으로 고정했습니다. 백엔드 CORS 기본 허용 목록이 3000 이라서,
바꾸려면 `vite.config.ts` 와 백엔드의 `CORS_ALLOW_ORIGINS` 를 같이 바꿔야 합니다.

```bash
npm run build      # tsc --noEmit && vite build → dist/
npm run typecheck
```

## 환경 변수

Vite 는 `VITE_` 접두사 변수를 **빌드 시점에 번들에 굽습니다.** S3·Amplify 는
정적 호스팅이라 배포 후에 값을 바꿀 수 없습니다. 값을 바꾸면 다시 빌드하세요.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `VITE_API_BASE_URL` | `http://127.0.0.1:8000` | 백엔드 FastAPI 주소 |
| `VITE_USE_MOCK` | `true` | `true` 면 네트워크 호출 없이 목업 데이터로 동작 |
| `VITE_DEMO_PERSON_ID` | `1` | 계정이 바인딩될 합성 EMR `person_id` |
| `VITE_AUTH_PROVIDER` | `local` | `cognito` 또는 `local` |
| `VITE_COGNITO_USER_POOL_ID` | — | `cognito` 일 때 필수 |
| `VITE_COGNITO_CLIENT_ID` | — | `cognito` 일 때 필수 |

### 목업 모드가 필요한 이유

백엔드는 `outputs/longitudinal_emr_v2/` 합성 EMR 데이터셋을 읽는데, 이 폴더는
저장소 `.gitignore` 에 걸려 있어 클론한 환경에는 없습니다. 데이터가 없으면
uvicorn 이 기동 단계에서 `FileNotFoundError` 로 죽습니다.

그래서 `src/api/mock.ts` 가 실제 API 와 **같은 타입**을 돌려주는 어댑터로 들어가
있습니다. 데이터셋이 있는 환경에서는 `VITE_USE_MOCK=false` 로 두면 곧장 실제
API 를 탑니다. 화면 코드는 어느 쪽인지 모릅니다.

목업 데이터는 시안의 예시 값(HbA1c 7.8, 임신 여부 확인필요 등)을 옮긴 것이고
실제 판정 결과가 아닙니다.

## 화면과 API 대응

| 경로 | 시안 | 호출하는 백엔드 API |
|---|---|---|
| `/login` | 2a 로그인 | 없음 (아래 "인증" 참고) |
| `/signup` | 2b 회원가입 + 동의 | 없음 |
| `/signup/survey` | 2c 간단 설문 4문항 | 없음 |
| `/signup/done` | 2d 가입 완료 | 없음 |
| `/home` | 5a 로그인 후 홈 | `GET /api/v1/trials`, `POST /api/v1/recommendations/run` |
| `/intake/:trialId` | 6a 서술형 → 6b 확인 챗 | `POST /api/v1/trials/{id}/application-schema`, `POST /api/v1/applications`, `POST /api/v1/applications/{id}/responses`, `POST /api/v1/applications/{id}/screening` |
| `/matching` | 4b 매칭 실행 중 | `POST /api/v1/recommendations/run` |
| `/results`, `/results/:trialId` | 4c 결과 + 기준별 판정 | 추천 응답의 `criteria`, 펼칠 때 `GET /api/v1/screening/{run_id}` |
| `/report` | 4d 최종 보고서 | 추천 응답 전체 |

시안 3a(구버전 홈)와 4e(모바일 전용 화면)는 옮기지 않았습니다. 3a 는 5a 로
대체됐고, 4e 는 같은 화면의 모바일 레이아웃이라 CSS 미디어 쿼리로 처리했습니다.

### 지원서 흐름 (5a → 6a → 6b)

홈에서 공고를 클릭하면 그 공고의 지원서 챗으로 들어갑니다. 시안의 "서술형 한 문항
→ 빠진 정보만 챗으로" 는 백엔드의 자연어 지원서 경로에 그대로 대응합니다.

```
공고 카드 클릭 → /intake/{trial_id}
  → POST /trials/{trial_id}/application-schema   공고 기준 → 지원서 스키마
  → POST /applications                           첫 서술 제출
  → POST /applications/{id}/responses            누락 항목 재질문 (최대 5회)
  → COMPLETE
  → POST /applications/{id}/screening            환자 기록 연결 + 오케스트레이터 실행
  → /matching?trial={trial_id} → /results/{trial_id}
```

묻는 항목은 공고마다 다릅니다. 기본 6항목(나이·성별·질환·약·알레르기·참여경험)에
그 공고의 선정·제외 기준에서 파생된 항목이 붙습니다. 예를 들어 당뇨 경구제 공고는
HbA1c 와 임신 여부를, 골관절염 공고는 통증 점수를 추가로 확인합니다. 1단계 화면이
"이 공고가 추가로 확인하는 항목" 으로 그 목록을 먼저 보여줍니다.

같은 공고면 `schema_id` 가 같으므로 화면이 스키마를 캐시할 필요가 없습니다. 확정
제외된 공고는 지원서를 받지 않고 판정 상세(`/results/{trial_id}`)로 보냅니다.

이 단계는 값을 모을 뿐 적격 여부를 판단하지 않습니다. 판정은 마지막 `screening`
호출부터 시작합니다.

주의할 점 두 가지:

- **빈 배열과 `false` 는 유효한 답변입니다.** "알레르기 없음" → `[]`,
  "참여 경험 없음" → `false` 이며 누락으로 처리되지 않습니다. 화면도 이를
  미응답처럼 보여주지 않습니다.
- **재질문은 실제로 발행한 횟수 기준 5회입니다.** 초과하면
  `MAX_FOLLOW_UPS_REACHED` 로 끝나고 `follow_up_prompt` 가 더 이상 오지 않습니다.
  종료된 세션에 답변을 보내면 백엔드가 `409` 를 돌려줍니다. 화면은 그 시점에
  입력을 막고 사람 확인 안내로 넘어갑니다.

`/intake` 로 공고 없이 들어오면 추천 1순위 공고를 씁니다. 그것도 없으면 공고를 먼저
고르라는 안내를 띄웁니다 — 지원서 항목이 공고 기준에서 만들어지므로 공고 없이는
질문을 구성할 수 없습니다.

스키마 준비는 `POST /trials/{id}/application-schema` 를 씁니다. 이 경로는 LLM 을 부르지
않아 `BEDROCK_ENABLED=false` 인 로컬에서도 동작합니다. 공고문 자유 텍스트에서 필드를
만들어야 하는 경우(기준이 아직 구조화되지 않은 공고)는
`POST /application-schemas` 를 쓰고, 그때는 `additional_fields` 를 명시해야 합니다.

### 적합도 점수는 백엔드 값입니다

`rank_score` 는 `POST /recommendations/run` 이 돌려주는 값을 0~100 으로만 바꾼
것입니다. 확정 `OK=1.0`, 미해소 `UNKNOWN=0.4`, 그라운딩된 A2A 합의 `OK=0.8` 처럼
**코드에 고정된 보수적 점수**이며 모델 생성값이 아닙니다. 프론트는 계산하지
않습니다.

`screening_decision`(원래 판정)과 `recommendation_decision`(추천 정렬용)이 다르면
A2A 교차 검토가 개입한 것입니다. `/results` 는 두 값이 갈릴 때만 그 사실을
드러냅니다.

### 여전히 UI 가 채우는 것

- **공고 메타 한 줄** (지역·방문 횟수) — `trial_definitions.csv` 에 없는 항목이라
  목업에만 있습니다. 실제 API 모드에서는 `trial_id` 로 대체됩니다.
- **매칭 실행 단계 표시** (4b) — 백엔드가 진행 상황을 스트리밍하지 않습니다.
  `POST /recommendations/run` 은 끝난 뒤 한 번에 응답합니다. 화면의 단계는 시간에
  맞춰 넘어가는 설명이지 서버가 알려준 실제 단계가 아니며, 마지막 단계에서 멈춰
  응답을 기다립니다(끝난 척하지 않습니다). 진짜 단계를 보여주려면 백엔드에 SSE 나
  상태 조회 엔드포인트가 필요합니다.

## 인증

아키텍처 v2 의 `Amazon Cognito` 를 구현했습니다. `VITE_AUTH_PROVIDER` 로 두 제공자
중 하나를 고릅니다. 둘 다 같은 `AuthProvider` 인터페이스를 구현하므로 화면 코드는
어느 쪽인지 모릅니다.

| 값 | 구현 | 용도 |
|---|---|---|
| `cognito` | `src/auth/cognitoProvider.ts` | 목표 구성. User Pool 필요 |
| `local` | `src/auth/localProvider.ts` | User Pool 없이 화면을 돌려볼 때 |

### User Pool 만들기

`backend/infra/auth_stack.py` 가 CDK 로 정의합니다. 다른 스택에 의존하지 않아
따로 배포할 수 있습니다.

```bash
cd backend
cdk deploy HealthcareAuthStack
```

출력값 `UserPoolId` · `UserPoolClientId` 를 프론트 환경변수에 넣고 다시 빌드합니다.

User Pool 구성:

- 이메일을 아이디로 사용, 셀프 가입 허용, **이메일 코드 인증**
- 비밀번호 8자 이상 + 영문 소문자 + 숫자 (시안 2b 문구와 같은 정책. 화면에 적힌
  것보다 실제 정책이 빡세면 사용자가 이유를 모른 채 막히므로 기호는 요구하지
  않습니다)
- 클라이언트 시크릿 없음 (SPA 는 번들에 비밀을 담을 수 없습니다)
- `RemovalPolicy.RETAIN` — 스택을 지워도 사용자 계정은 남습니다

### 시안에 없는 화면이 하나 늘었습니다

Cognito 는 가입 직후 계정이 `UNCONFIRMED` 이고 이메일 코드를 확인해야 로그인할 수
있습니다. 시안은 2b(가입) → 2c(설문) 로 바로 넘어가지만 그 사이에
`/signup/verify` 를 넣었습니다. `local` 제공자에서는 이 화면을 거치지 않습니다.

### 프로필·설문 저장 위치

지금은 Cognito User Pool 의 커스텀 속성(`custom:interest_areas` 등)에 둡니다.
아키텍처의 목표 저장소는 DynamoDB `UserProfileTable` 이며, 그 테이블과 API 가
생기면 `cognitoProvider.ts` 의 속성 읽기·쓰기만 HTTP 호출로 바꿉니다.

커스텀 속성은 문자열만 담을 수 있어 관심 분야 배열은 쉼표로 잇습니다. 한 번 만들면
삭제할 수 없으므로 개수를 최소로 유지했습니다.

### 백엔드도 같은 User Pool 을 알아야 합니다

프론트는 `Authorization: Bearer <ID 토큰>` 을 모든 API 호출에 붙이고
(`src/api/client.ts`), 백엔드가 그 토큰을 검증합니다
(`backend/api/app/auth/`). 그래서 같은 값을 API 쪽에도 넣어야 합니다.

```bash
COGNITO_USER_POOL_ID=<UserPoolId>
COGNITO_CLIENT_ID=<UserPoolClientId>
AUTH_REQUIRED=true
```

두 설정이 어긋나면 증상이 이렇게 갈립니다.

| 프론트 | 백엔드 | 결과 |
|---|---|---|
| `cognito` | 설정됨 | 정상 |
| `local` | 설정됨 | 모든 API 가 `401` (토큰이 없음) |
| `cognito` | 설정 없음 | 동작하지만 API 는 무인증 (개방 모드) |

백엔드의 현재 모드는 `GET /health` 의 `auth_mode` 로 확인합니다
(`cognito` · `open` · `blocked`).

관리자 전용 API(환자 목록, 근거 패킷, 코호트, 검토 큐, 감사 로그)는 Cognito
`admin` 그룹만 호출할 수 있습니다. 지금 화면들은 이 API 를 쓰지 않습니다.
환자 단위 데이터는 계정의 `custom:person_id` 와 일치할 때만 열립니다.

### `local` 은 진짜 인증이 아닙니다

검증이 브라우저에서 일어나 누구든 우회할 수 있고, 기기 간에 계정이 공유되지
않습니다. 토큰도 없으므로 보호된 백엔드를 호출할 수 없습니다. 화면 확인용입니다.

### 번들 크기

`VITE_AUTH_PROVIDER` 는 빌드 시점에 리터럴로 치환되므로, `local` 빌드에서는
aws-amplify 가 통째로 트리셰이킹됩니다.

| 설정 | 번들 | gzip |
|---|---|---|
| `local` | 228 KB | 74 KB |
| `cognito` | 360 KB | 111 KB |

`cognito` 인데 User Pool 값이 비어 있으면 조용히 `local` 로 내려앉지 않고 즉시
실패합니다. 진짜 인증인 줄 알고 배포하는 상황을 막기 위해서입니다.

## 배포

### 현재 배포 상태 (2026-08-04)

| 항목 | 값 |
|---|---|
| URL | https://backend-dev-integration.d2n974jp53i37s.amplifyapp.com |
| Amplify 앱 | `trial-matching-web` (`d2n974jp53i37s`, ap-northeast-2) |
| 브랜치 | `backend-dev-integration` (수동 배포) |
| 인증 | 실제 Cognito (`ap-northeast-2_M9srfkUaM`) |
| 데이터 | 목업 (`VITE_USE_MOCK=true`) |

로그인·회원가입·이메일 인증은 실제 Cognito를 씁니다. 로그인 이후 화면 데이터는
목업입니다. 백엔드가 `outputs/longitudinal_emr_v2/` 데이터셋을 읽는데 그 폴더가 없어
uvicorn이 기동되지 않고, API를 올릴 스택도 아직 없습니다. 데이터셋과 API 배포가
준비되면 `VITE_USE_MOCK=false` + `VITE_API_BASE_URL` 로 바꿔 재배포합니다.

### 수동 배포 (Git 연결 없이)

저장소를 Amplify에 연결하지 않고 빌드 산출물만 올리는 경로입니다. 위 배포도 이
방식으로 했습니다.

```bash
cd frontend && npm run build
cd dist && zip -qr /tmp/frontend-dist.zip .

APP=d2n974jp53i37s
BRANCH=backend-dev-integration
aws amplify create-deployment --app-id $APP --branch-name $BRANCH \
  --region ap-northeast-2 > /tmp/deploy.json
# jobId 와 zipUploadUrl 을 꺼내서
curl -X PUT -T /tmp/frontend-dist.zip "<zipUploadUrl>"
aws amplify start-deployment --app-id $APP --branch-name $BRANCH \
  --job-id <jobId> --region ap-northeast-2
```

Vite는 `VITE_*` 값을 빌드 시점에 굽습니다. 이 방식은 로컬 `.env` 값이 그대로
번들에 들어가므로, 배포 전에 `frontend/.env` 가 배포 대상 설정인지 확인해야 합니다.

SPA rewrite 규칙은 앱 생성 시 함께 등록했습니다. 이게 없으면 `/results/...`
딥링크가 404가 됩니다.

```bash
aws amplify get-app --app-id d2n974jp53i37s --region ap-northeast-2 \
  --query "app.customRules"
```

### Amplify Hosting (Git 연결)

저장소 루트의 `amplify.yml` 이 모노레포 형식으로 `frontend` 를 앱 루트로 가리킵니다.

1. Amplify 콘솔에서 저장소 연결 → **Monorepo** 선택 → 앱 루트 `frontend`
2. 환경 변수에 `VITE_API_BASE_URL`, `VITE_USE_MOCK`, `VITE_DEMO_PERSON_ID` 등록
3. **Rewrites and redirects** 에 SPA 규칙 추가 (이게 없으면 `/results/...` 딥링크가 404)

   | Source | Target | Type |
   |---|---|---|
   | `</^[^.]+$\|\.(?!(css\|gif\|ico\|jpg\|js\|png\|txt\|svg\|woff\|woff2\|ttf\|map\|json)$)([^.]+$)/>` | `/index.html` | 200 (Rewrite) |

응답 헤더는 `frontend/customHttp.yml` 이 담당합니다.

### S3 + CloudFront

```bash
S3_BUCKET=my-bucket CLOUDFRONT_DISTRIBUTION_ID=E123ABC ./scripts/deploy-s3.sh
```

CloudFront 배포에 오류 응답 두 개를 등록해야 딥링크가 삽니다. 없으면
`/results/medi25-10842` 로 새로고침할 때 S3 가 `NoSuchKey` 를 돌려줍니다.

| HTTP 오류 코드 | 응답 페이지 | 응답 코드 |
|---|---|---|
| 403 | `/index.html` | 200 |
| 404 | `/index.html` | 200 |

버킷은 퍼블릭으로 열지 말고 CloudFront OAC 로만 읽게 두세요.

### 배포 후 백엔드 CORS

프론트가 배포되면 백엔드가 그 출처를 허용해야 합니다. 환경 변수로 받습니다.

```bash
CORS_ALLOW_ORIGINS=https://main.d123.amplifyapp.com,https://trial.example.com
```

지정하지 않으면 로컬 개발용(`localhost:3000`, `127.0.0.1:3000`)만 허용합니다.

## 구조

```
frontend/
├── amplify.yml (저장소 루트)   Amplify 빌드 스펙
├── customHttp.yml              Amplify 응답 헤더
├── scripts/deploy-s3.sh        S3 + CloudFront 배포
└── src/
    ├── api/
    │   ├── types.ts            backend/api/app/schemas.py 와 1:1
    │   ├── client.ts           fetch 래퍼, ApiError, 설정
    │   ├── endpoints.ts        main.py 라우트와 1:1 · 목업 스위치
    │   └── mock.ts             데이터셋 없이 도는 어댑터 (공고별 지원서 항목 포함)
    ├── auth/                   인증 계약 + localStorage 구현
    ├── state/MatchingContext   추천 실행 결과 보관
    ├── components/             ds 클래스를 감싼 입력·태그·단계 표시
    ├── pages/                  시안 화면 1:1
    └── styles/
        ├── ds.css              디자인 시스템 (시안 ds/styles.css 이식)
        └── app.css             앱 오버라이드 · 레이아웃 클래스
```

`ds.css` 는 시안 원본에서 인쇄 판형 연출(`.cmyk`, `.halftone`, 판 번호 등)을 뺀
것입니다. 그 규칙들은 `print-plates.js` 가 심는 SVG 필터를 전제로 하는데
이 앱의 화면 중 어느 것도 쓰지 않습니다.
