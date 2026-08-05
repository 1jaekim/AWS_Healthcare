from __future__ import annotations

from copy import deepcopy

import pytest

from app.criteria_repository import (
    DynamoDBCriteriaRepository,
    InvalidApproval,
    ReviewAlreadyDecided,
    flatten_protocol_item,
)
from app.intake.trial_schema import TrialSchemaBuilder


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
            },
            {
                "criterion_id": "INC-002",
                "criterion_type": "INCLUSION",
                "field": "bmi",
                "operator": ">=",
                "value_low": "18",
            },
            {
                "criterion_id": "EXC-001",
                "criterion_type": "EXCLUSION",
                "field": "active_pregnancy",
                "operator": "=",
                "value_low": "false",
            },
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
    assert pending[0]["criteria_count"] == 3

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


def test_legacy_range_and_unstructured_criterion_are_preserved() -> None:
    rows = flatten_protocol_item(
        {
            "inclusion_criteria": [
                {
                    "id": "INC-001",
                    "description": "18세 이상 65세 이하",
                    "structured": {
                        "parameter": "age",
                        "operator": "between",
                        "value": "18-65",
                        "unit": "years",
                    },
                },
                {
                    "id": "INC-002",
                    "description": "서면 동의가 가능한 사람",
                    "structured": None,
                },
            ]
        }
    )
    assert (rows[0]["value_low"], rows[0]["value_high"]) == ("18", "65")
    assert rows[1]["field"] == "criterion_inclusion_inc_002"
    assert rows[1]["value_low"] == "true"
    assert rows[1]["label"] == "서면 동의가 가능한 사람"


def test_age_only_notice_cannot_be_approved() -> None:
    item = _item()
    item["criteria"] = item["criteria"][:1]
    repository = DynamoDBCriteriaRepository(
        table_name="test", region="ap-northeast-2", table=FakeTable(item)
    )
    with pytest.raises(InvalidApproval, match="3개 미만"):
        repository.decide_review(
            trial_id="KCT-001",
            source_key="raw/notices/one.png",
            decision="approved",
            reviewed_by="cognito:admin-sub",
        )


def test_unstructured_criteria_become_distinct_boolean_questions() -> None:
    rows = flatten_protocol_item(
        {
            "inclusion_criteria": [
                {"id": "INC-001", "description": "서면 동의 가능", "structured": None}
            ],
            "exclusion_criteria": [
                {"id": "EXC-001", "description": "현재 임신 중", "structured": None}
            ],
        }
    )
    request = TrialSchemaBuilder().build(
        trial={"trial_id": "T", "trial_name": "테스트 공고"}, criteria=rows
    )

    assert [item["name"] for item in request.additional_fields] == [
        "criterion_inclusion_inc_001",
        "criterion_exclusion_exc_001",
    ]
    assert all(item["type"] == "boolean" for item in request.additional_fields)
    assert "서면 동의 가능" in request.additional_fields[0]["description"]
    assert "현재 임신 중" in request.additional_fields[1]["description"]


def test_screenshot_duplicate_is_replaced_by_detailed_document() -> None:
    screenshot = _item()
    screenshot.update(
        trial_id="SRC-old",
        source_key="trials/screenshots/kct_same_20260805.png",
        status="approved",
    )
    screenshot["criteria"] = screenshot["criteria"][:1]
    document = _item()
    document.update(
        trial_id="PROTOCOL-REAL",
        source_key="trials/documents/kct_same.txt",
        status="approved",
    )

    class MultiTable(FakeTable):
        def scan(self, **kwargs):
            return {"Items": [deepcopy(screenshot), deepcopy(document)]}

    repository = DynamoDBCriteriaRepository(
        table_name="test", region="ap-northeast-2", table=MultiTable(document)
    )

    trials = repository.list_trials()
    assert [item["trial_id"] for item in trials] == ["PROTOCOL-REAL"]
    assert trials[0]["criteria_count"] == 3
