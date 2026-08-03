# Agent

스크리닝 오케스트레이터가 호출하는 **에이전트 계층**입니다.
모델 클라이언트, 도구 스키마, tool-use 루프, FM 검증·설명 생성을 담고 있습니다.

이 패키지는 `backend` 를 import 하지 않습니다. 필요한 동작은 `contracts.py` 에
Protocol 로 선언하고, 호출자가 구현을 주입합니다. 의존성은 `backend → agent`
한 방향입니다.

```python
>>> import agent.manager        # backend 없이 단독으로 import 된다
```

## 설계 원칙

FM은 적격성을 결정하지 않습니다. 동일 입력 → 동일 판정을 보장하기 위해
판정 확정은 결정론적 규칙이 맡습니다.

| 담당 | 주체 |
|------|------|
| 근거 수집 판단, NLI 검증, 설명·확인질문 생성 | Bedrock FM |
| 수치·기간 계산, 기준별 상태 확정, 종합 적격성 판정 | 규칙 (결정론적) |

## 구성

| 파일 | 역할 |
|------|------|
| `contracts.py` | 외부 계약(포트)과 공유 용어. 이 패키지 밖을 import 하지 않는다 |
| `manager.py` | 에이전트 조립과 상태 관리. 폴백 구현을 주입받는다 |
| `model.py` | 모델 클라이언트 (Bedrock Converse / 결정론적 스텁) |
| `prompts.py` | 시스템 프롬프트 |
| `toolspec.py` | 모델에 노출하는 도구 스키마와 화이트리스트 |
| `loop.py` | tool-use 루프로 근거 보강 |
| `verifier.py` | 근거 ↔ 기준 NLI 검증 |
| `narration.py` | 판정 설명 생성, 확인 질문 작성 |
| `intake.py` | 자유 문장 → 측정값·약물·이상반응·상태 이벤트 정규화 |

## 에이전트 목록

| 에이전트 | 역할 | 구현 |
|----------|------|------|
| Evidence Gathering | tool-use 루프로 근거 수집 | `loop.py` |
| Evidence Verifier | 근거 ↔ 기준 NLI 검증 | `verifier.py` |
| Next Best Evidence | 정보 가치순 확인 질문 생성 | `narration.py` |
| Explanation | 판정 근거 설명 생성 | `narration.py` |
| Intake | 자유 문장 → 이벤트 정규화 | `intake.py` |
| Medi25 Crawler | 모집공고 수집 (별도 계층) | `crawler/medi25-crawler-agent.md` |

## Intake 에이전트

참여자나 의료진이 쓴 문장은 그대로는 판정에 쓸 수 없습니다. Intake 가 문장을
네 종류의 이벤트로 바꿔 놓으면 이후 계층이 관찰값처럼 다룰 수 있습니다.

| 이벤트 | 예시 입력 | 결과 |
|--------|-----------|------|
| `MEASUREMENT` | `3개월 전 HbA1c 7.8%` | `hba1c=7.8 %` / `2024-03-15` |
| `MEASUREMENT` | `혈압 150/95 mmHg` | `systolic_bp=150`, `diastolic_bp=95` 두 건 |
| `MEDICATION` | `지난달부터 metformin 을 중단했습니다` | `metformin=STOPPED` / `2024-05-15` |
| `ADVERSE_EVENT` | `저혈당은 없었습니다` | `hypoglycemia=False` |
| `CONDITION` | `임신 중입니다` | `active_pregnancy=True` |

### 역할 분리

여기서도 FM 은 판정하지 않습니다. 나아가 **계산도 하지 않습니다**.

| 담당 | 주체 |
|------|------|
| 문장에서 조각 추출 (무엇이 적혀 있는가) | FM |
| 날짜 계산 (`3개월 전` → ISO 날짜) | 규칙 |
| 필드 정교화, 단위 확인, 범위 검사 | 규칙 |
| 부정 표현 판단 | 규칙 (문장 단위) |

FM 이 날짜를 계산하면 같은 문장에 다른 날짜가 나올 수 있습니다. 그래서 프롬프트는
원문 표현(`when`)을 그대로 옮기라고 지시하고, 변환은 `parse_when()` 이 전담합니다.

### 통과 조건

- **그라운딩**: `span` 이 원문에 문자 그대로 없으면 이벤트를 버립니다. 버린 항목은
  `dropped` 에 이유와 함께 남습니다. 모델이 지어낸 값이 조용히 통과하지 않습니다.
- **중복 제거**: 모델 용어를 에이전트 어휘로 정규화한 뒤(`당화혈색소` → `hba1c`)
  규칙 결과와 겹치면 규칙 쪽을 남깁니다.
- **확신도 상한**: 모델 추출은 규칙 확인을 거치지 않았으므로 0.8 을 넘지 않습니다.
- **검토 승격**: 단위 불일치, 범위 이탈, 해석 못한 시점, 기준일보다 뒤의 날짜는
  버리지 않고 `needs_review=True` 로 올립니다.

