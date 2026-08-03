# Agent

**에이전트 정의·프롬프트·도구 스펙** 작업 공간입니다.

스크리닝 오케스트레이터가 호출하는 에이전트들의 역할, 프롬프트, 노출 도구,
그리고 FM과 규칙의 책임 경계를 문서와 설정으로 관리합니다.

## 설계 원칙

FM은 적격성을 결정하지 않습니다. 동일 입력 → 동일 판정을 보장하기 위해
판정 확정은 결정론적 규칙이 맡습니다.

| 담당 | 주체 |
|------|------|
| 근거 수집 판단, NLI 검증, 설명·확인질문 생성 | Bedrock FM |
| 수치·기간 계산, 기준별 상태 확정, 종합 적격성 판정 | 규칙 (결정론적) |

## 에이전트 목록

| 에이전트 | 역할 | 구현 |
|----------|------|------|
| Evidence Gathering | tool-use 루프로 근거 수집 | `backend/api/app/agent/loop.py` |
| Evidence Verifier | 근거 ↔ 기준 NLI 검증 | `backend/api/app/agent/verifier.py` |
| Next Best Evidence | 정보 가치순 확인 질문 생성 | `backend/api/app/agent/narration.py` |
| Explanation | 판정 근거 설명 생성 | `backend/api/app/agent/narration.py` |
| Medi25 Crawler | 모집공고 수집 (별도 계층) | `crawler/medi25-crawler-agent.md` |

에이전트 조립·상태 관리는 `backend/api/agent/manager.py`,
모델 클라이언트(Bedrock Converse / 스텁)는 `backend/api/app/agent/model.py`,
프롬프트는 `backend/api/app/agent/prompts.py`, 도구 스펙은 `backend/api/app/agent/toolspec.py`에 있습니다.

## 담당 범위

- 에이전트별 역할 명세와 입출력 계약
- 프롬프트 버전 관리 및 변경 이력
- 노출 도구(tool spec) 정의와 권한 범위
- 가드레일 정책, 실패/폴백 시나리오
- 에이전트 응답 품질 평가

## 예정 구조

```
agent/
├── README.md
├── specs/         # 에이전트별 명세 (역할·입출력·도구)
├── prompts/       # 프롬프트 버전 관리
└── eval/          # 응답 품질 평가 시나리오
```

## 다음 작업

- [ ] 에이전트별 명세 문서화 (`specs/`)
- [ ] 프롬프트 버전 규칙 정의
- [ ] 스텁 모델 → Bedrock Converse API 전환 검증
- [ ] 가드레일 위반 케이스 테스트 시나리오
