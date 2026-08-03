# RAG

임상시험 매칭에 쓰이는 **검색·근거 확보(Retrieval) 계층** 작업 공간입니다.

비식별 EMR(`s3://.../rag/canonical_notes.jsonl`)을 Bedrock Knowledge Base로 색인하고,
기준(criterion) 단위 질의로 근거 문장을 회수하는 부분을 담당합니다.

## 담당 범위

- 청킹/메타데이터 스키마 설계 (`person_id`, `note_id`, `date` 등 필터 키)
- 임베딩 모델·차원 선택, 색인 파라미터 튜닝
- 기준별 검색 질의 템플릿 (선정/제외 기준 → 검색 쿼리 변환)
- 재현율/정밀도 평가셋 및 회귀 측정
- 하이브리드 검색(키워드 + 벡터), 리랭킹

## 관련 기존 코드

현재 검색 관련 구현은 `backend/` 안에 있습니다. 이 폴더는 그 위에서 돌아가는
검색 품질 실험·평가·설정을 모아두는 자리입니다.

| 경로 | 역할 |
|------|------|
| `backend/infra/bedrock_stack.py` | Bedrock KB + OpenSearch Serverless 프로비저닝 |
| `backend/lambdas/sanitizer/` | PHI 마스킹 → KB 소스 데이터 생성 |
| `backend/lambdas/opensearch_query/` | Bedrock KB Retrieve 호출 |
| `backend/api/app/tools/evidence_retrieval.py` | API 측 근거 검색 도구 (현재 로컬 키워드 어댑터) |

## 예정 구조

```
rag/
├── README.md
├── indexing/      # 청킹·메타데이터·색인 스크립트
├── queries/       # 기준별 검색 질의 템플릿
└── eval/          # 검색 품질 평가셋 및 리포트
```

## 다음 작업

- [ ] 청킹 전략 확정 (문장 단위 vs 섹션 단위)
- [ ] 기준 유형별 질의 템플릿 초안
- [ ] 평가셋 구축 (기준 x 환자 → 정답 근거 문장)
- [ ] `evidence_retrieval_tool` 로컬 어댑터 → Bedrock KB 전환