측정값은 문장에 숫자가 적혀 있으면 사실로 봅니다. 같은 문장에 `없음` 이 있어도
값을 뒤집지 않습니다. 부정은 이상반응·상태에만 적용됩니다.

### 모델 없이도 동작

규칙 추출기(`RuleIntakeExtractor`)가 항상 먼저 돌기 때문에 모델이 없거나 실패해도
결과가 나옵니다. 그래서 `intake_agent` 는 Bedrock 이 꺼져 있어도 `enabled=True` 입니다.
모델 호출이 실패하면 `error` 에 이유가 남고 규칙 결과는 그대로 유지됩니다.

카탈로그에 없는 용어(체중, 크레아티닌, 수축기 혈압)는 `field=None` 로 통과합니다.
기록은 남고 판정에는 쓰이지 않습니다.

### 호출 지점

| 경로 | 용도 |
|------|------|
| `POST /api/v1/intake/normalize` | 문장 하나를 직접 정규화 |
| `POST /api/v1/patients/{id}/answers` | 확인 질문 답변을 저장하며 함께 정규화 |

`/answers` 는 원문을 그대로 보관하고 해석을 `intake` 로 덧붙입니다. 상대 시점의
기준일은 실행의 인덱스 방문일입니다.

## 계약 (contracts.py)

호출자가 주입해야 하는 포트입니다.

| 포트 | 주입되는 구현 (backend) |
|------|------------------------|
| `Tracer` | `app.safety.observability.TraceCollector` |
| `Guardrail` | `app.safety.guardrails.LocalGuardrail` |
| `ToolGateway` | `app.orchestration.gateway.ToolGateway` |
| `EvidenceVerifier` | `app.reasoning.verifier.LocalEvidenceVerifier` |
| `ExplanationWriter` | `app.actions.explanation.ExplanationAgent` |
| `NextBestEvidenceWriter` | `app.actions.next_best.NextBestEvidenceAgent` |
| `FieldResolver` | `app.domain.intake_vocabulary.CatalogFieldResolver` |
| `AgentSettings` | `app.config.ModelSettings` |

데이터 형태(`EvidenceBundle`, `CriterionResult`, `ExecutionPlan` 등)도 Protocol로
선언되어 런타임 의존성이 없습니다. 구체 타입이 필요한 두 가지는 예외입니다.

- `CriterionStatus`: 에이전트가 만든 상태값으로 호출자가 분기하므로 같은 enum
  객체를 공유해야 합니다. 이 모듈이 정의하고 `app.domain.states` 가 가져다 씁니다.
- `ToolPermissionDenied`: 루프가 잡아야 하는 예외입니다.
  `app.tools.base.PermissionDenied` 가 이 예외를 상속합니다.

에이전트가 결과 객체를 만들 때는 폴백이 돌려준 객체를 `dataclasses.replace` 로
복사해 필드만 바꿉니다. 그래서 구체 dataclass를 import 할 필요가 없습니다.

## 조립

`AgentManager` 가 모델 클라이언트를 해석하고 에이전트를 묶어 돌려줍니다.
모델이 없거나 붙지 못하면 주입된 폴백 구현으로 내려앉습니다.

```python
agents = AgentManager(
    config=model_settings,
    trace=trace,
    guardrail=guardrail,
    gateway=gateway,
    local_verifier=LocalEvidenceVerifier(...),
    local_explainer=ExplanationAgent(...),
    local_next_best=NextBestEvidenceAgent(...),
    narration_guardrail=LocalGuardrail(attach_disclaimer=False),
    field_resolver=CatalogFieldResolver(),
    model_factory=build_model_client,
).build()

agents.verifier    # 근거 검증
agents.explainer   # 설명 생성
agents.next_best   # 확인 질문
agents.gatherer    # 근거 수집 루프 (모델 없으면 None)
agents.intake      # 자유 문장 정규화 (모델 없어도 동작)
```

실제 호출부는 `backend/api/app/container.py` 입니다.

## 담당 범위

- 에이전트별 역할 명세와 입출력 계약
- 프롬프트 관리 및 변경 이력
- 노출 도구(tool spec) 정의와 권한 범위
- 가드레일 정책, 실패/폴백 시나리오
- 에이전트 응답 품질 평가

## 다음 작업

- [x] Intake 에이전트 구현 (자유 문장 → 측정값·약물·이상반응·상태 이벤트)
- [ ] Intake 이벤트를 관찰값으로 승격해 미해소 기준 재판정에 반영
- [ ] 스텁 모델 → Bedrock Converse API 실제 호출 검증
- [ ] 프롬프트 버전 규칙 정의
- [ ] 가드레일 위반 케이스 테스트 시나리오
