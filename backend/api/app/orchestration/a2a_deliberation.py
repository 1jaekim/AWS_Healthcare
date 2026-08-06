"""Remote A2A deliberation client for the Matching API runtime."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Protocol, Sequence

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.helpers.proto_helpers import get_data_parts, new_data_message
from a2a.types import SendMessageRequest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session

from agent.contracts import CriterionResult, CriterionStatus, Tracer
from agent.deliberation import DeliberationResult, UnknownDeliberationAgent

from a2a_agents.contracts import REQUEST_SCHEMA, RESPONSE_SCHEMA


_OPEN_STATUSES = frozenset(
    {
        CriterionStatus.UNKNOWN,
        CriterionStatus.CONFLICTING,
        CriterionStatus.REVIEW_REQUIRED,
    }
)


class A2AError(RuntimeError):
    """The remote agent did not return a valid completed A2A task."""


class ReviewTransport(Protocol):
    def review(self, url: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class LambdaFunctionUrlSigV4(httpx.Auth):
    """Sign httpx requests for an AWS_IAM Lambda Function URL."""

    requires_request_body = True

    def __init__(self, *, region: str, session: Session | None = None) -> None:
        self._region = region
        self._session = session or Session()

    def auth_flow(self, request: httpx.Request):
        credentials = self._session.get_credentials()
        if credentials is None:
            raise A2AError("AWS credentials are unavailable for A2A invocation")
        aws_request = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers=dict(request.headers),
        )
        SigV4Auth(credentials.get_frozen_credentials(), "lambda", self._region).add_auth(
            aws_request
        )
        request.headers.update(dict(aws_request.headers))
        yield request


class A2AHttpTransport:
    def __init__(self, *, region: str, timeout_seconds: float = 90.0) -> None:
        self._region = region
        self._timeout_seconds = timeout_seconds

    def review(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._review(url, payload))
        raise A2AError("synchronous A2A client cannot run on an active event loop")

    async def _review(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(
            auth=LambdaFunctionUrlSigV4(region=self._region),
            timeout=self._timeout_seconds,
        ) as http:
            client = await ClientFactory(
                ClientConfig(streaming=False, polling=False, httpx_client=http)
            ).create_from_url(url.rstrip("/"))
            final_task = None
            request = SendMessageRequest(
                message=new_data_message(payload, media_type="application/json")
            )
            async for response in client.send_message(request):
                if response.HasField("task"):
                    final_task = response.task

        if final_task is None:
            raise A2AError("agent returned no task")
        for artifact in reversed(final_task.artifacts):
            data_parts = get_data_parts(artifact.parts)
            if data_parts and isinstance(data_parts[-1], dict):
                result = data_parts[-1]
                if result.get("schema") != RESPONSE_SCHEMA:
                    raise A2AError("agent returned an unsupported response schema")
                return result
        raise A2AError("completed task contains no structured review artifact")


class A2ADeliberationAgent:
    """Coordinate two independent agents and arbitrate their grounded outputs."""

    def __init__(
        self,
        *,
        reviewer_url: str,
        challenger_url: str,
        transport: ReviewTransport,
        trace: Tracer,
        max_criteria: int = 5,
    ) -> None:
        self._reviewer_url = reviewer_url
        self._challenger_url = challenger_url
        self._transport = transport
        self._trace = trace
        self._max_criteria = min(5, max(1, max_criteria))

    def deliberate(
        self,
        results: Sequence[CriterionResult],
        *,
        run_id: str | None = None,
    ) -> DeliberationResult:
        open_items = sorted(
            (item for item in results if item.status in _OPEN_STATUSES),
            key=UnknownDeliberationAgent._review_priority,
        )
        selected = open_items[: self._max_criteria]
        outcome = DeliberationResult()
        if not selected:
            return outcome

        discussion_id = str(uuid.uuid4())
        criteria = [UnknownDeliberationAgent._criterion_payload(item) for item in selected]
        base = {
            "schema": REQUEST_SCHEMA,
            "discussion_id": discussion_id,
            "run_id": run_id or "unattached",
            "criteria": criteria,
        }
        try:
            first = self._call(
                self._reviewer_url,
                {**base, "round": 1},
                role="evidence_reviewer",
                run_id=run_id,
                round_number=1,
            )
            outcome.rounds = 1
            outcome.input_tokens += int(first.get("usage", {}).get("input_tokens", 0))
            outcome.output_tokens += int(first.get("usage", {}).get("output_tokens", 0))
            second = self._call(
                self._challenger_url,
                {**base, "round": 2, "prior_review": first["decisions"]},
                role="challenge_reviewer",
                run_id=run_id,
                round_number=2,
            )
            outcome.rounds = 2
            outcome.input_tokens += int(second.get("usage", {}).get("input_tokens", 0))
            outcome.output_tokens += int(second.get("usage", {}).get("output_tokens", 0))
        except Exception as exc:  # noqa: BLE001 - fail closed to human review
            outcome.stopped_reason = "a2a_error"
            outcome.error = f"{type(exc).__name__}: {exc}"
            return outcome

        left = self._decision_map(first)
        right = self._decision_map(second)
        outcome.items = UnknownDeliberationAgent._arbitrate(selected, left, right)
        outcome.stopped_reason = (
            "criteria_limit" if len(open_items) > self._max_criteria else "bounded_consensus"
        )
        return outcome

    def _call(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        role: str,
        run_id: str | None,
        round_number: int,
    ) -> dict[str, Any]:
        with self._trace.span(
            run_id or "unattached",
            "a2a:deliberation",
            "AGENT",
            protocol="A2A/1.0",
            role=role,
            round=round_number,
            endpoint=url,
            discussion_id=payload["discussion_id"],
        ) as attributes:
            response = self._transport.review(url, payload)
            if response.get("schema") != RESPONSE_SCHEMA:
                raise A2AError("unsupported response schema")
            if response.get("role") != role:
                raise A2AError(f"expected {role}, received {response.get('role')}")
            if response.get("discussion_id") != payload["discussion_id"]:
                raise A2AError("discussion_id mismatch")
            if int(response.get("round", 0) or 0) != round_number:
                raise A2AError("round mismatch")
            if not isinstance(response.get("decisions"), list):
                raise A2AError("decisions are missing")
            attributes["decision_count"] = len(response["decisions"])
            return response

    @staticmethod
    def _decision_map(response: dict[str, Any]) -> dict[str, dict[str, Any]]:
        decisions: dict[str, dict[str, Any]] = {}
        for item in response.get("decisions", []):
            if not isinstance(item, dict) or not item.get("criterion_id"):
                continue
            normalized = dict(item)
            recommendation = str(normalized.get("recommendation", "UNKNOWN")).upper()
            rationale = str(normalized.get("rationale", "")).strip()
            if recommendation not in {"OK", "NOT_OK", "UNKNOWN"}:
                recommendation = "UNKNOWN"
            if recommendation in {"OK", "NOT_OK"} and not rationale:
                recommendation = "UNKNOWN"
            normalized["recommendation"] = recommendation
            normalized["rationale"] = rationale
            decisions[str(normalized["criterion_id"])] = normalized
        return decisions


__all__ = [
    "A2ADeliberationAgent",
    "A2AError",
    "A2AHttpTransport",
    "LambdaFunctionUrlSigV4",
]
