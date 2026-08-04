"""요청 주체.

토큰에서 뽑아낸 값만 담는다. 여기에 담기는 것은 계정 식별자와 권한 판단에
필요한 최소한이며, 임상 정보는 담지 않는다.

`person_id` 는 Cognito 커스텀 속성 `custom:person_id` 다. 백엔드 API 가 전부
합성 EMR 의 `person_id` 로 말하기 때문에 계정과 환자를 잇는 유일한 값이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Principal:
    """검증된 토큰 한 건에서 파생된 요청 주체."""

    subject: str
    """Cognito `sub`. 계정의 불변 식별자."""

    email: str | None = None
    groups: tuple[str, ...] = ()
    person_id: int | None = None
    token_use: str = "id"
    is_admin: bool = False
    anonymous: bool = False
    """개방 모드(Cognito 미설정)에서 만들어진 주체인지."""

    claims: dict = field(default_factory=dict, repr=False, compare=False)
    """검증을 통과한 원본 클레임. 로그에 그대로 쓰지 않는다."""

    @property
    def actor(self) -> str:
        """감사 로그에 남길 행위자 문자열.

        이메일은 직접 식별자다. 감사 로그는 `sub` 로 남기고, 사람이 볼 화면에서만
        이메일을 붙인다.
        """
        if self.anonymous:
            return "anonymous"
        return f"cognito:{self.subject}"

    def can_access_person(self, person_id: int) -> bool:
        """이 주체가 특정 환자 데이터를 볼 수 있는지.

        관리자는 전부 볼 수 있다. 일반 사용자는 자기 계정에 바인딩된 환자만
        볼 수 있고, 바인딩이 없으면 아무것도 볼 수 없다. 임상 정보는 계정 하나가
        잘못 열리면 되돌릴 수 없으므로 모르는 경우는 막는 쪽으로 둔다.
        """
        if self.is_admin:
            return True
        if self.person_id is None:
            return False
        return self.person_id == person_id

    @classmethod
    def development(cls) -> Principal:
        """Cognito 미설정 환경의 주체.

        로컬 개발과 기존 테스트를 위한 것이다. 권한 검사를 통과시키므로
        배포 환경에서는 `AUTH_REQUIRED=true` 로 이 경로를 막는다.
        """
        return cls(
            subject="local-dev",
            groups=("admin",),
            token_use="none",
            is_admin=True,
            anonymous=True,
        )

    def to_dict(self) -> dict:
        return {
            "subject": self.subject,
            "email": self.email,
            "groups": list(self.groups),
            "person_id": self.person_id,
            "token_use": self.token_use,
            "is_admin": self.is_admin,
            "anonymous": self.anonymous,
        }
