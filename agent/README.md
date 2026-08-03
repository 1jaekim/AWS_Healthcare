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

## 에이전트 목록

| 에이전트 | 역할 | 구현 |
|----------|------|------|
| Evidence Gathering | tool-use 루프로 근거 수집 | `loop.py` |
| Evidence Verifier | 근거 ↔ 기준 NLI 검증 | `verifier.py` |
| Next Best Evidence | 정보 가치순 확인 질문 생성 | `narration.py` |
| Explanation | 판정 근거 설명 생성 | `narration.py` |
| Intake | 자유 문장 → 이벤트 정규화 | 미구현 (계획) |
| Medi25 Crawler | 모집공고 수집 (별도 계층) | `crawler/medi25-crawler-agent.md` |

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
    model_factory=build_model_client,
).build()
```

실제 호출부는 `backend/api/app/container.py` 입니다.

## 담당 범위

- 에이전트별 역할 명세와 입출력 계약
- 프롬프트 관리 및 변경 이력
- 노출 도구(tool spec) 정의와 권한 범위
- 가드레일 정책, 실패/폴백 시나리오
- 에이전트 응답 품질 평가

## 다음 작업

- [ ] Intake 에이전트 구현 (자유 문장 → 약물·이상반응·날짜 이벤트)
- [ ] 스텁 모델 → Bedrock Converse API 실제 호출 검증
- [ ] 프롬프트 버전 규칙 정의
- [ ] 가드레일 위반 케이스 테스트 시나리오
