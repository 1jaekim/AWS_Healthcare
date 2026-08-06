"""API 진입 계층의 인증.

Cognito 가 발급한 토큰을 검증해 `Principal` 로 바꾸고, 그 주체로 접근을
통제한다. 계정 저장소는 Cognito User Pool 이며 이 패키지는 상태를 갖지 않는다.
"""

from .cognito import (
    AuthGuard,
    AuthUnavailable,
    CognitoTokenVerifier,
    SigningKeyResolver,
    TokenRejected,
    build_auth_guard,
)
from .dependencies import (
    AdminUser,
    CurrentUser,
    PUBLIC_PATHS,
    current_principal,
    enforce_auth,
    ensure_person_access,
    require_admin,
)
from .principal import Principal
from .profile import account_profile_values, interest_areas, korean_age

__all__ = [
    "AdminUser",
    "AuthGuard",
    "AuthUnavailable",
    "CognitoTokenVerifier",
    "CurrentUser",
    "PUBLIC_PATHS",
    "Principal",
    "SigningKeyResolver",
    "TokenRejected",
    "account_profile_values",
    "build_auth_guard",
    "current_principal",
    "enforce_auth",
    "ensure_person_access",
    "interest_areas",
    "korean_age",
    "require_admin",
]
