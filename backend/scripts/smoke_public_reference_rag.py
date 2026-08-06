"""배포된 공개문서 GraphRAG의 색인과 Retrieve를 끝까지 확인한다."""

from __future__ import annotations

import argparse
import json
import time

import boto3


def invoke(function_name: str, payload: dict) -> dict:
    response = boto3.client("lambda", region_name="ap-northeast-2").invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    body = json.loads(response["Payload"].read())
    if response.get("FunctionError"):
        raise RuntimeError(body)
    return body


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--knowledge-base-id", required=True)
    parser.add_argument("--trial-id", required=True)
    parser.add_argument(
        "--ingestion-function", default="healthcare-graphrag-ingestion"
    )
    parser.add_argument("--timeout-seconds", type=int, default=480)
    args = parser.parse_args()

    started = invoke(
        args.ingestion_function,
        {
            "action": "start",
            "description": f"Public reference smoke test: {args.trial_id}",
        },
    )
    job_id = started["ingestion_job_id"]
    deadline = time.monotonic() + args.timeout_seconds
    status = started["status"]
    while status not in {"COMPLETE", "FAILED", "STOPPED"}:
        if time.monotonic() >= deadline:
            raise TimeoutError(f"ingestion job timed out: {job_id}")
        time.sleep(15)
        polled = invoke(
            args.ingestion_function,
            {"action": "get", "ingestion_job_id": job_id},
        )
        status = polled["status"]
    if status != "COMPLETE":
        raise RuntimeError(f"ingestion failed: {polled}")

    request = {
        "knowledgeBaseId": args.knowledge_base_id,
        "retrievalQuery": {
            "text": "공개 임상시험 공고의 선정 기준과 제외 기준"
        },
        "retrievalConfiguration": {
            "vectorSearchConfiguration": {
                "numberOfResults": 5,
                "filter": {
                    "andAll": [
                        {
                            "equals": {
                                "key": "document_type",
                                "value": "trial_notice",
                            }
                        },
                        {
                            "equals": {
                                "key": "trial_id",
                                "value": args.trial_id,
                            }
                        },
                    ]
                },
            }
        },
    }
    response = boto3.client(
        "bedrock-agent-runtime", region_name="us-east-1"
    ).retrieve(**request)
    results = response.get("retrievalResults", [])
    if not results:
        raise RuntimeError("filtered public reference Retrieve returned no results")
    if any(
        item.get("metadata", {}).get("document_type") != "trial_notice"
        or item.get("metadata", {}).get("trial_id") != args.trial_id
        for item in results
    ):
        raise RuntimeError("Retrieve returned a document outside the public trial filter")

    print(
        json.dumps(
            {
                "ingestion_job_id": job_id,
                "ingestion_status": status,
                "trial_id": args.trial_id,
                "result_count": len(results),
                "sources": [
                    item.get("metadata", {}).get("source_id") for item in results
                ],
                "patient_identifier_in_request": any(
                    token in json.dumps(request)
                    for token in ("person_id", "patient_key")
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
