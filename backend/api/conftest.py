"""테스트 실행 시 import 경로 설정.

`app` 패키지는 `backend/api` 를, `agent` 패키지는 저장소 루트를 기준으로 찾는다.
두 경로를 여기서 한 번에 넣어두면 어느 디렉터리에서 pytest 를 띄워도 동작한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = API_ROOT.parent
REPO_ROOT = API_ROOT.parents[1]

for path in (str(REPO_ROOT), str(BACKEND_ROOT), str(API_ROOT)):
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)
