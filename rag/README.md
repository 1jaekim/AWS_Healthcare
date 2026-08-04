# GraphRAG

임상시험 매칭의 환자 근거·표준문서 검색 계층 작업 공간이다.

공고 선정/제외 기준 JSON은 `TrialCriteriaTable`이 Source of Truth이며 GraphRAG에
넣지 않는다. GraphRAG는 비식별 환자 문서와 표준문서에서 관련 근거를 찾는 용도로만
사용한다. 수치·날짜·기간 계산은 구조화 임상 이벤트와 규칙 엔진이 담당한다.

## 현재 구조

```text
환자 임상 JSON/EMR
  -> 가명처리 및 정규식 1차 마스킹
  -> 환자별 Markdown + metadata sidecar
  -> S3 rag/patients/
  -> Bedrock Knowledge Bases GraphRAG
  -> Neptune Analytics
  -> Bedrock Retrieve(patient_key 필터 필수)
  -> EvidenceGatheringAgent
```

## 데이터 계약

| 항목 | 값 |
|------|----|
| 문서 | `rag/patients/{patient_key}.md` |
| 메타데이터 | `rag/patients/{patient_key}.md.metadata.json` |
| 문서 유형 | `patient_evidence` |
| 스키마 버전 | `graphrag-patient-v1` |
| 가명처리 상태 | `pseudonymized_regex_v1` |
| 환자 격리 | 모든 환자 검색에 `patient_key` metadata filter 필수 |
| 이벤트 참조 | 원본 방문 ID 대신 HMAC 기반 `event_key` 사용 |

## AWS 구성

| 서비스 | 역할 |
|--------|------|
| Amazon Bedrock Knowledge Bases | GraphRAG 수집과 Retrieve API |
| Amazon Neptune Analytics | Bedrock 관리형 그래프·벡터 저장소 |
| Amazon Titan Text Embeddings v2 | 1,024차원 임베딩 |
| Amazon Nova Micro | 수집 시 chunk entity extraction |
| Amazon S3 | 환자별 Markdown과 metadata sidecar 원본 |

## 구현 위치

| 경로 | 역할 |
|------|------|
| `backend/infra/bedrock_stack.py` | Bedrock KB + Neptune Analytics 프로비저닝 |
| `backend/lambdas/sanitizer/graphrag_documents.py` | 환자별 문서·메타데이터 생성 |
| `backend/lambdas/sanitizer/handler.py` | S3 `rag/patients/` 적재 |
| `backend/step_functions/emr_pipeline.json` | 문서 생성 후 ingestion job 동기화 |
| `backend/api/app/tools/evidence_retrieval.py` | 로컬/Bedrock KB Retrieve 어댑터와 환자 격리 필터 |

## Agent 경계

RAG 자체는 Agent가 아니라 검색 Tool이다. `EvidenceGatheringAgent`만 다음 두 Tool을
선택 호출할 수 있다.

- `evidence_retrieval_tool`: GraphRAG에서 자유서술 근거 검색
- `timeline_graph_tool`: 구조화 임상 이벤트 조회

공고 원문이나 `TrialCriteriaTable`은 RAG Tool 권한에 포함하지 않는다.

## 다음 작업

- [x] API의 `evidence_retrieval_tool`에 Bedrock Retrieve 어댑터 연결
- [x] HMAC/Secrets Manager 기반 비식별 `patient_key` 필터 계약 연결
- [ ] 기준 유형별 검색 질의 템플릿 작성
- [ ] 기준 x 환자별 정답 근거 평가셋 구축
- [ ] 검색 재현율·정밀도·환자 격리 회귀 테스트 추가

상위 흐름은 `doc/MATCHING_MODEL_V2.md`와 `doc/ARCHITECTURE_V2.md`를 따른다.
