"""FastAPI 인증 의존성.

`app/main.py` 는 앱 전역 의존성으로 `enforce_auth` 를 걸고, 개별 엔드포인트는
필요한 것만 추가로 선언한다.

    CurrentUser  로그인한 사용자 (개방 모드에서는 개발 주체)
    AdminUser    관리자 그룹에 속한 사용자
    ensure_person_access(principal, person_id)  본인 환자 데이터인지 확인

전역 의존성을 쓰는 이유는 라우트가 30개 넘게 한 모듈에 있어서, 새 엔드포인트가
추가될 때 인증을 빼먹는 쪽이 기본값이 되면 안 되기 때문이다. 공개로 둘 경로만
아래 `PUBLIC_PATHS` 에 적는다.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .cognito import AuthGuard, AuthUnavailable, TokenRejected
from .principal import Principal

PUBLIC_PATHS = frozenset(
    {
        "/",
        "/health",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/openapi.json",
    }
)
"""토큰 없이 열어두는 경로. 임상·환자 데이터를 반환하지 않는 것만 둔다."""

_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="CognitoIdToken",
    description="Cognito 로그인 후 받은 ID 토큰",
)

BearerToken = Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]


def get_guard(request: Request) -> AuthGuard:
    return request.app.state.auth


def _resolve(request: Request, credentials: HTTPAuthorizationCredentials | None) -> Principal:
    """요청당 한 번만 검증한다. 결과는 `request.state.principal` 에 둔다."""
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached

    try:
        principal = get_guard(request).resolve(
            credentials.credentials if credentials else None
        )
    except TokenRejected as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except AuthUnavailable as exc:
        # 설정 누락이나 JWKS 조회 실패다. 자격 문제로 오해하지 않도록 구분한다.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    request.state.principal = principal
    return principal


def enforce_auth(request: Request, credentials: BearerToken) -> None:
    """앱 전역 의존성. 공개 경로를 뺀 모든 요청에 토큰을 요구한다."""
    if request.url.path in PUBLIC_PATHS:
        return
    _resolve(request, credentials)


def current_principal(request: Request, credentials: BearerToken) -> Principal:
    return _resolve(request, credentials)


CurrentUser = Annotated[Principal, Depends(current_principal)]


def require_admin(principal: CurrentUser) -> Principal:
    if not principal.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="관리자 권한이 필요합니다",
        )
    return principal


AdminUser = Annotated[Principal, Depends(require_admin)]


def ensure_person_access(principal: Principal, person_id: int) -> None:
    """본인(또는 관리자)만 환자 데이터에 닿게 한다.

    계정에 `custom:person_id` 바인딩이 없으면 통과시키지 않는다. 임상 정보는
    한 번 잘못 열리면 되돌릴 수 없어서, 모르는 경우는 막는 쪽으로 둔다.
    """
    if principal.can_access_person(person_id):
        return
    if principal.person_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "계정에 환자 번호(custom:person_id)가 연결되지 않아 "
                "환자 데이터에 접근할 수 없습니다"
            ),
        )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="다른 사용자의 환자 데이터에 접근할 수 없습니다",
    )
