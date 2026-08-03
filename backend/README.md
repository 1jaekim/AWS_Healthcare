# AWS Healthcare Backend Pipeline

임상시험 매칭 플랫폼을 위한 데이터 파이프라인 백엔드입니다.  
환자 EMR 데이터를 전처리하고, 임상시험 공고를 구조화하여 매칭 계층에 제공합니다.

## 아키텍처 개요

```
현재 보유 데이터          전처리              Amazon S3           검색·그래프 구축
┌─────────────┐    ┌──────────────┐    ┌─────────────┐    ┌────────────────────┐
│clinical_notes│───▶│  Sanitizer   │───▶│  raw/ 원본  │───▶│ Bedrock KB         │
│  .jsonl      │    │ PHI 마스킹   │    │  rag/ 비식별│    │ OpenSearch Vector  │
│ 10,485 노트  │    │ 판정문장 제거 │    │             │    │                    │
└─────────────┘    └──────────────┘    │             │    └────────────────────┘
                                       │             │
┌─────────────┐    ┌──────────────┐    │             │    ┌────────────────────┐
│timeline.csv  │───▶│  Graph ETL   │───▶│  graph/     │───▶│ Neptune 그래프     │
│measurement   │    │ Node·Edge CSV│    │  Neptune CSV│    │ Bulk Load + Upsert │
│medication    │    └──────────────┘    │             │    └────────────────────┘
└─────────────┘                        │             │
                                       │             │    ┌────────────────────┐
┌─────────────┐    ┌──────────────┐    │             │    │ Bedrock Protocol   │
│임상시험 공고  │───▶│  공고 업로드  │───▶│  trials/    │───▶│ Parser → DynamoDB  │
│ PDF · 자연어 │    └──────────────┘    │  공고 원문  │    │ Criteria Store     │
└─────────────┘                        └─────────────┘    └────────────────────┘
```

## 프로젝트 구조

```
backend/
├── app.py                           # CDK 엔트리포인트
├── cdk.json                         # CDK 설정
├── requirements.txt                 # CDK 의존성
├── config/
│   └── settings.py                  # 전역 환경변수 설정
├── infra/
│   ├── s3_stack.py                  # S3 데이터 레이크 (KMS 암호화)
│   ├── bedrock_stack.py             # Bedrock KB + OpenSearch Serverless
│   ├── observability_stack.py       # CloudWatch 대시보드/알람/SNS
│   └── main_stack.py               # Lambda, Neptune, DynamoDB, Step Functions
├── lambdas/
│   ├── sanitizer/                   # PHI 마스킹 + 판정 문장 제거
│   ├── graph_etl/                   # CSV → Neptune Node/Edge CSV 변환
│   ├── neptune_upsert/              # Bulk Load + 증분 Gremlin Upsert
│   ├── protocol_parser/             # 공고 PDF → 선정/제외 기준 JSON
│   └── opensearch_query/            # Bedrock KB Retrieve API
├── step_functions/
│   ├── emr_pipeline.json            # Sanitizer → GraphETL → Neptune
│   └── trials_pipeline.json         # Protocol Parser → SNS 검토 알림
├── tests/
└── scripts/
```

## 주요 컴포넌트

### 1. Sanitizer Lambda
- `clinical_notes.jsonl` (10,485개) → 비식별 EMR 생성
- PHI 마스킹: 이름, 날짜, 전화번호, SSN, 이메일, MRN, 주소
- 판정 문장 제거: 확정 진단/판정 관련 문장 필터링
- 출력: canonical 2,097건 → S3 `rag/`

### 2. Graph ETL Lambda
- timeline / measurement / medication CSV 파싱
- Neptune Bulk Loader 형식 CSV 생성 (Node + Edge)
- 노드: Patient, Event, Measurement, Medication
- 엣지: HAS_EVENT, HAS_MEASUREMENT, TAKES_MEDICATION, RECORDED_DURING

### 3. Neptune Upsert Lambda
- **초기 적재**: Neptune Bulk Loader API
- **증분 업데이트**: S3 이벤트 → Gremlin upsert (fold + coalesce 패턴)
- IAM SigV4 인증, VPC 내부 실행

### 4. Protocol Parser Lambda
- 임상시험 공고 PDF → Textract 텍스트 추출
- Bedrock Claude → 선정/제외 기준 구조화 JSON
- DynamoDB Criteria Store 저장 (status: `pending_review`)
- 연구자 검토 후 `approved`로 상태 변경

### 5. OpenSearch Query Lambda
- Bedrock Knowledge Bases Retrieve API 호출
- 벡터 유사도 + 키워드 하이브리드 검색
- 필터 지원 (note_type, patient_id)

## Step Functions 워크플로우

### EMR Pipeline
```
Sanitizer → Graph ETL → Neptune Bulk Load → (상태 폴링) → 완료
```
- 각 단계 재시도 3회 (BackoffRate 2.0)
- 실패 시 SNS 알림 + DLQ 전달

### Trials Pipeline
```
EventBridge (trials/ 업로드) → Protocol Parser → SNS 검토 알림
```
- Bedrock 쓰로틀링 대응 (재시도 5회)
- 실패 시 SNS 알림

## 관측성

- **CloudWatch Dashboard**: Lambda 호출/에러/실행시간, Bedrock 토큰/비용
- **알람**: Lambda 에러(≥3회/5분), 쓰로틀(≥5회), Duration(p95 > 240s)
- **SNS 알림**: 파이프라인 실패, 검토 대기건
- **로그 보존**: 30일

## 배포 방법

### 사전 요구사항
- Python 3.12+
- AWS CLI 설정 완료 (`aws configure`)
- AWS CDK CLI (`npm install -g aws-cdk`)

### 배포

```bash
cd backend
pip install -r requirements.txt

# 알림 이메일 설정 (선택)
export ALERT_EMAIL="your-email@example.com"

# 최초 배포 시 bootstrap 필요
cdk bootstrap

# 전체 배포
cdk deploy --all
```

### 배포 스택 순서
1. `HealthcareS3Stack` - S3 데이터 레이크
2. `HealthcareBedrockStack` - Bedrock KB + OpenSearch
3. `HealthcareObservabilityStack` - 모니터링
4. `HealthcareMainStack` - Lambda, Neptune, Step Functions

## S3 버킷 구조

| 경로 | 용도 | 보안 |
|------|------|------|
| `raw/` | 원본 데이터 보존 | KMS 암호화, 90일 후 Glacier |
| `rag/` | 비식별 EMR (KB 소스) | KMS 암호화 |
| `graph/` | Neptune CSV | KMS 암호화 |
| `trials/` | 임상시험 공고 원문 | KMS 암호화 |

## 담당 범위

- ✅ 데이터 전처리 (Sanitizer, Graph ETL)
- ✅ 검색·그래프 구축 (Bedrock KB, OpenSearch, Neptune)
- ✅ 공고 구조화 (Protocol Parser, DynamoDB)
- ✅ 파이프라인 오케스트레이션 (Step Functions)
- ✅ 관측성 (CloudWatch)
- ⬜ 매칭 계층 (다른 팀원 담당)
