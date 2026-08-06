"""Windows에서도 쓸 수 있는 S3 + CloudFront 정적 프런트 배포기."""

from __future__ import annotations

import argparse
import mimetypes
from pathlib import Path
from uuid import uuid4

import boto3


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--distribution-id")
    parser.add_argument(
        "--dist", type=Path, default=Path(__file__).resolve().parents[1] / "dist"
    )
    args = parser.parse_args()

    index = args.dist / "index.html"
    if not index.is_file():
        raise FileNotFoundError("dist/index.html not found; run the frontend build first")

    s3 = boto3.client("s3", region_name="ap-northeast-2")
    uploaded: list[str] = []
    # index.html은 마지막에 올려 새 HTML이 아직 없는 해시 자산을 가리키지 않게 한다.
    files = sorted(path for path in args.dist.rglob("*") if path.is_file())
    files.sort(key=lambda path: path.name == "index.html")
    for path in files:
        key = path.relative_to(args.dist).as_posix()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        cache_control = (
            "no-cache, no-store, must-revalidate"
            if key == "index.html"
            else "public, max-age=31536000, immutable"
        )
        s3.upload_file(
            str(path),
            args.bucket,
            key,
            ExtraArgs={"ContentType": content_type, "CacheControl": cache_control},
        )
        uploaded.append(key)

    invalidation_id = None
    if args.distribution_id:
        response = boto3.client("cloudfront").create_invalidation(
            DistributionId=args.distribution_id,
            InvalidationBatch={
                "Paths": {"Quantity": 1, "Items": ["/*"]},
                "CallerReference": f"codex-{uuid4().hex}",
            },
        )
        invalidation_id = response["Invalidation"]["Id"]

    print(
        f"uploaded={len(uploaded)} bucket={args.bucket} "
        f"invalidation={invalidation_id or 'skipped'}"
    )


if __name__ == "__main__":
    main()
