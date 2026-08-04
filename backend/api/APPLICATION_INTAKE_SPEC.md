# 임상시험 자연어 지원서 및 JSON Schema 연동 명세

## 1. 문서 목적

이 문서는 임상시험 공고를 기반으로 지원서용 JSON Schema를 만들고, 사용자의 자연어
지원서를 구조화하며, 누락된 정보가 모두 채워질 때까지 추가 답변을 받는 기능의 요구사항과
현재 구현 상태를 정리한다.

담당 범위는 **완성된 지원서 JSON을 반환하는 시점까지**다.

- 임상시험 공고문 제작·등록: 다른 팀 담당
- 완성된 지원서 JSON 이후 적격성 판단·후속 처리: 다른 팀 담당
- 이 모듈: 기본 Schema 제공, 공고별 Schema 확장, 자연어 추출, 누락 질문, 답변 병합,
  완성 여부 확인

이 모듈에서는 적격·부적격을 판단하지 않는다.

---

## 2. 전체 처리 흐름

```text
고정 기본 JSON Schema v1
        ↓
임상시험 공고문 분석
        ↓
공고별 추가 필드 생성
        ↓
버전이 고정된 최종 JSON Schema 저장
        ↓
사용자가 자연어 지원서 제출
        ↓
LLM이 Schema 필드에 맞춰 값 추출
        ↓
필수 필드 누락 여부 확인
        ├─ 누락 있음 → 공고문 + 누락 필드 + 추가 작성 요청 반환(최대 5회)
        │               ↓
        │          자연어 추가 답변 병합
        │               ↓
        └──────── 누락 검사를 반복
                        ↓
                 모든 필드가 채워짐
                        ↓
              COMPLETE + 완성 JSON 반환
```

---

## 3. 기본 JSON Schema

공고 내용과 관계없이 모든 지원서에 다음 필드가 기본으로 포함된다.

| 필드 | 타입 | 설명 | 예시 |
|---|---|---|---|
| `age` | `integer` | 지원 시점의 만 나이 | `42` |
| `sex` | `string` + `enum` | 성별 | `"female"` |
| `diagnosed_conditions` | `array<string>` | 현재 진단받은 질환 | `["당뇨병"]` |
| `current_medications` | `array<string>` | 현재 복용 중인 약 | `["메트포르민"]` |
| `allergies` | `array<string>` | 약물 및 기타 알레르기 | `["페니실린"]` |
| `prior_trial_participation` | `boolean` | 과거 임상시험 참여 여부 | `false` |

