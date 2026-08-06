"""식약처 DUR 병용금기 10건을 GraphRAG 표준문서로 적재한다.

인증키는 명령행 인자로 받지 않는다. ``MFDS_DUR_SERVICE_KEY`` 환경변수로만
읽으며 파일·로그·S3 객체 어디에도 저장하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.lambdas.protocol_parser.drug_reference_documents import (  # noqa: E402
    build_dur_coadministration_document,
    is_diabetes_dur_row,
)


API_URL = (
    "https://apis.data.go.kr/1471000/DURIrdntInfoService03/"
    "getUsjntTabooInfoList02"
)


def _unwrap_rows(items: Any, *, limit: int) -> list[dict[str, Any]]:
    if isinstance(items, dict):
        items = items.get("item") or []
    if isinstance(items, dict):
        items = [items]
    rows: list[dict[str, Any]] = []
    for candidate in items:
        if not isinstance(candidate, dict):
            continue
        # 이 API는 JSON 응답에서 각 행을 {"item": {...}}로 한 번 더 감싼다.
        # 다른 공공데이터 API처럼 평평한 행이 오는 경우도 함께 허용한다.
        inner = candidate.get("item")
        rows.append(inner if isinstance(inner, dict) else candidate)
        if len(rows) == limit:
            break
    return rows


def fetch_page(
    service_key: str, *, page_no: int = 1, num_rows: int = 100
) -> tuple[list[dict[str, Any]], int]:
    query = urlencode(
        {
            "serviceKey": service_key,
            "pageNo": page_no,
            "numOfRows": num_rows,
            "type": "json",
        }
    )
    request = Request(
        f"{API_URL}?{query}",
        headers={"Accept": "application/json", "User-Agent": "clinical-trial-rag/1.0"},
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 - 고정 공식 URL
        payload = json.loads(response.read().decode("utf-8"))

    header = payload.get("header") or payload.get("response", {}).get("header") or {}
    result_code = str(header.get("resultCode") or "00")
    if result_code not in {"00", "0"}:
        message = header.get("resultMsg") or "DUR API request failed"
        raise RuntimeError(f"DUR API 오류 {result_code}: {message}")

    body = payload.get("body") or payload.get("response", {}).get("body") or {}
    rows = _unwrap_rows(body.get("items") or [], limit=num_rows)
    try:
        total_count = int(body.get("totalCount") or len(rows))
    except (TypeError, ValueError):
        total_count = len(rows)
    return rows, total_count


def fetch_rows(service_key: str, *, limit: int = 10) -> list[dict[str, Any]]:
    rows, _ = fetch_page(service_key, page_no=1, num_rows=limit)
    if len(rows) != limit:
        raise RuntimeError(f"DUR API가 요청한 {limit}건 대신 {len(rows)}건을 반환했습니다")
    return rows


def fetch_diabetes_rows(
    service_key: str, *, limit: int = 10, page_size: int = 100
) -> tuple[list[dict[str, Any]], int]:
    """당뇨병용제 관계만 모으고 limit에 도달하면 즉시 조회를 멈춘다."""
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    page_no = 1
    scanned = 0
    total_count: int | None = None
    while total_count is None or scanned < total_count:
        rows, total_count = fetch_page(
            service_key, page_no=page_no, num_rows=page_size
        )
        if not rows:
            break
        scanned += len(rows)
        for row in rows:
            if not is_diabetes_dur_row(row):
                continue
            pair = (
                str(row.get("INGR_CODE") or ""),
                str(row.get("MIXTURE_INGR_CODE") or ""),
            )
            if pair in seen:
                continue
            seen.add(pair)
            selected.append(row)
            if len(selected) == limit:
                return selected, scanned
        page_no += 1
    raise RuntimeError(
        f"DUR 전체 {scanned}건에서 당뇨병용제 관계를 {len(selected)}건만 찾았습니다"
    )


def write_document(
    rows: list[dict[str, Any]], output_root: Path, *, scope: str = "pilot"
) -> tuple[Path, Path]:
    document = build_dur_coadministration_document(rows, scope=scope)
    body_path = output_root / document.key
    metadata_path = output_root / f"{document.key}.metadata.json"
    body_path.parent.mkdir(parents=True, exist_ok=True)
    body_path.write_text(document.body, encoding="utf-8")
    metadata_path.write_text(
        json.dumps(document.metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return body_path, metadata_path


def upload_document(
    rows: list[dict[str, Any]], *, bucket: str, region: str, scope: str = "pilot"
) -> tuple[str, str]:
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - 실행 환경 안내
        raise RuntimeError("S3 업로드에는 boto3가 필요합니다") from exc

    document = build_dur_coadministration_document(rows, scope=scope)
    metadata_key = f"{document.key}.metadata.json"
    client = boto3.client("s3", region_name=region)
    client.put_object(
        Bucket=bucket,
        Key=document.key,
        Body=document.body.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
        ServerSideEncryption="aws:kms",
    )
    client.put_object(
        Bucket=bucket,
        Key=metadata_key,
        Body=json.dumps(document.metadata, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
        ServerSideEncryption="aws:kms",
    )
    return document.key, metadata_key


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--s3-bucket")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--scope", choices=("pilot", "diabetes"), default="pilot")
    args = parser.parse_args()

    service_key = os.getenv("MFDS_DUR_SERVICE_KEY", "").strip()
    if not service_key:
        raise SystemExit(
            "MFDS_DUR_SERVICE_KEY가 없습니다. 공공데이터포털 인증키를 환경변수로 "
            "설정한 뒤 다시 실행하세요."
        )

    scanned = 10
    if args.scope == "diabetes":
        rows, scanned = fetch_diabetes_rows(service_key, limit=10)
    else:
        rows = fetch_rows(service_key, limit=10)
    body_path, metadata_path = write_document(
        rows, args.output_root.resolve(), scope=args.scope
    )
    result: dict[str, Any] = {
        "row_count": len(rows),
        "rows_scanned": scanned,
        "scope": args.scope,
        "document": str(body_path),
        "metadata": str(metadata_path),
        "source": API_URL,
    }
    if args.s3_bucket:
        document_key, metadata_key = upload_document(
            rows, bucket=args.s3_bucket, region=args.region, scope=args.scope
        )
        result["s3"] = {
            "bucket": args.s3_bucket,
            "region": args.region,
            "document_key": document_key,
            "metadata_key": metadata_key,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
