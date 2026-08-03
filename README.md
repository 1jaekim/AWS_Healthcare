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
└── agent/       에이전트 정의·프롬프트·도구 스펙
```

| 폴더 | 내용 | 문서 |
|------|------|------|
| `backend/` | AWS CDK 인프라, 파이프라인 Lambda, Step Functions, FastAPI 스크리닝 API | [backend/README.md](backend/README.md) · [backend/api/README.md](backend/api/README.md) |
| `crawler/` | Medi25 검색 → 모집공고 선별 → 메타데이터 표준화(JSON) | [crawler/README.md](crawler/README.md) |
| `rag/` | 청킹·색인·질의 템플릿·검색 품질 평가 | [rag/README.md](rag/README.md) |
| `agent/` | 에이전트 역할 명세, 프롬프트, tool spec, 가드레일 정책 | [agent/README.md](agent/README.md) |

## 데이터 흐름

```
[crawler]  질환 키워드 → Medi25 모집공고 → 표준화 JSON
                                              │
                                              ▼
[backend]  공고 PDF/JSON → Protocol Parser → DynamoDB Criteria Store
           원본 EMR      → Sanitizer      → S3 rag/ ─┐
           타임라인/측정값 → Graph ETL      → Neptune   │
                                                      ▼
[rag]                                    Bedrock KB + OpenSearch 색인
                                                      │
                                                      ▼
[agent]    근거 수집 루프 · NLI 검증 · 설명/확인질문 생성 (Bedrock FM)
                                                      │
                                                      ▼
[backend]  규칙 기반 기준별 상태 확정 → 종합 적격성 판정 → 근거 패킷 반환
```

FM은 적격성을 결정하지 않습니다. 판정 확정은 결정론적 규칙이 담당해
동일 입력에 동일 결과를 보장합니다.

## 시작하기

### 스크리닝 API 로컬 실행

```bash
python -m venv backend/.venv
backend/.venv/Scripts/activate      # Windows (macOS/Linux: source backend/.venv/bin/activate)
pip install -r backend/api/requirements.txt
uvicorn app.main:app --app-dir backend/api --reload --port 8000
```

- Swagger: http://127.0.0.1:8000/docs
- 헬스체크: http://127.0.0.1:8000/health

### 테스트

```bash
cd backend/api
pytest tests -q
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
| 데이터 파이프라인 (Sanitizer, Graph ETL, Neptune, Protocol Parser) | Lee-namju | 완료 |
| 관측성 (CloudWatch, SNS) | Lee-namju | 완료 |
| Medi25 크롤러 | 1jaekim | 문서 완료, 구현 예정 |
| 스크리닝 오케스트레이터 API | GGeunGGeun | 완료 |
| RAG 검색 계층 | 미정 | 진행 예정 |
| 에이전트 명세·프롬프트 | 미정 | 진행 예정 |
| 로컬 어댑터 → AWS 연결 | 공동 | 진행 예정 |
