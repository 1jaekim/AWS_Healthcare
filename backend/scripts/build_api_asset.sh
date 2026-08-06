#!/usr/bin/env bash
# API Lambda 배포 패키지를 만든다.
#
#   backend/scripts/build_api_asset.sh
#   → backend/build/api_lambda/
#
# Docker 를 쓰지 않는다. CDK 의 파이썬 번들링과 Lambda 컨테이너 이미지는 둘 다
# 로컬 Docker 데몬을 요구하는데, 그것 하나 때문에 배포가 막히는 상황을 만들지
# 않으려는 것이다. 대신 pip 에게 linux 휠을 직접 지정해서 받는다. mac 에서
# 빌드해도 Lambda(리눅스 x86_64)에서 도는 바이너리가 들어간다.
#
# `--only-binary=:all:` 이 핵심이다. 이게 없으면 pip 이 소스 배포판을 받아
# 로컬(mac arm64)에서 컴파일해버리고, 그 결과물은 Lambda 에서 import 되지 않는다.
# cryptography(PyJWT 의 RSA 서명 검증)가 특히 그렇다.

set -euo pipefail

BACKEND="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$BACKEND/.." && pwd)"
BUILD_DIR="$BACKEND/build/api_lambda"

PYTHON_VERSION="3.12"
PLATFORM="manylinux2014_x86_64"
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "==> 이전 산출물 정리"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

echo "==> 의존성 설치 (${PLATFORM}, py${PYTHON_VERSION})"
"$PYTHON_BIN" -m pip install \
  --quiet \
  --requirement "$BACKEND/lambda_api/requirements.txt" \
  --target "$BUILD_DIR" \
  --platform "$PLATFORM" \
  --python-version "$PYTHON_VERSION" \
  --implementation cp \
  --only-binary=:all: \
  --upgrade

echo "==> 애플리케이션 코드 복사"
# app 과 agent 를 같은 층에 둔다. 로컬에서 `--app-dir backend/api` 와 저장소
# 루트가 각각 담당하던 import 경로를 Lambda 에서는 /var/task 하나가 겸한다.
cp -R "$BACKEND/api/app" "$BUILD_DIR/app"
cp -R "$REPO_ROOT/agent" "$BUILD_DIR/agent"
cp -R "$BACKEND/a2a_agents" "$BUILD_DIR/a2a_agents"
cp "$BACKEND/lambda_api/lambda_handler.py" "$BUILD_DIR/lambda_handler.py"

echo "==> 시드 데이터셋 생성"
# DatasetRepository 는 기동 시점에 CSV 7종을 읽는다. 없으면 FileNotFoundError 로
# 콜드 스타트가 통째로 죽는다. 실제 데이터셋은 레포 밖에서 공유되므로 배포
# 패키지에는 스키마만 같은 시드를 넣는다.
"$PYTHON_BIN" "$BACKEND/scripts/generate_seed_dataset.py" "$BUILD_DIR/dataset"

echo "==> 불필요한 파일 제거"
# 캐시와 테스트만 지운다. 패키지 크기는 콜드 스타트 시간에 반영되지만,
# *.dist-info 는 남긴다 — importlib.metadata.version() 을 부르는 라이브러리가
# 있고, 지우면 import 시점에 PackageNotFoundError 로 죽는다. 몇 MB 아끼려다
# 기동이 안 되는 쪽이 훨씬 비싸다.
find "$BUILD_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR" -type d -name "tests" -prune -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR" -type f -name "*.pyc" -delete 2>/dev/null || true

SIZE="$(du -sh "$BUILD_DIR" | cut -f1)"
echo "==> 완료: $BUILD_DIR ($SIZE)"
