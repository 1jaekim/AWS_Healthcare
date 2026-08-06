"""Protocol Parser 문서 입력 및 식별자 회귀 테스트."""

from __future__ import annotations

from . import handler as parser


def test_png_uses_textract_instead_of_binary_decode(monkeypatch) -> None:
    class Textract:
        def detect_document_text(self, **kwargs):
            assert kwargs["Document"]["S3Object"] == {
                "Bucket": "bucket",
                "Name": "trials/screenshots/notice.png",
            }
            return {
                "Blocks": [
                    {"BlockType": "LINE", "Text": "만 19세 이상"},
                    {"BlockType": "WORD", "Text": "무시"},
                    {"BlockType": "LINE", "Text": "건강한 성인 대상"},
                ]
            }

    monkeypatch.setattr(parser, "textract_client", Textract())

    text = parser.extract_text_from_document(
        "bucket", "trials/screenshots/notice.png"
    )

    assert text == "만 19세 이상\n건강한 성인 대상"


def test_unknown_trial_id_is_replaced_with_source_hash(monkeypatch) -> None:
    monkeypatch.setattr(
        parser,
        "extract_text_from_document",
        lambda bucket, key: "선정 기준이 포함된 임상시험 공개 모집 공고입니다." * 3,
    )
    monkeypatch.setattr(
        parser,
        "parse_with_bedrock",
        lambda text: {
            "trial_id": "UNKNOWN",
            "trial_title": "공개 모집 공고",
            "inclusion_criteria": [
                {"id": "INC-001", "description": "만 19세 이상", "structured": None}
            ],
            "exclusion_criteria": [],
        },
    )
    saved = {}

    def save(trial_data, source_key, **kwargs):
        saved.update(trial_data)
        return trial_data["trial_id"]

    monkeypatch.setattr(parser, "save_to_dynamodb", save)

    result = parser.handler(
        {
            "bucket": "bucket",
            "source_key": "trials/documents/notice.txt",
        },
        None,
    )

    assert result["status"] == "success"
    assert result["trial_id"].startswith("SRC-")
    assert saved["trial_id"] == result["trial_id"]


def test_screenshot_is_excluded_before_ocr_or_storage(monkeypatch) -> None:
    monkeypatch.setattr(
        parser,
        "extract_text_from_document",
        lambda *_: (_ for _ in ()).throw(AssertionError("OCR must not run")),
    )
    monkeypatch.setattr(
        parser,
        "save_to_dynamodb",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("screenshot must not be stored as criteria")
        ),
    )

    result = parser.handler(
        {"bucket": "bucket", "source_key": "trials/screenshots/notice.png"},
        None,
    )

    assert result["status"] == "skipped"
    assert result["failure_reason"] == "SCREENSHOT_EXCLUDED"
    assert result["rag_document_key"] is None


def test_korean_ocr_failure_is_recorded_as_needs_fix(monkeypatch) -> None:
    monkeypatch.setattr(
        parser,
        "extract_text_from_document",
        lambda *_: "YYXOB Polly olo eligibility criteria " * 5,
    )
    monkeypatch.setattr(
        parser,
        "parse_with_bedrock",
        lambda *_: (_ for _ in ()).throw(AssertionError("Bedrock must not run")),
    )
    saved = {}

    def save(trial_data, source_key, **kwargs):
        saved.update(trial_data=trial_data, source_key=source_key, **kwargs)
        return trial_data["trial_id"]

    monkeypatch.setattr(parser, "save_to_dynamodb", save)

    result = parser.handler(
        {"bucket": "bucket", "source_key": "trials/documents/kct_bad.pdf"},
        None,
    )

    assert result["status"] == "NEEDS_FIX"
    assert result["quality_issues"] == ["KOREAN_OCR_UNREADABLE"]
    assert saved["status"] == "NEEDS_FIX"
    assert saved["quality_report"]["hangul_ratio"] == 0.0


def test_zero_criteria_is_not_published_and_is_recorded_as_needs_fix(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        parser,
        "extract_text_from_document",
        lambda *_: "선정 및 제외 기준을 포함해야 하는 공개 모집 공고입니다. " * 4,
    )
    monkeypatch.setattr(
        parser,
        "parse_with_bedrock",
        lambda *_: {
            "trial_id": "KCT-ZERO",
            "trial_title": "기준 누락 공고",
            "inclusion_criteria": [],
            "exclusion_criteria": [],
        },
    )
    monkeypatch.setattr(
        parser,
        "publish_reference_document",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("failed quality must not be published")
        ),
    )
    saved = {}

    def save(trial_data, source_key, **kwargs):
        saved.update(trial_data=trial_data, source_key=source_key, **kwargs)
        return trial_data["trial_id"]

    monkeypatch.setattr(parser, "save_to_dynamodb", save)

    result = parser.handler(
        {"bucket": "bucket", "source_key": "trials/documents/kct_zero.txt"},
        None,
    )

    assert result["status"] == "NEEDS_FIX"
    assert "NO_ELIGIBILITY_CRITERIA" in result["quality_issues"]
    assert saved["status"] == "NEEDS_FIX"