기본 Schema 예시는 다음과 같다.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "title": "임상시험 공통 지원서",
  "additionalProperties": false,
  "properties": {
    "age": {
      "type": "integer",
      "title": "만 나이",
      "minimum": 0,
      "maximum": 130,
      "x-source": "base"
    },
    "sex": {
      "type": "string",
      "title": "성별",
      "enum": ["male", "female", "other", "prefer_not_to_say"],
      "x-source": "base"
    },
    "diagnosed_conditions": {
      "type": "array",
      "items": {"type": "string"},
      "x-source": "base"
    },
    "current_medications": {
      "type": "array",
      "items": {"type": "string"},
      "x-source": "base"
    },
    "allergies": {
      "type": "array",
      "items": {"type": "string"},
      "x-source": "base"
    },
    "prior_trial_participation": {
      "type": "boolean",
      "x-source": "base"
    }
  },
  "required": [
    "age",
    "sex",
    "diagnosed_conditions",
    "current_medications",
    "allergies",
    "prior_trial_participation"
  ]
}
```

기본 Schema에는 버전이 부여된다. 공고별 Schema에도 해당 Schema가 어떤 기본 버전에서
파생되었는지 `base_schema_version`으로 기록한다.

이름, 전화번호, 이메일과 같은 직접 식별정보는 지원 자격 JSON에 포함하지 않는다. 이러한
정보는 사용자 계정·본인 확인 영역에서 별도로 관리하는 것을 전제로 한다.

---

## 4. 공고별 Schema 확장

LLM은 공고문에서 지원자가 직접 답해야 하는 사실을 찾아 기본 Schema에 새로운 필드를
추가한다.

예를 들어 공고에 다음 조건이 있다고 가정한다.

```text
최근 HbA1c가 7.5% 이상이고, 최근 6개월 동안 사용한 당뇨병 치료제를 확인합니다.
```

LLM이 제안할 수 있는 추가 필드는 다음과 같다.

```json
{
  "fields": [
    {
      "name": "latest_hba1c",
      "type": "number",
      "title": "최근 HbA1c",
      "description": "가장 최근 HbA1c 검사 결과"
    },
    {
      "name": "diabetes_medications_last_6_months",
      "type": "array",
      "title": "최근 6개월 당뇨병 치료제",
      "description": "최근 6개월 동안 사용한 당뇨병 치료제 목록"
    }
  ]
}
```

### 확장 규칙

- 공고별 필드는 기본 필드를 수정하거나 덮어쓸 수 없다.
- 기본 필드와 동일하거나 정규화 후 중복되는 이름은 거부한다.
- 지원 타입은 `string`, `integer`, `number`, `boolean`, `array`다.
- 공고별 필드는 `x-source: trial_notice`로 표시한다.
- Schema에 추가된 필드는 모두 수집 대상이며 `required`에 포함한다.
- 공고별 추가 필드는 최대 64개로 제한한다.
- 공고문, `trial_id`, 완성 Schema를 함께 해시해 Schema 버전을 만든다.
- `additionalProperties: false`이므로 Schema에 없는 값은 저장하지 않는다.

공고 담당 팀이 이미 공고를 구조화했다면 LLM을 다시 호출하지 않고 `additional_fields`로
필드를 직접 전달할 수 있다.

```json
{
  "trial_id": "TRIAL-T2D-01",
  "notice_text": "공고문 전체 내용",
  "additional_fields": [
    {
      "name": "latest_hba1c",
      "type": "number",
      "title": "최근 HbA1c",
      "description": "가장 최근 HbA1c 검사 결과"
    }
  ]
}
```

`BEDROCK_ENABLED=false`인 로컬 환경에서는 공고 내용을 임의로 추측하지 않는다. 이 경우
`additional_fields`를 명시적으로 전달해야 한다.

실제 LLM 호출의 기본 설정은 서울 리전(`ap-northeast-2`)과 검증된 Global Claude Sonnet
4.5 inference profile(`global.anthropic.claude-sonnet-4-5-20250929-v1:0`)이다. 환경별로
`AWS_REGION`, `BEDROCK_MODEL_ID`를 지정해 교체할 수 있다.

---

## 5. 단일값과 다중값 기준

### 단일값

특정 시점에 하나의 값만 성립하거나 하나만 선택해야 하는 항목이다.

| 의미 | 권장 타입 | 예시 |
|---|---|---|
| 여부 | `boolean` | 임신 여부, 과거 참여 여부 |
| 정수 | `integer` | 나이, 진단 후 경과 연수 |
| 실수 | `number` | 최근 HbA1c, BMI |
| 자유 단일값 | `string` | 직업, 단일 상태 |
| 상호 배타적인 선택지 | `string + enum` | 흡연 상태, 질환 유형 |

```json
{
  "smoking_status": {
    "type": "string",
    "enum": ["never", "former", "current"]
  }
}
```

### 다중값

동시에 여러 항목이 존재하거나 사용자가 목록으로 답해야 하는 항목이다.

| 의미 | 권장 타입 |
|---|---|
| 현재 진단 질환 목록 | `array<string>` |
| 복용 중인 약 목록 | `array<string>` |
| 알레르기 목록 | `array<string>` |
| 과거 수술 목록 | `array<string>` |
| 여러 증상·치료 목록 | `array<string>` |

```json
{
  "current_medications": {
    "type": "array",
    "items": {"type": "string"}
  }
}
```

### LLM이 cardinality를 판단할 때 사용할 기준

1. 동시에 여러 값이 존재할 수 있으면 `array`를 선택한다.
2. 공고에 `목록`, `모두`, `복수`, `하나 이상`이 있으면 다중값으로 본다.
3. `현재 여부`, `가장 최근`, `하나 선택`이면 단일값으로 본다.
4. 선택지가 상호 배타적이면 `string + enum`을 사용한다.
5. `최근 N회 검사 결과`처럼 같은 구조가 반복되면 본래 `array<object>`가 적합하다.

### 현재 한계

현재 구현에서 LLM이 필드 타입을 제안하고 구조 검증기가 지원 타입 여부를 검사하지만,
선택한 cardinality가 공고의 의미와 일치하는지는 결정론적으로 검증하지 않는다.

또한 현재 배열 항목은 `string`만 지원한다. 다음과 같은 복합 배열은 아직 지원하지 않는다.

```json
{
  "hba1c_results": [
    {"value": 8.1, "measured_at": "2026-01-10"},
    {"value": 7.8, "measured_at": "2026-04-10"}
  ]
}
```

운영에서 Schema를 바로 확정하기보다는 다음 절차가 권장된다.

```text
LLM 필드 후보 추출
        ↓
