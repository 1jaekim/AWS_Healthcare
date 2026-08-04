#!/usr/bin/env bash
# S3 + CloudFront 정적 배포.
#
# Amplify Hosting 대신 S3 로 직접 올릴 때 쓴다. 둘 중 하나만 쓰면 된다.
#
#   S3_BUCKET=my-bucket CLOUDFRONT_DISTRIBUTION_ID=E123 ./scripts/deploy-s3.sh
#
# 사전 조건 — 버킷은 퍼블릭이 아니라 CloudFront OAC 로만 읽게 두고,
# CloudFront 배포에 아래 오류 응답 두 개를 등록해야 SPA 딥링크가 산다.
#
#   403 -> /index.html (200)
#   404 -> /index.html (200)
#
# 이게 없으면 /results/medi25-10842 같은 주소로 새로고침할 때 S3 가
# NoSuchKey 를 돌려주고 화면이 깨진다.

set -euo pipefail

: "${S3_BUCKET:?S3_BUCKET 을 지정하세요}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> 빌드"
npm run build

echo "==> 해시 에셋 업로드 (장기 캐시)"
aws s3 sync dist/ "s3://${S3_BUCKET}/" \
  --delete \
  --exclude "index.html" \
  --cache-control "public, max-age=31536000, immutable"

# index.html 은 마지막에, 캐시 없이 올린다. 순서를 바꾸면 새 HTML 이
# 아직 올라가지 않은 에셋을 가리키는 순간이 생긴다.
echo "==> index.html 업로드 (캐시 없음)"
aws s3 cp dist/index.html "s3://${S3_BUCKET}/index.html" \
  --cache-control "no-cache, no-store, must-revalidate" \
  --content-type "text/html; charset=utf-8"

if [[ -n "${CLOUDFRONT_DISTRIBUTION_ID:-}" ]]; then
  echo "==> CloudFront 무효화"
  aws cloudfront create-invalidation \
    --distribution-id "$CLOUDFRONT_DISTRIBUTION_ID" \
    --paths "/*" >/dev/null
fi

echo "==> 완료: s3://${S3_BUCKET}"
