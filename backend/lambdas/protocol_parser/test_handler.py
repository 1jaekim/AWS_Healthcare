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
            "inclusion_criteria": [],
            "exclusion_criteria": [],
        },
    )
    saved = {}

    def save(trial_data, source_key):
        saved.update(trial_data)
        return trial_data["trial_id"]

    monkeypatch.setattr(parser, "save_to_dynamodb", save)

    result = parser.handler(
        {
            "bucket": "bucket",
            "source_key": "trials/screenshots/notice.png",
        },
        None,
    )

    assert result["status"] == "success"
    assert result["trial_id"].startswith("SRC-")
    assert saved["trial_id"] == result["trial_id"]
