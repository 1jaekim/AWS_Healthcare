from __future__ import annotations

from copy import deepcopy

import pytest

from app.criteria_repository import (
    DynamoDBCriteriaRepository,
    InvalidApproval,
    ReviewAlreadyDecided,
)


def _item(*, title: str = "당뇨 임상시험") -> dict:
    return {
        "trial_id": "KCT-001",
        "source_key": "raw/notices/one.png",
        "status": "pending_review",
        "trial_title": title,
        "condition": "당뇨",
        "created_at": 100,
        "updated_at": 100,
        "criteria": [
            {
                "criterion_id": "INC-001",
                "criterion_type": "INCLUSION",
                "field": "age",
                "operator": ">=",
                "value_low": "19",
            }
        ],
    }


class FakeTable:
    def __init__(self, item: dict) -> None:
        self.item = deepcopy(item)

    def query(self, **kwargs):
        if kwargs.get("IndexName") == "status-index":
            return {"Items": [deepcopy(self.item)] if self.item["status"] == "pending_review" else []}
        return {"Items": [deepcopy(self.item)]}

    def get_item(self, **kwargs):
        return {"Item": deepcopy(self.item)}

    def scan(self, **kwargs):
        return {"Items": [deepcopy(self.item)]}

    def update_item(self, **kwargs):
        if self.item["status"] != "pending_review":
            raise AssertionError("조건부 갱신 전에 상태 확인이 누락됨")
        values = kwargs["ExpressionAttributeValues"]
        self.item.update(
            status=values[":decision"],
            reviewed_at=values[":now"],
            reviewed_by=values[":actor"],
            review_note=values[":note"],
            updated_at=values[":now"],
        )
        return {"Attributes": deepcopy(self.item)}


def test_pending_notice_can_be_approved_with_audit_fields() -> None:
    table = FakeTable(_item())
    repository = DynamoDBCriteriaRepository(table_name="test", region="ap-northeast-2", table=table)

    pending = repository.list_for_review()
    assert pending[0]["criteria_count"] == 1

    approved = repository.decide_review(
        trial_id="KCT-001",
        source_key="raw/notices/one.png",
        decision="approved",
        reviewed_by="cognito:admin-sub",
        note="원문 대조 완료",
    )
    assert approved is not None
    assert approved["status"] == "approved"
    assert approved["reviewed_by"] == "cognito:admin-sub"
    assert repository.list_trials()[0]["trial_id"] == "KCT-001"


def test_unknown_title_cannot_be_approved() -> None:
    repository = DynamoDBCriteriaRepository(
        table_name="test", region="ap-northeast-2", table=FakeTable(_item(title="UNKNOWN"))
    )
    with pytest.raises(InvalidApproval):
        repository.decide_review(
            trial_id="KCT-001",
            source_key="raw/notices/one.png",
            decision="approved",
            reviewed_by="cognito:admin-sub",
        )


def test_already_decided_notice_cannot_be_changed() -> None:
    item = _item()
    item["status"] = "rejected"
    repository = DynamoDBCriteriaRepository(
        table_name="test", region="ap-northeast-2", table=FakeTable(item)
    )
    with pytest.raises(ReviewAlreadyDecided):
        repository.decide_review(
            trial_id="KCT-001",
            source_key="raw/notices/one.png",
            decision="approved",
            reviewed_by="cognito:admin-sub",
        )
