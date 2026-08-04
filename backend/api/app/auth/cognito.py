"""Cognito 토큰 검증.

아키텍처 v2 의 `Amazon Cognito` → `Matching API` 구간이다. User Pool 은
`backend/infra/auth_stack.py` 가 만들고, 프론트엔드는 로그인 후 받은 ID 토큰을
`Authorization: Bearer <token>` 으로 보낸다. 이 모듈이 그 토큰을 검증한다.

검증 항목:
  1. 서명 — User Pool 의 JWKS 공개키(RS256)
  2. `iss` — `https://cognito-idp.{region}.amazonaws.com/{pool_id}`
  3. `exp` / `iat` — 만료 (시계 오차는 설정값만큼 허용)
  4. `token_use` — `id` 또는 `access`
  5. 대상 — ID 토큰은 `aud`, Access 토큰은 `client_id` 가 우리 앱 클라이언트인지

`aud` 를 PyJWT 에 맡기지 않고 직접 보는 이유는 Cognito 가 토큰 종류에 따라
대상을 다른 클레임에 넣기 때문이다. Access 토큰에는 `aud` 가 아예 없다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import jwt
from jwt import PyJWKClient
from jwt.exceptions import (
    InvalidTokenError,
    PyJWKClientConnectionError,
    PyJWKClientError,
)

from ..config import AuthSettings, settings as default_settings
from .principal import Principal

ALLOWED_ALGORITHMS = ("RS256",)
"""Cognito 는 RS256 만 발급한다. 화이트리스트가 `alg: none` 위조를 막는다."""

ALLOWED_TOKEN_USE = ("id", "access")


class TokenRejected(Exception):
    """토큰이 유효하지 않다. 호출자는 401 로 바꾼다."""


class AuthUnavailable(Exception):
    """검증을 수행할 수 없다. 설정 누락이나 JWKS 조회 실패. 503 으로 바꾼다."""


class SigningKeyResolver(Protocol):
    """토큰의 `kid` 에 맞는 공개키를 돌려준다. 테스트에서 교체한다."""

    def resolve(self, token: str) -> Any: ...


class JwksSigningKeyResolver:
    """User Pool JWKS 를 캐시해 사용하는 기본 구현.

    첫 요청에서만 네트워크를 탄다. 서버 기동 시점에 받아오지 않는 이유는
    Cognito 에 닿지 못하는 순간이 기동 실패로 번지지 않게 하기 위해서다.
    """

    def __init__(self, jwks_url: str, cache_seconds: int) -> None:
        self._client = PyJWKClient(
            jwks_url,
            cache_keys=True,
            cache_jwk_set=True,
            lifespan=cache_seconds,
            timeout=5,
        )

    def resolve(self, token: str) -> Any:
        return self._client.get_signing_key_from_jwt(token).key


def _parse_person_id(raw: Any) -> int | None:
    """`custom:person_id` 는 문자열이다. 숫자가 아니면 없는 것으로 본다."""
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _groups(claims: dict) -> tuple[str, ...]:
    raw = claims.get("cognito:groups")
    if not isinstance(raw, list):
        return ()
    return tuple(str(item) for item in raw)


@dataclass(frozen=True)
class CognitoTokenVerifier:
    """서명·발급자·대상·만료를 검증하고 `Principal` 로 바꾼다."""

    config: AuthSettings
    keys: SigningKeyResolver

    def verify(self, token: str) -> Principal:
        try:
            key = self.keys.resolve(token)
        except PyJWKClientConnectionError as exc:
            # 우리 쪽 문제다. 사용자에게 인증 실패로 돌려주면 원인을 오해한다.
            raise AuthUnavailable(f"JWKS 조회 실패: {exc}") from exc
        except PyJWKClientError as exc:
            raise TokenRejected(f"서명 키를 찾을 수 없습니다: {exc}") from exc

        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=list(ALLOWED_ALGORITHMS),
                issuer=self.config.issuer,
                leeway=self.config.leeway_seconds,
                options={
                    # 대상 검증은 token_use 를 본 뒤 아래에서 직접 한다.
                    "verify_aud": False,
                    "require": ["exp", "iat", "iss", "sub"],
                },
            )
        except InvalidTokenError as exc:
            raise TokenRejected(f"토큰 검증 실패: {exc}") from exc

        token_use = str(claims.get("token_use") or "")
        if token_use not in ALLOWED_TOKEN_USE:
            raise TokenRejected("token_use 클레임이 id 또는 access 가 아닙니다")

        audience = claims.get("aud") if token_use == "id" else claims.get("client_id")
        if audience not in self.config.client_ids:
            raise TokenRejected("이 앱 클라이언트로 발급된 토큰이 아닙니다")

        groups = _groups(claims)
        return Principal(
            subject=str(claims["sub"]),
            email=claims.get("email"),
            groups=groups,
            person_id=_parse_person_id(claims.get("custom:person_id")),
            token_use=token_use,
            is_admin=self.config.admin_group in groups,
            claims=claims,
        )


@dataclass(frozen=True)
class AuthGuard:
    """요청 하나의 토큰을 주체로 바꾼다. 모드는 기동 시점에 확정된다.

    | 모드 | 조건 | 동작 |
    |---|---|---|
    | `cognito` | User Pool 설정 있음 | 토큰 검증, 없거나 틀리면 거부 |
    | `open` | 설정 없음, `AUTH_REQUIRED=false` | 검증하지 않고 통과 (로컬 개발) |
    | `blocked` | 설정 없음, `AUTH_REQUIRED=true` | 전부 거부 (설정 누락 방어) |

    `open` 모드는 API 를 무인증으로 열어둔다. 로컬 개발과 기존 테스트를 위한
    경로이며, 배포 환경에서는 `AUTH_REQUIRED=true` 로 막는다.
    """

    config: AuthSettings
    verifier: CognitoTokenVerifier | None

    @property
    def mode(self) -> str:
        if self.verifier is not None:
            return "cognito"
        return "blocked" if self.config.required else "open"

    def resolve(self, token: str | None) -> Principal:
        if self.verifier is None:
            if self.config.required:
                raise AuthUnavailable(
                    "AUTH_REQUIRED=true 인데 COGNITO_USER_POOL_ID 또는 "
                    "COGNITO_CLIENT_ID 가 설정되지 않았습니다"
                )
            return Principal.development()

        if not token:
            raise TokenRejected("Authorization 헤더에 Bearer 토큰이 없습니다")
        return self.verifier.verify(token)

    def describe(self) -> dict:
        """운영 확인용 요약. 비밀값은 넣지 않는다."""
        return {
            "mode": self.mode,
            "user_pool_id": self.config.user_pool_id,
            "issuer": self.config.issuer if self.config.configured else None,
            "admin_group": self.config.admin_group,
            "client_count": len(self.config.client_ids),
        }


def build_auth_guard(
    config: AuthSettings | None = None,
    *,
    keys: SigningKeyResolver | None = None,
) -> AuthGuard:
    """설정을 보고 Guard 를 만든다. `keys` 는 테스트 주입점이다."""
    resolved = config or default_settings.auth
    if not resolved.configured:
        return AuthGuard(config=resolved, verifier=None)

    resolver = keys or JwksSigningKeyResolver(
        resolved.jwks_url, resolved.jwks_cache_seconds
    )
    return AuthGuard(
        config=resolved,
        verifier=CognitoTokenVerifier(config=resolved, keys=resolver),
    )
