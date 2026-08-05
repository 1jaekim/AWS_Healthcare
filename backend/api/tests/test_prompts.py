"""프롬프트 버전 규칙 검증.

이 파일의 목적은 프롬프트가 조용히 바뀌는 것을 막는 것이다.

프롬프트 본문을 고치면 체크섬이 바뀌어 `LOCKED` 와 어긋나고 테스트가 깨진다.
그때 해야 하는 일은 아래 둘 중 하나다.

- 출력 계약(JSON 필드·허용값)이 바뀌었다면 MAJOR 를 올리고 호출부 파싱도 고친다.
- 문구만 바꿨다면 MINOR 를 올린다.

그다음 `LOCKED` 의 버전과 체크섬을 갱신한다. 체크섬을 손으로 계산하지 말고
실패 메시지에 찍힌 값을 옮긴다.
"""

from __future__ import annotations

import copy
import pickle
import re

import pytest
from fastapi.testclient import TestClient

from agent.prompts import (
    CRITERION_JUDGE,
    EVIDENCE_PLANNER,
    EXPLANATION_ADMIN,
    EXPLANATION_PATIENT,
    INTAKE_NORMALIZER,
    NLI_VERIFIER,
    PROMPTS,
    QUESTION_WRITER,
    UNKNOWN_DELIBERATION,
    Prompt,
    prompt_versions,
)
from app.main import app

# ---------------------------------------------------------------------------
# 잠금 목록: (버전, 체크섬)
# ---------------------------------------------------------------------------

LOCKED: dict[str, tuple[str, str]] = {
    "criterion_judge": ("1.1", "d2c878918a12"),
    "evidence_planner": ("1.0", "e80b7eb3c1ac"),
    "explanation_admin": ("1.1", "6c6463c12609"),
    "explanation_patient": ("1.1", "09f63b6bda66"),
    "intake_normalizer": ("1.0", "4d4108db0a8d"),
    "nli_verifier": ("1.0", "e426b2f7ec26"),
    "question_writer": ("1.0", "69eb08054734"),
    "unknown_deliberation": ("1.0", "cd12314e1b3e"),
}


def test_잠금_목록과_등록_목록이_일치한다() -> None:
    assert set(PROMPTS) == set(LOCKED), (
        "프롬프트를 추가·삭제했다면 LOCKED 도 함께 고쳐야 한다."
    )


@pytest.mark.parametrize("prompt_id", sorted(LOCKED))
def test_프롬프트_본문이_바뀌면_버전을_올려야_한다(prompt_id: str) -> None:
    prompt = PROMPTS[prompt_id]
    version, checksum = LOCKED[prompt_id]
    assert prompt.checksum == checksum, (
        f"{prompt_id} 본문이 바뀌었습니다. 버전을 올리고 LOCKED 를 "
        f"('{prompt.version}', '{prompt.checksum}') 로 갱신하세요."
    )
    assert prompt.version == version


# ---------------------------------------------------------------------------
# 버전 표기 규칙
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prompt_id", sorted(LOCKED))
def test_버전은_MAJOR_MINOR_형식이다(prompt_id: str) -> None:
    assert re.fullmatch(r"\d+\.\d+", PROMPTS[prompt_id].version)


@pytest.mark.parametrize("prompt_id", sorted(LOCKED))
def test_모든_프롬프트는_출력_계약을_적어둔다(prompt_id: str) -> None:
    """MAJOR 를 올려야 하는 변경인지 판단할 근거가 있어야 한다."""
    assert PROMPTS[prompt_id].contract.strip()


def test_id는_등록_키와_같다() -> None:
    for key, prompt in PROMPTS.items():
        assert prompt.id == key


def test_체크섬은_본문에서_산출된다() -> None:
    import hashlib

    for prompt in PROMPTS.values():
        expected = hashlib.sha256(str(prompt).encode("utf-8")).hexdigest()[:12]
        assert prompt.checksum == expected


def test_본문이_다르면_체크섬도_다르다() -> None:
    checksums = {prompt.checksum for prompt in PROMPTS.values()}
    assert len(checksums) == len(PROMPTS)


def test_한_글자만_바뀌어도_체크섬이_달라진다() -> None:
    edited = Prompt(
        str(NLI_VERIFIER) + " ", id="nli_verifier", version="1.0", contract="x"
    )
    assert edited.checksum != NLI_VERIFIER.checksum


# ---------------------------------------------------------------------------
# 문자열로 그대로 쓰일 수 있어야 한다
# ---------------------------------------------------------------------------


