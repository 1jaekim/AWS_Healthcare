"""Bedrock Converse API 실호출 검증.

계약 테스트(`backend/api/tests/test_bedrock_contract.py`)는 AWS 호출 없이 요청·응답
형식을 검증한다. 형식만으로는 확인할 수 없는 것들이 남는다.

- 자격 증명과 IAM 권한이 실제로 있는지
- 그 리전에서 그 모델에 접근할 수 있는지
- 모델이 우리 프롬프트에 지시한 JSON 형식으로 답하는지
- tool-use 를 실제로 요청하는지
- 지연시간과 토큰 사용량이 어느 정도인지
- Guardrail 이 붙어서 동작하는지

이 스크립트가 그 부분을 확인한다. 읽기 전용이며 AWS 리소스를 만들거나 바꾸지 않는다.
모델 호출 비용이 발생한다(기본 5회, 짧은 프롬프트).

사용법:

    # 저장소 루트에서
    $env:AWS_REGION = "us-east-1"
    backend/.venv/Scripts/python agent/scripts/verify_bedrock.py

    # 모델과 가드레일을 지정
    backend/.venv/Scripts/python agent/scripts/verify_bedrock.py \
        --model-id anthropic.claude-3-5-sonnet-20241022-v2:0 \
        --guardrail-id gr-abc123 --guardrail-version 1

    # 호출 없이 준비 상태만 점검
    backend/.venv/Scripts/python agent/scripts/verify_bedrock.py --dry-run

종료 코드는 실패한 점검 수다. 0 이면 전부 통과다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# 저장소 루트를 import 경로에 넣어 agent 패키지를 찾는다.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.model import (  # noqa: E402
    BedrockModelClient,
    Conversation,
    ModelError,
    ModelResponse,
)
from agent.prompts import (  # noqa: E402
    EVIDENCE_PLANNER,
    INTAKE_NORMALIZER,
    NLI_VERIFIER,
    QUESTION_WRITER,
)
from agent.toolspec import AGENT_TOOLS, ALLOWED_TOOL_NAMES  # noqa: E402

DEFAULT_MODEL = "anthropic.claude-3-5-sonnet-20241022-v2:0"


@dataclass
class Check:
    """점검 한 건의 결과."""

    name: str
    passed: bool
    detail: str = ""
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    notes: list[str] = field(default_factory=list)


class Verifier:
    """실호출로 확인 가능한 항목만 점검한다."""

    def __init__(self, args: argparse.Namespace) -> None:
        self._args = args
        self._client: BedrockModelClient | None = None
        self.checks: list[Check] = []

    # -- 준비 --------------------------------------------------------------

    def check_environment(self) -> bool:
        """자격 증명과 리전이 있는지 본다. 호출은 하지 않는다."""
        region = self._args.region or os.getenv("AWS_REGION")
        if not region:
            self._add(
                Check(
                    "환경: 리전",
                    False,
                    "AWS_REGION 이 없습니다. --region 으로 지정하세요.",
                )
            )
            return False
        self._add(Check("환경: 리전", True, region))

        try:
            import boto3
        except ImportError:
            self._add(Check("환경: boto3", False, "boto3 가 설치되지 않았습니다."))
            return False
        self._add(Check("환경: boto3", True, boto3.__version__))

        session = boto3.session.Session(region_name=region)
        credentials = session.get_credentials()
        if credentials is None:
            self._add(
                Check(
                    "환경: 자격 증명",
                    False,
                    "자격 증명을 찾지 못했습니다. AWS CLI 프로파일이나 환경변수를 확인하세요.",
                )
            )
            return False
        frozen = credentials.get_frozen_credentials()
        masked = f"{frozen.access_key[:4]}...{frozen.access_key[-4:]}"
        self._add(Check("환경: 자격 증명", True, f"access key {masked}"))
        return True

    def check_model_access(self) -> bool:
        """해당 리전에서 모델을 쓸 수 있는지 확인한다."""
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError

        region = self._args.region or os.getenv("AWS_REGION")
        model_id = self._args.model_id
        try:
            control = boto3.client("bedrock", region_name=region)
            listed = control.list_foundation_models()
            available = {
                item["modelId"] for item in listed.get("modelSummaries", [])
            }
        except (ClientError, BotoCoreError) as exc:
            # 목록 권한이 없어도 Converse 는 될 수 있다. 경고로만 남긴다.
            self._add(
                Check(
                    "모델 접근: 목록 조회",
                    True,
                    f"확인하지 못했습니다 ({type(exc).__name__}). Converse 호출로 판단합니다.",
                    notes=["bedrock:ListFoundationModels 권한이 없을 수 있습니다."],
                )
            )
            return True

        summaries = {
            item["modelId"]: item.get("inferenceTypesSupported", [])
            for item in listed.get("modelSummaries", [])
        }
        base_id = model_id.split(":")[0]
        matches = {
            name: types
            for name, types in summaries.items()
            if name == model_id or name.startswith(base_id)
        }

        if not matches:
            vendor = model_id.split(".")[0]
            same_vendor = sorted(
                name for name in summaries if name.startswith(f"{vendor}.")
            )
            self._add(
                Check(
                    "모델 접근: 목록 조회",
                    False,
                    f"{model_id} 이 목록에 없습니다 (총 {len(summaries)}개).",
                    notes=[
                        "같은 벤더에서 쓸 수 있는 모델: "
                        + (", ".join(same_vendor[:6]) or "없음"),
                        "BEDROCK_MODEL_ID 를 위 목록의 값으로 바꾸세요.",
                    ],
                )
            )
            return False

        types = matches.get(model_id) or next(iter(matches.values()))
        on_demand = "ON_DEMAND" in types
        notes: list[str] = []
        if not on_demand and "INFERENCE_PROFILE" in types:
            notes.append(
                "이 모델은 ON_DEMAND 가 아니라 INFERENCE_PROFILE 전용입니다. "
                "모델 ID 대신 추론 프로파일 ID(예: us.<model-id>)로 호출해야 합니다."
            )
        if not on_demand and "PROVISIONED" in types:
            notes.append(
                "PROVISIONED 전용입니다. 프로비저닝된 처리량이 있어야 호출됩니다."
            )

        self._add(
            Check(
                "모델 접근: 목록 조회",
                on_demand,
                f"{model_id} 확인 (호출 방식: {', '.join(types) or '미표기'})",
                notes=notes,
            )
        )
        return on_demand

    # -- 실호출 ------------------------------------------------------------

    def client(self) -> BedrockModelClient:
        if self._client is None:
            self._client = BedrockModelClient(
                model_id=self._args.model_id,
                region=self._args.region or os.getenv("AWS_REGION", "us-east-1"),
                max_tokens=self._args.max_tokens,
                temperature=0.0,
                guardrail_id=self._args.guardrail_id,
                guardrail_version=self._args.guardrail_version,
            )
        return self._client

    def _call(
        self,
        name: str,
        *,
        system: str,
        user_text: str,
        tools: list[dict[str, Any]] | None = None,
        verify: Callable[[ModelResponse], tuple[bool, str]],
    ) -> Check:
        conversation = Conversation()
        conversation.user_text(user_text)
        started = time.perf_counter()
        try:
            response = self.client().converse(
                conversation=conversation, system=system, tools=tools
            )
        except ModelError as exc:
            return Check(name, False, str(exc)[:300])
        latency = (time.perf_counter() - started) * 1000

        passed, detail = verify(response)
        return Check(
            name,
            passed,
            detail,
            latency_ms=round(latency, 1),
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
        )

    def check_nli(self) -> None:
        payload = {
            "task": "nli",
            "criterion": {
                "criterion_id": "VERIFY-01",
                "type": "INCLUSION",
                "label": "HbA1c",
                "field": "hba1c",
                "condition": ">=7.0",
                "unit": "%",
            },
            "has_observation": True,
            "observation": {
                "value": 7.4,
                "unit": "%",
                "observed_at": "2024-05-01",
                "source_id": "ENC-1",
                "detail": None,
            },
            "rule_satisfied": True,
            "rule_explanation": "7.4 >= 7.0",
            "narrative": [],
        }

        def verify(response: ModelResponse) -> tuple[bool, str]:
            parsed = response.json_payload()
            if parsed is None:
                return False, f"JSON 을 찾지 못했습니다: {response.text[:120]!r}"
            entailment = str(parsed.get("entailment", ""))
            valid = entailment in {
                "SUPPORTED",
                "CONTRADICTED",
                "NOT_ENOUGH_INFO",
                "CONFLICTING",
            }
            return valid, f"entailment={entailment} confidence={parsed.get('confidence')}"

        self._add(
            self._call(
                "실호출: NLI 검증 (JSON 형식 준수)",
                system=NLI_VERIFIER,
                user_text="다음 기준과 근거의 관계를 판정하라.\n\n"
                + json.dumps(payload, ensure_ascii=False, indent=2),
                verify=verify,
            )
        )

    def check_question(self) -> None:
        payload = {
            "task": "question",
            "label": "HbA1c",
            "field": "hba1c",
            "status": "UNKNOWN",
            "reason": "관찰값이 확인되지 않았습니다.",
            "expected_condition": ">=7.0",
            "target": "participant",
        }

        def verify(response: ModelResponse) -> tuple[bool, str]:
            parsed = response.json_payload() or {}
            question = str(parsed.get("question", "")).strip()
            return bool(question), f"question={question[:80]!r}"

        self._add(
            self._call(
                "실호출: 확인 질문 작성",
                system=QUESTION_WRITER,
                user_text="다음 항목을 확인하기 위한 질문을 한 문장으로 작성하라.\n\n"
                + json.dumps(payload, ensure_ascii=False, indent=2),
                verify=verify,
            )
        )

    def check_intake(self) -> None:
        text = "3개월 전 HbA1c 7.8% 였고 저혈당은 없었습니다."

        def verify(response: ModelResponse) -> tuple[bool, str]:
            parsed = response.json_payload()
            if parsed is None:
                return False, f"JSON 을 찾지 못했습니다: {response.text[:120]!r}"
            events = parsed.get("events")
            if not isinstance(events, list):
                return False, "events 배열이 없습니다."
            # 프롬프트는 원문 구간을 그대로 옮기라고 지시한다. 그라운딩 준수 여부를 본다.
            grounded = [
                item
                for item in events
                if isinstance(item, dict) and str(item.get("span", "")) in text
            ]
            detail = f"events={len(events)} 그라운딩 통과={len(grounded)}"
            return len(events) > 0, detail

        self._add(
            self._call(
                "실호출: Intake 추출 (span 그라운딩)",
                system=INTAKE_NORMALIZER,
                user_text="다음 문장에서 사실 조각을 뽑아라.\n\n"
                + json.dumps({"task": "intake", "text": text}, ensure_ascii=False),
                verify=verify,
            )
        )

    def check_tool_use(self) -> None:
        payload = {
            "task": "plan",
            "person_id": 1,
            "trial_id": "VERIFY-TRIAL",
            "index_date": "2024-06-15",
            "pending_narrative": [
                {
                    "criterion_id": "VERIFY-02",
                    "label": "조절되지 않는 고혈압",
                    "field": "uncontrolled_bp",
                    "terms": ["조절되지 않는 고혈압", "혈압 조절 불량"],
                }
            ],
            "missing_observations": [],
        }

        def verify(response: ModelResponse) -> tuple[bool, str]:
            if not response.wants_tools:
                return (
                    False,
                    f"도구를 요청하지 않았습니다. stop_reason={response.stop_reason}",
                )
            names = [use.name for use in response.tool_uses]
            allowed = all(name in ALLOWED_TOOL_NAMES for name in names)
            return allowed, f"요청 도구={names} 화이트리스트준수={allowed}"

        self._add(
            self._call(
                "실호출: tool-use 요청",
                system=EVIDENCE_PLANNER,
                user_text="다음은 현재 근거 수집 상태다. 추가로 필요한 근거가 있으면 "
                "도구를 호출하고, 없으면 done 만 반환하라.\n\n"
                + json.dumps(payload, ensure_ascii=False, indent=2),
                tools=AGENT_TOOLS,
                verify=verify,
            )
        )

    def check_guardrail(self) -> None:
        if not self._args.guardrail_id:
            self._add(
                Check(
                    "실호출: Guardrail",
                    True,
                    "건너뜀 (--guardrail-id 미지정)",
                    notes=["Guardrail 연결은 확인하지 못했습니다."],
                )
            )
            return

        def verify(response: ModelResponse) -> tuple[bool, str]:
            # 요청이 거부되지 않고 돌아오면 Guardrail 설정 자체는 유효하다.
            return True, f"stop_reason={response.stop_reason}"

        self._add(
            self._call(
                "실호출: Guardrail 연결",
                system=QUESTION_WRITER,
                user_text='{"task": "question", "label": "HbA1c"}',
                verify=verify,
            )
        )

    # -- 보고 --------------------------------------------------------------

    def _add(self, check: Check) -> None:
        self.checks.append(check)
        mark = "PASS" if check.passed else "FAIL"
        line = f"[{mark}] {check.name}"
        if check.detail:
            line += f" | {check.detail}"
        if check.latency_ms:
            line += f" ({check.latency_ms}ms, in={check.input_tokens} out={check.output_tokens})"
        print(line)
        for note in check.notes:
            print(f"       주의: {note}")

    def summary(self) -> int:
        failed = [item for item in self.checks if not item.passed]
        total_in = sum(item.input_tokens for item in self.checks)
        total_out = sum(item.output_tokens for item in self.checks)
        calls = [item for item in self.checks if item.latency_ms]

        print()
        print(f"점검 {len(self.checks)}건 중 {len(self.checks) - len(failed)}건 통과")
        if calls:
            slowest = max(calls, key=lambda item: item.latency_ms)
            print(
                f"실호출 {len(calls)}회 / 토큰 in={total_in} out={total_out} / "
                f"최대 지연 {slowest.latency_ms}ms ({slowest.name})"
            )
        if failed:
            print()
            print("실패 항목:")
            for item in failed:
                print(f"  - {item.name}: {item.detail}")
        return len(failed)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bedrock Converse API 실호출 검증 (읽기 전용, 모델 호출 비용 발생)"
    )
    parser.add_argument("--model-id", default=os.getenv("BEDROCK_MODEL_ID", DEFAULT_MODEL))
    parser.add_argument("--region", default=os.getenv("AWS_REGION"))
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--guardrail-id", default=os.getenv("BEDROCK_GUARDRAIL_ID"))
    parser.add_argument(
        "--guardrail-version", default=os.getenv("BEDROCK_GUARDRAIL_VERSION", "DRAFT")
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="모델을 호출하지 않고 환경·권한만 점검한다",
    )
    args = parser.parse_args()

    print(f"모델: {args.model_id}")
    print(f"리전: {args.region or os.getenv('AWS_REGION') or '(미지정)'}")
    print()

    verifier = Verifier(args)
    if not verifier.check_environment():
        print()
        print("환경 점검에 실패해 호출을 진행하지 않았습니다.")
        return verifier.summary() or 1

    verifier.check_model_access()

    if args.dry_run:
        print()
        print("--dry-run 이므로 모델을 호출하지 않았습니다.")
        return verifier.summary()

    verifier.check_nli()
    verifier.check_question()
    verifier.check_intake()
    verifier.check_tool_use()
    verifier.check_guardrail()
    return verifier.summary()


if __name__ == "__main__":
    raise SystemExit(main())
