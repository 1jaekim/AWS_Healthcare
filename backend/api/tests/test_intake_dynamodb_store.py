from __future__ import annotations

from copy import deepcopy

from app.intake.store import DynamoDBIntakeStore


class FakeTable:
    def __init__(self) -> None:
        self.items: dict[str, dict] = {}

    def put_item(self, *, Item: dict) -> None:
        self.items[Item["record_id"]] = deepcopy(Item)

    def get_item(self, *, Key: dict, ConsistentRead: bool) -> dict:
        item = self.items.get(Key["record_id"])
        return {"Item": deepcopy(item)} if item else {}


def test_schema_and_application_survive_store_instance_change() -> None:
    table = FakeTable()
    first = DynamoDBIntakeStore(
        table_name="ApplicationStore", region="ap-northeast-2", table=table
    )
    second = DynamoDBIntakeStore(
        table_name="ApplicationStore", region="ap-northeast-2", table=table
    )

    first.save_schema({"schema_id": "SCHEMA-1", "threshold": 7.5})
    first.save_application({"application_id": "APP-1", "data": {"age": 42}})

    assert second.get_schema("SCHEMA-1") == {
        "schema_id": "SCHEMA-1",
        "threshold": 7.5,
    }
    assert second.get_application("APP-1") == {
        "application_id": "APP-1",
        "data": {"age": 42},
    }
