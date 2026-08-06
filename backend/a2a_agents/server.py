"""A2A 1.0 FastAPI server for an independent deliberation role."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any, Protocol

from a2a.helpers.proto_helpers import (
    get_data_parts,
    new_data_part,
    new_task,
    new_text_message,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.request_handlers.response_helpers import agent_card_to_dict
from a2a.server.routes import add_a2a_routes_to_fastapi, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    TaskState,
)
from fastapi import FastAPI, Request

from .contracts import ContractError


class ReviewEngine(Protocol):
    role: str

    def review(self, payload: Any) -> dict[str, Any]: ...


class DeliberationExecutor(AgentExecutor):
    def __init__(self, engine: ReviewEngine) -> None:
        self._engine = engine

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        if context.current_task is None:
            await event_queue.enqueue_event(
                new_task(
                    context.task_id,
                    context.context_id,
                    TaskState.TASK_STATE_SUBMITTED,
                    history=[context.message] if context.message else [],
                )
            )
        await updater.start_work()
        try:
            parts = get_data_parts(context.message.parts) if context.message else []
            if len(parts) != 1:
                raise ContractError("exactly one JSON data part is required")
            result = await asyncio.to_thread(self._engine.review, parts[0])
            await updater.add_artifact(
                parts=[new_data_part(result, media_type="application/json")],
                name="deliberation-review",
            )
            await updater.complete()
        except Exception as exc:  # noqa: BLE001 - turn failure into A2A task state
            await updater.failed(
                new_text_message(f"{type(exc).__name__}: {exc}")
            )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()


def _card(*, role: str, url: str) -> AgentCard:
    label = "Evidence Reviewer" if role == "evidence_reviewer" else "Challenge Reviewer"
    return AgentCard(
        name=f"Clinical Trial {label}",
        description=(
            "Reviews unresolved clinical-trial criteria and returns grounded "
            "recommendations with rationale and source identifiers."
        ),
        supported_interfaces=[
            AgentInterface(
                url=url,
                protocol_binding="JSONRPC",
                protocol_version="1.0",
            )
        ],
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=[
            AgentSkill(
                id="review-unresolved-criteria",
                name=label,
                description="Evaluate unresolved criteria using only supplied evidence.",
                tags=["clinical-trial", "eligibility", "evidence", "explanation"],
                input_modes=["application/json"],
                output_modes=["application/json"],
            )
        ],
    )


def create_a2a_app(engine: ReviewEngine) -> FastAPI:
    placeholder_card = _card(role=engine.role, url="http://localhost/")
    handler = DefaultRequestHandler(
        agent_executor=DeliberationExecutor(engine),
        task_store=InMemoryTaskStore(),
        agent_card=placeholder_card,
    )
    app = FastAPI(title=f"Clinical Trial A2A {engine.role}")
    add_a2a_routes_to_fastapi(
        app,
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/"),
    )

    @app.get("/.well-known/agent-card.json")
    async def agent_card(request: Request) -> dict[str, Any]:
        card = deepcopy(placeholder_card)
        card.supported_interfaces[0].url = str(request.base_url)
        return agent_card_to_dict(card)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "role": engine.role}

    return app