def test_프롬프트는_문자열처럼_쓰인다() -> None:
    """호출부는 Converse 요청에 그대로 넣는다. str 계약이 깨지면 안 된다."""
    assert isinstance(NLI_VERIFIER, str)
    assert [{"text": NLI_VERIFIER}] == [{"text": str(NLI_VERIFIER)}]
    assert NLI_VERIFIER.startswith("당신은")


def test_복사와_직렬화가_동작한다() -> None:
    """요청 dict 를 deepcopy 하거나 캐시에 담는 경로에서 깨지지 않아야 한다."""
    assert copy.copy(NLI_VERIFIER) is NLI_VERIFIER
    assert copy.deepcopy({"system": NLI_VERIFIER})["system"] is NLI_VERIFIER

    restored = pickle.loads(pickle.dumps(NLI_VERIFIER))
    assert str(restored) == str(NLI_VERIFIER)
    assert (restored.id, restored.version, restored.checksum) == (
        NLI_VERIFIER.id,
        NLI_VERIFIER.version,
        NLI_VERIFIER.checksum,
    )


def test_label은_id와_버전을_함께_보여준다() -> None:
    assert NLI_VERIFIER.label == f"{NLI_VERIFIER.id}@{NLI_VERIFIER.version}"


# ---------------------------------------------------------------------------
# 내용 불변 조건
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        EVIDENCE_PLANNER,
        CRITERION_JUDGE,
        NLI_VERIFIER,
        EXPLANATION_ADMIN,
        EXPLANATION_PATIENT,
        QUESTION_WRITER,
        INTAKE_NORMALIZER,
        UNKNOWN_DELIBERATION,
    ],
    ids=lambda item: item.id,
)
def test_모든_프롬프트에_공통_제약이_들어있다(prompt: Prompt) -> None:
    """의료적 확정 표현 금지 문구가 빠진 프롬프트가 있으면 안 된다."""
    assert "의료적 진단이나 치료 권고를 하지 않는다" in prompt
    assert "추측해 채우지 않는다" in prompt


def test_판정을_하지_않는다는_지시가_들어있다() -> None:
    assert "판정하지 않는다" in EVIDENCE_PLANNER or "판정은" in EVIDENCE_PLANNER
    assert "최종 적격성을 결정하지 않는다" in NLI_VERIFIER


def test_intake_프롬프트는_날짜_계산을_금지한다() -> None:
    """날짜 계산은 규칙이 한다. 프롬프트가 이 경계를 명시해야 한다."""
    assert "직접 계산하지 않는다" in INTAKE_NORMALIZER
    assert "글자 그대로" in INTAKE_NORMALIZER


def test_참여자용_설명은_확정_인상을_금지한다() -> None:
    assert "참여가 확정되었다는 인상을 주지 않는다" in EXPLANATION_PATIENT


# ---------------------------------------------------------------------------
# 노출 경로
# ---------------------------------------------------------------------------


def test_prompt_versions는_정렬된_목록을_낸다() -> None:
    versions = prompt_versions()
    assert [item["id"] for item in versions] == sorted(LOCKED)
    for item in versions:
        assert set(item) == {"id", "version", "contract", "checksum", "length"}
        assert item["length"] > 0


def test_아키텍처_응답에_프롬프트_버전이_노출된다() -> None:
    with TestClient(app) as client:
        body = client.get("/api/v1/architecture").json()
    prompts = {item["id"]: item for item in body["agent"]["prompts"]}
    assert set(prompts) == set(LOCKED)
    for prompt_id, (version, checksum) in LOCKED.items():
        assert prompts[prompt_id]["version"] == version
        assert prompts[prompt_id]["checksum"] == checksum


def test_모델_호출_스팬에_프롬프트_버전이_남는다() -> None:
    """어떤 프롬프트로 낸 판정인지 Trace 로 되짚을 수 있어야 한다."""
    from agent.intake import IntakeAgent
    from agent.model import ModelResponse
    from app.safety.guardrails import LocalGuardrail
    from app.safety.observability import TraceCollector

    class Silent:
        mode = "silent"

        def converse(self, *, conversation, system, tools=None):
            return ModelResponse(text='{"events": []}')

    trace = TraceCollector()
    IntakeAgent(
        guardrail=LocalGuardrail(attach_disclaimer=False),
        trace=trace,
        model=Silent(),
    ).normalize("HbA1c 7.2%", reference_date="2024-06-15", run_id="RUN-P")

    span = next(
        item
        for item in trace.spans_for("RUN-P")
        if item["name"] == "model:intake_normalizer"
    )
    assert span["attributes"]["prompt"] == INTAKE_NORMALIZER.label
    assert span["attributes"]["prompt_checksum"] == INTAKE_NORMALIZER.checksum