타입·cardinality 규칙 검증
        ↓
공고 원문과 Schema 대조
        ↓
공고 담당자 승인
        ↓
승인된 Schema로 지원서 수집 시작
```

---

## 6. 자연어 지원서 추출

사용자의 자연어 지원서와 완성된 JSON Schema를 LLM에 함께 전달한다. LLM은 Schema에
정의된 필드만 반환해야 하며, 언급되지 않은 값은 추측하지 않는다.

입력 예시:

```text
저는 42세 여성이고 제2형 당뇨병을 진단받았습니다.
현재 메트포르민을 복용하고 있으며 알레르기는 없습니다.
과거 임상시험에 참여한 적은 없습니다.
최근 HbA1c는 8.1%였습니다.
```

추출 결과 예시:

```json
{
  "age": 42,
  "sex": "female",
  "diagnosed_conditions": ["제2형 당뇨병"],
  "current_medications": ["메트포르민"],
  "allergies": [],
  "prior_trial_participation": false,
  "latest_hba1c": 8.1
}
```

LLM 출력은 그대로 저장하지 않고 다음 검증을 거친다.

- Schema에 정의되지 않은 필드 제거
- 필드 타입 변환 및 검사
- 숫자의 최소·최대 범위 검사
- `enum` 값 검사
- `null` 및 공백 문자열 제거

---

## 7. 미응답, `null`, `없음`의 의미

현재 저장 규칙은 다음과 같다.

| 상태 | 저장 형태 | 누락 여부 |
|---|---|---|
| 사용자가 응답하지 않음 | 키 없음 | 누락 |
| LLM이 `null` 반환 | 키를 저장하지 않음 | 누락 |
| 목록에 해당 사항 없음 | `[]` | 완료 |
| 불리언 질문에 아니요 | `false` | 완료 |
| 실제 목록 값 있음 | `["값1", "값2"]` | 완료 |

예를 들어 사용자가 진단 질환에 답하지 않으면 `data`에 키가 없다.

```json
{
  "data": {
    "age": 42,
    "sex": "female"
  },
  "missing_fields": [
    {
      "name": "diagnosed_conditions",
      "title": "현재 진단받은 질환",
      "type": "array"
    }
  ]
}
```

사용자가 `진단받은 질환은 없음`이라고 답하면 다음처럼 저장한다.

```json
{
  "diagnosed_conditions": []
}
```

다음 표현은 배열의 명시적인 `없음`으로 정규화한다.

- `없음`, `없어요`, `없습니다`
- `해당 없음`
- `복용하지 않음`
- `진단받은 질환 없음`
- `알레르기 없음`
- `none`, `no`, `n/a`

---

## 8. 누락 필드 재질문

매 응답 처리 후 Schema의 `required` 목록과 현재 `data`를 비교한다. 이미 채워진 항목은
다시 요청하지 않고 누락된 항목만 `missing_fields`와 `follow_up_prompt`에 포함한다.

재질문 횟수는 사용자에게 실제 `follow_up_prompt`를 발행한 시점을 기준으로 계산한다.

1. 첫 지원서에 누락이 있으면 첫 번째 재질문을 발행한다.
2. 추가 답변 후에도 누락이 있으면 다음 재질문을 발행한다.
3. 재질문은 최대 5회까지 발행한다.
4. 다섯 번째 질문에 대한 답변 이후에도 누락이 남으면
   `MAX_FOLLOW_UPS_REACHED`로 종료한다.
5. 종료 상태에서는 `follow_up_prompt`가 `null`이고 추가 답변 API는 `409 Conflict`다.

```json
{
  "status": "MAX_FOLLOW_UPS_REACHED",
  "follow_up_count": 5,
  "max_follow_ups": 5,
  "missing_fields": [{"name": "latest_hba1c"}],
  "follow_up_prompt": null
}
```

```json
{
  "status": "NEEDS_MORE_INFO",
  "missing_fields": [
    {
      "name": "allergies",
      "title": "알레르기",
      "description": "알고 있는 약물 또는 기타 알레르기",
      "type": "array"
    },
    {
      "name": "prior_trial_participation",
      "title": "과거 임상시험 참여 여부",
      "description": "과거 다른 임상시험 참여 여부",
      "type": "boolean"
    }
  ],
  "follow_up_prompt": "지원서에서 다음 내용이 확인되지 않았습니다: 알레르기, 과거 임상시험 참여 여부. 해당 내용을 추가로 작성해 주세요.",
  "notice_text": "공고문 전체 내용"
}
```

현재 질문은 누락 필드의 제목을 묶어 안내하는 공통 템플릿이다. 필드별로 자연스러운 개별
질문을 생성하는 기능은 아직 구현하지 않았다.

추가 답변을 분석할 때는 Schema, 기존 값, 새 답변을 LLM에 함께 제공한다. 사용자가 기존
값을 명시적으로 정정하면 단일값은 최신 답변으로 수정할 수 있다.

---

## 9. 중복 및 추가 답변 병합

### 배열 필드

배열은 기존 값을 유지하면서 새 값을 누적하고, 대소문자와 앞뒤 공백을 정규화해 중복을
제거한다. 기본 배열뿐 아니라 공고가 추가한 모든 `array` 필드에 같은 규칙을 적용한다.

첫 답변:

```json
{
  "diagnosed_conditions": ["당뇨병"],
  "current_medications": ["메트포르민"]
}
```

추가 답변:

```json
{
  "diagnosed_conditions": ["당뇨병", "고혈압"],
  "current_medications": ["인슐린"]
}
```

병합 결과:

```json
{
  "diagnosed_conditions": ["당뇨병", "고혈압"],
  "current_medications": ["메트포르민", "인슐린"]
}
```

사용자가 배열 항목에 대해 명시적으로 `없음`이라고 정정하면 기존 값을 모두 지우고 빈
배열로 저장한다.

```text
알레르기는 없습니다.
```

```json
{
  "allergies": []
}
```

### 단일값

`age`, `sex`, `number`, `integer`, `boolean`, 단일 `string`은 가장 최근의 명확한 답변으로
교체한다.

---

## 10. 완료 조건과 반환 JSON

모든 필수 필드가 채워지기 전에는 다음 상태를 반환한다.

```json
{
  "status": "NEEDS_MORE_INFO"
}
```

모든 필드가 채워지면 다음 상태와 완성된 `data`를 반환한다.

```json
{
  "application_id": "APP-...",
  "schema_id": "APP-SCHEMA-TRIAL-T2D-01-...",
  "trial_id": "TRIAL-T2D-01",
  "status": "COMPLETE",
  "data": {
    "age": 42,
    "sex": "female",
    "diagnosed_conditions": ["제2형 당뇨병"],
    "current_medications": ["메트포르민"],
    "allergies": [],
    "prior_trial_participation": false,
    "latest_hba1c": 8.1
  },
  "missing_fields": [],
  "follow_up_prompt": null,
  "iteration": 2,
  "follow_up_count": 1,
  "max_follow_ups": 5
}
```

`COMPLETE`는 정보 수집 완료를 의미하며 임상시험 참여 가능 또는 적격 판정을 의미하지 않는다.

---

## 11. API 계약

| Method | Path | 설명 |
|---|---|---|
| `GET` | `/api/v1/application-schemas/base` | 고정 기본 JSON Schema 조회 |
| `POST` | `/api/v1/application-schemas` | 기본 Schema에 공고별 필드를 추가 |
| `GET` | `/api/v1/application-schemas/{schema_id}` | 버전이 고정된 Schema 조회 |
| `POST` | `/api/v1/applications` | 첫 자연어 지원서 제출 |
| `POST` | `/api/v1/applications/{application_id}/responses` | 누락 또는 정정 답변 제출 |
| `GET` | `/api/v1/applications/{application_id}` | 현재 작성 상태 또는 완성 JSON 조회 |

### Schema 생성

```http
POST /api/v1/application-schemas
```

```json
{
  "trial_id": "TRIAL-T2D-01",
  "notice_text": "임상시험 공고문 전체 내용"
}
```

### 첫 지원서 제출

```http
POST /api/v1/applications
```

```json
{
  "schema_id": "APP-SCHEMA-TRIAL-T2D-01-...",
  "application_text": "저는 42세 여성이고..."
}
```

### 추가 답변 제출

```http
POST /api/v1/applications/{application_id}/responses
```

```json
{
  "response_text": "알레르기는 없으며 과거 임상시험 참여 경험도 없습니다."
}
```

---

## 12. 저장소와 다른 팀의 연결 지점

현재 `IntakeStore`는 로컬 개발용 메모리 저장소다. API 프로세스가 재시작되면 Schema와 지원서
세션이 사라지며, 여러 API 인스턴스 사이에서 상태를 공유하지 못한다.

운영 연결 전 다음 작업이 필요하다.

1. `IntakeStore`의 공개 메서드 계약을 유지하는 DynamoDB 어댑터 구현
2. Schema 레코드와 지원서 세션의 보존 정책 결정
3. 지원서 데이터 암호화 및 접근 권한 분리
4. `application_id`와 실제 사용자 계정 연결 방식 결정
5. Schema 승인 상태(`DRAFT`, `APPROVED`, `RETIRED`) 추가 검토
6. `COMPLETE` 이벤트를 후속 스크리닝 팀에 전달하는 방식 결정

후속 시스템은 `status == "COMPLETE"`인 응답의 `schema_id`, `trial_id`, `data`를 입력 계약으로
사용할 수 있다.

---

## 13. LLM 단독 처리 가능 범위와 권장 검토

LLM은 다음 작업을 수행할 수 있다.

- 공고문에서 추가 필드 후보 추출
- 필드 이름·설명 생성
- 단일값·다중값 타입 제안
- 자연어 지원서에서 Schema 값 추출
- 추가 답변에서 기존 값의 수정 또는 배열 값 추가

현재 코드의 결정론적 검증은 다음을 담당한다.

- 기본 필드 덮어쓰기 방지
- 필드 이름과 타입 검사
- 중복 필드 및 최대 개수 검사
- Schema 밖의 값 제거
- 숫자 범위와 `enum` 검사
- 누락 필드 계산
- 배열 누적·중복 제거·명시적 초기화
- 완료 상태 결정

그러나 LLM이 선택한 필드의 임상적 의미나 cardinality가 정확한지는 아직 자동으로 보장하지
않는다. 따라서 공고별 Schema는 LLM이 초안을 만들고 공고 담당자가 승인한 뒤 지원서 수집에
사용하는 방식이 권장된다.

---

## 14. 현재 구현 파일

| 파일 | 역할 |
|---|---|
| `app/intake/service.py` | 기본·공고 Schema 생성, 추출, 누락 검사, 답변 병합 |
| `app/intake/store.py` | Schema·지원서 세션 메모리 저장소 |
| `app/intake/__init__.py` | 모듈 공개 인터페이스 |
| `app/schemas.py` | FastAPI 요청·응답 모델 |
| `app/main.py` | 지원서 API 엔드포인트 |
| `app/agent/model.py` | Bedrock 모델 계약과 로컬 스텁 |
| `tests/test_intake.py` | Schema·반복 수집·배열 병합 테스트 |

---

## 15. 미구현 또는 추가 논의가 필요한 항목

- LLM이 선택한 single/multiple 의미를 검증하는 규칙 엔진
- `array<object>` 및 날짜·수치가 결합된 반복 측정값 지원
- 누락 필드마다 개별적이고 자연스러운 질문 생성
- 공고별 Schema의 담당자 승인 워크플로우
- Schema·지원서 세션용 DynamoDB 영속 저장 어댑터
- 사용자 계정과 가명 `application_id` 연결
- 지원서 수정 이력 및 감사 로그
- 동의 철회 및 데이터 삭제 정책
- 완성 지원서를 후속 적격성 판단 시스템에 전달하는 이벤트 계약
