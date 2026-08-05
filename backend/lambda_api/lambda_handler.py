"""FastAPI 를 Lambda 에서 띄우는 진입점.

`backend/api/app/main.py` 의 ASGI 앱을 Mangum 으로 감싼다. 앱 코드는 Lambda 에
대해 아무것도 몰라야 하므로(로컬 uvicorn 과 같은 앱이어야 한다) 어댑터를 여기
한 곳에만 둔다.

패키지 배치는 `backend/scripts/build_api_asset.sh` 가 만든다.

    /var/task/
      lambda_handler.py     ← 이 파일
      app/                  ← backend/api/app
      agent/                ← 저장소 루트의 agent
      dataset/              ← 시드 데이터셋 (EMR_DATA_DIR)
      fastapi/ mangum/ ...  ← linux 휠로 받은 의존성

`app` 과 `agent` 가 같은 층에 놓이는 것은 로컬 실행과 같은 구조다. 로컬에서는
`--app-dir backend/api` 가 `app` 을, 저장소 루트가 `agent` 를 import 경로에
넣는다. Lambda 에서는 `/var/task` 하나가 두 역할을 겸한다.
"""

from __future__ import annotations

import logging
import os

# Lambda 는 콜드 스타트마다 이 모듈을 다시 읽는다. 데이터셋 경로가 비어 있으면
# `DatasetRepository` 가 FileNotFoundError 로 죽고 원인이 로그에 안 남으므로,
# 기본값을 패키지 안쪽으로 못박아 둔다.
os.environ.setdefault("EMR_DATA_DIR", os.path.join(os.path.dirname(__file__), "dataset"))

logging.getLogger().setLevel(os.getenv("LOG_LEVEL", "INFO"))

from mangum import Mangum  # noqa: E402  (환경 변수 설정 뒤에 import 해야 한다)

from app.main import app  # noqa: E402


# lifespan="on" 이어야 `app.state.container` 가 채워진다. Mangum 기본값은
# "auto" 인데, 그러면 컨테이너 조립이 첫 요청 전에 끝났는지 보장되지 않는다.
handler = Mangum(app, lifespan="on")
