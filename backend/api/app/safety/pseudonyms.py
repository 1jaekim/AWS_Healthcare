"""환자 식별자를 GraphRAG 필터용 가명 키로 변환한다."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from ..config import resolve_region


def pseudonymize_patient_id(person_id: int | str, secret: str) -> str:
    """Sanitizer와 동일한 HMAC-SHA256 계약으로 patient_key를 만든다."""
    identifier = str(person_id).strip()
    if not identifier:
        raise ValueError("person_id must not be empty")
    if not secret:
        raise ValueError("pseudonymization secret must not be empty")
    digest = hmac.new(
        secret.encode("utf-8"), identifier.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"pt_{digest[:24]}"


class PatientKeyResolver:
    """직접 설정 또는 Secrets Manager에서 HMAC 키를 지연 로딩한다."""

    def __init__(
        self,
        *,
        secret: str | None = None,
        secret_arn: str | None = None,
        region: str | None = None,
        secrets_client: Any | None = None,
    ) -> None:
        if not secret and not secret_arn:
            raise ValueError(
                "PATIENT_PSEUDONYM_SECRET or PATIENT_PSEUDONYM_SECRET_ARN is required"
            )
        self._secret = secret
        self._secret_arn = secret_arn
        # 리전 기본값은 config 한 곳에서 정한다. 여기에 직접 적으면 배포 리전을
        # 바꿀 때 이 줄이 남아 Secrets Manager 를 다른 리전에서 찾는다.
        self._region = region or resolve_region()
        self._secrets_client = secrets_client

    def __call__(self, person_id: int) -> str:
        return pseudonymize_patient_id(person_id, self._load_secret())

    def _load_secret(self) -> str:
        if self._secret:
            return self._secret
        if self._secrets_client is None:
            import boto3

            self._secrets_client = boto3.client(
                "secretsmanager", region_name=self._region
            )
        response = self._secrets_client.get_secret_value(SecretId=self._secret_arn)
        secret = str(response.get("SecretString") or "")
        if not secret:
            raise RuntimeError("pseudonymization secret is empty")
        self._secret = secret
        return secret


__all__ = ["PatientKeyResolver", "pseudonymize_patient_id"]
