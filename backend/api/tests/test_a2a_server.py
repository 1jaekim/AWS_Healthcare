"""Protocol-level tests for the independent A2A Lambda application."""

from __future__ import annotations

import asyncio

import httpx
from a2a.client import ClientConfig, ClientFactory
from a2a.helpers.proto_helpers import get_data_parts, new_data_message
from a2a.types import SendMessageRequest, TaskState

from a2a_agents.contracts import REQUEST_SCHEMA, RESPONSE_SCHEMA
from a2a_agents.server import create_a2a_app


class FakeEngine:
    role = "evidence_reviewer"

    def review(self, payload):
        return {
            "schema": RESPONSE_SCHEMA,
            "role": self.role,
            "discussion_id": payload["discussion_id"],
            "round": payload["round"],
            "decisions": [
                {
                    "criterion_id": "T-C01",
                    "recommendation": "OK",
                    "source_ids": ["APPLICATION-1"],
                    "rationale": "The supplied value satisfies the criterion.",
                }
            ],
            "usage": {"input_tokens": 12, "output_tokens": 6},
        }


def test_agent_card_and_jsonrpc_task_round_trip() -> None:
    async def exercise() -> None:
        app = create_a2a_app(FakeEngine())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://reviewer.test",
        ) as http:
            client = await ClientFactory(
                ClientConfig(streaming=False, polling=False, httpx_client=http)
            ).create_from_url("http://reviewer.test")
            task = None
            async for response in client.send_message(
                SendMessageRequest(
                    message=new_data_message(
                        {
                            "schema": REQUEST_SCHEMA,
                            "discussion_id": "DISCUSSION-1",
                            "run_id": "RUN-1",
                            "round": 1,
                            "criteria": [
                                {
                                    "criterion_id": "T-C01",
                                    "source_ids": ["APPLICATION-1"],
                                }
                            ],
                        },
                        media_type="application/json",
                    )
                )
            ):
                if response.HasField("task"):
                    task = response.task

            assert task is not None
            assert task.status.state == TaskState.TASK_STATE_COMPLETED
            result = get_data_parts(task.artifacts[0].parts)[0]
            assert result["schema"] == RESPONSE_SCHEMA
            assert result["decisions"][0]["rationale"]
            assert result["decisions"][0]["source_ids"] == ["APPLICATION-1"]

    asyncio.run(exercise())
