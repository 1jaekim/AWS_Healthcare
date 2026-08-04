# AWS_Healthcare

임상시험 매칭 AI 프로젝트 (AWS bootcamp)

환자 EMR과 임상시험 모집공고를 각각 수집·정제한 뒤, 기준별로 근거를 확보해
적격성을 판정하고 근거·확인질문·설명을 함께 반환하는 시스템입니다.

흩어져 있던 브랜치(`backend`, `GGeunGGeun`, `RAG`, `Crwaler`)를 이 `dev` 브랜치로 통합했고,
계층별 최상위 폴더로 나눠 한곳에서 관리합니다.

## 레포 구조

```
.
├── backend/     데이터 파이프라인 + 스크리닝 오케스트레이터 API
├── crawler/     Medi25 임상시험 모집공고 크롤러
├── rag/         검색·근거 확보(Retrieval) 계층
└── agent/       에이전트 계층 (모델 클라이언트, tool-use 루프, FM 검증·설명)
```

| 폴더 | 내용 | 문서 |
|------|------|------|
| `backend/` | AWS CDK 인프라, 파이프라인 Lambda, Step Functions, FastAPI 스크리닝 API | [backend/README.md](backend/README.md) · [backend/api/README.md](backend/api/README.md) |
| `crawler/` | Medi25 검색 → 모집공고 선별 → 메타데이터 표준화(JSON) | [crawler/README.md](crawler/README.md) |
| `rag/` | 청킹·색인·질의 템플릿·검색 품질 평가 | [rag/README.md](rag/README.md) |
| `agent/` | 모델 클라이언트, tool spec, tool-use 루프, FM 검증·설명 생성 | [agent/README.md](agent/README.md) |

`agent/` 는 `backend` 를 import 하지 않습니다. 필요한 동작은 `agent/contracts.py` 의
Protocol 로 선언하고 `backend/api/app/container.py` 가 구현을 주입합니다.
의존성은 `backend → agent` 한 방향입니다.

## 데이터 흐름

```
[crawler]  질환 키워드 → Medi25 모집공고 → 표준화 JSON
                                              │
                                              ▼
[backend]  공고 PDF/JSON → Protocol Parser → DynamoDB Criteria Store
           환자 임상 JSON → Sanitizer → S3 rag/patients/
                                      → Bedrock KB GraphRAG
                                      → Neptune Analytics
                                                │
                                                ▼
[agent]    Criteria Store + GraphRAG Retrieve + Timeline Tool로 근거 수집
           → 기준별 검증 → 제한된 UNKNOWN 토론 → 추천·설명 생성 (Bedrock FM)
                                                │
                                                ▼
[backend]  OK / NOT_OK / UNKNOWN 확정 → 근거 패킷 반환
```

FM은 적격성을 결정하지 않습니다. 판정 확정은 결정론적 규칙이 담당해
동일 입력에 동일 결과를 보장합니다.

## 시작하기

### 스크리닝 API 로컬 실행

저장소 루트에서 실행합니다.

```powershell
python -m venv backend/.venv
backend/.venv/Scripts/python -m pip install -r backend/api/requirements.txt
backend/.venv/Scripts/python -m uvicorn app.main:app --app-dir backend/api --reload --port 8000
```

- Swagger: http://127.0.0.1:8000/docs
- 헬스체크: http://127.0.0.1:8000/health

`-m` 이 저장소 루트를 import 경로에 넣어 `agent` 패키지를 찾고,
`--app-dir` 이 `backend/api` 를 넣어 `app` 패키지를 찾습니다.

### 테스트

```powershell
cd backend/api
../.venv/Scripts/python -m pytest tests -q
```

### 인프라 배포 (CDK)

```bash
cd backend
pip install -r requirements.txt
cdk bootstrap    # 최초 1회
cdk deploy --all
```

자세한 내용은 [backend/README.md](backend/README.md)를 참고하세요.

## 브랜치 운영

| 브랜치 | 용도 |
|--------|------|
| `dev` | 통합 브랜치. 모든 작업은 여기로 모읍니다 |
| `main` | 릴리스 대상 |
| 기타 | 기능 단위 작업 브랜치 → `dev`로 PR |

## 담당 구분

| 영역 | 담당 | 상태 |
|------|------|------|
| 데이터 파이프라인 (Sanitizer, Bedrock GraphRAG, Protocol Parser) | Lee-namju | 구현 중 |
| 관측성 (CloudWatch, SNS) | Lee-namju | 완료 |
| Medi25 크롤러 | 1jaekim | 실행 코드 포함 |
| 스크리닝 오케스트레이터 API | GGeunGGeun | 완료 |
| 에이전트 계층 (`agent/`) | GGeunGGeun | 완료, Bedrock 실호출 미검증 |
| RAG 검색 계층 | 미정 | Bedrock KB Retrieve 연결 완료 |
| AWS 배포 검증 | 공동 | 진행 예정 |
