"""스크리닝 오케스트레이터 API 클라이언트.

`backend/api/app/main.py` 의 엔드포인트와 1:1 로 대응한다. UI 코드가 URL 이나
HTTP 세부사항을 직접 다루지 않도록 여기서만 처리한다.
"""

from __future__ import annotations

from typing import Any

import requests

JsonDict = dict[str, Any]
JsonList = list[JsonDict]

# 백엔드 스키마의 리터럴 값. UI 위젯 선택지로도 쓴다.
REVIEW_DECISIONS: tuple[str, ...] = ("APPROVED", "REJECTED", "RERUN_REQUESTED")
TICKET_STATUSES: tuple[str, ...] = (
    "PENDING",
    "APPROVED",
    "REJECTED",
    "RERUN_REQUESTED",
)


class ApiError(RuntimeError):
    """백엔드 호출 실패.

    `unreachable` 는 서버가 떠 있지 않은 경우를 구분한다. UI 에서 설정 안내를
    따로 보여주기 위한 플래그다.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        unreachable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.unreachable = unreachable


class ScreeningApiClient:
    """FastAPI 백엔드에 붙는 얇은 HTTP 클라이언트."""

    def __init__(self, base_url: str, timeout: float = 60.0) -> None:
        self.base_url: str = base_url.rstrip("/")
        self.timeout: float = timeout
        self._session: requests.Session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    # -- 내부 -------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: JsonDict | None = None,
        json_body: JsonDict | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        clean_params = (
            {key: value for key, value in params.items() if value not in (None, "")}
            if params
            else None
        )
        try:
            response = self._session.request(
                method,
                url,
                params=clean_params,
                json=json_body,
                timeout=self.timeout,
            )
        except requests.ConnectionError as exc:
            raise ApiError(
                f"백엔드에 연결할 수 없습니다. ({url})", unreachable=True
            ) from exc
        except requests.Timeout as exc:
            raise ApiError(
                f"요청이 {self.timeout:.0f}초 안에 끝나지 않았습니다. ({path})"
            ) from exc
        except requests.RequestException as exc:
            raise ApiError(f"요청이 실패했습니다. ({path}) {exc}") from exc

        if response.status_code >= 400:
            raise ApiError(
                self._error_detail(response), status_code=response.status_code
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(f"JSON 이 아닌 응답을 받았습니다. ({path})") from exc

    @staticmethod
    def _error_detail(response: requests.Response) -> str:
        """FastAPI 에러 본문을 사람이 읽을 문장으로 바꾼다."""
        try:
            payload = response.json()
        except ValueError:
            return f"HTTP {response.status_code}: {response.text[:200]}"

        detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
        if isinstance(detail, list):
            # 422 검증 오류는 loc/msg 목록으로 온다.
            parts = [
                f"{'.'.join(str(item) for item in entry.get('loc', []))}: "
                f"{entry.get('msg', '')}"
                for entry in detail
                if isinstance(entry, dict)
            ]
            return f"HTTP {response.status_code}: " + "; ".join(parts)
        return f"HTTP {response.status_code}: {detail}"

    # -- 시스템 -----------------------------------------------------------

    def health(self) -> JsonDict:
        return self._request("GET", "/health")

    def architecture(self) -> JsonDict:
        return self._request("GET", "/api/v1/architecture")

    # -- 환자 · 시험 -------------------------------------------------------

    def list_patients(self, limit: int = 20, offset: int = 0) -> JsonDict:
        return self._request(
            "GET", "/api/v1/patients", params={"limit": limit, "offset": offset}
        )

    def get_patient(self, person_id: int) -> JsonDict:
        return self._request("GET", f"/api/v1/patients/{person_id}")

    def get_patient_timeline(self, person_id: int) -> JsonDict:
        return self._request("GET", f"/api/v1/patients/{person_id}/timeline")

    def list_trials(self) -> JsonList:
        return self._request("GET", "/api/v1/trials")

    def get_trial(self, trial_id: str) -> JsonDict:
        return self._request("GET", f"/api/v1/trials/{trial_id}")

    # -- 스크리닝 ---------------------------------------------------------

    def run_screening(
        self, person_id: int, trial_id: str, actor: str = "system"
    ) -> JsonDict:
        return self._request(
            "POST",
            "/api/v1/screening/run",
            json_body={
                "person_id": person_id,
                "trial_id": trial_id,
                "actor": actor,
            },
        )

    def get_screening(self, run_id: str) -> JsonDict:
        return self._request("GET", f"/api/v1/screening/{run_id}")

    def get_screening_evidence(self, run_id: str) -> JsonDict:
        return self._request("GET", f"/api/v1/screening/{run_id}/evidence")

    def get_screening_trace(self, run_id: str) -> JsonDict:
        return self._request("GET", f"/api/v1/screening/{run_id}/trace")

    # -- 코호트 -----------------------------------------------------------

    def run_cohort(
        self,
        trial_id: str,
        person_ids: list[int] | None = None,
        limit: int = 20,
        actor: str = "system",
    ) -> JsonDict:
        body: JsonDict = {"trial_id": trial_id, "limit": limit, "actor": actor}
        if person_ids:
            body["person_ids"] = person_ids
        return self._request("POST", "/api/v1/cohort/run", json_body=body)

    def get_cohort(self, trial_id: str) -> JsonDict:
        return self._request("GET", f"/api/v1/cohort/{trial_id}")

    # -- 참여자 확인 질문 --------------------------------------------------

    def get_patient_questions(
        self, person_id: int, trial_id: str | None = None
    ) -> JsonList:
        return self._request(
            "GET",
            f"/api/v1/patients/{person_id}/questions",
            params={"trial_id": trial_id},
        )

    def submit_answer(
        self,
        person_id: int,
        run_id: str,
        criterion_id: str,
        value: str,
        submitted_by: str = "participant",
        request_id: str | None = None,
    ) -> JsonDict:
        body: JsonDict = {
            "run_id": run_id,
            "criterion_id": criterion_id,
            "value": value,
            "submitted_by": submitted_by,
        }
        if request_id:
            body["request_id"] = request_id
        return self._request(
            "POST", f"/api/v1/patients/{person_id}/answers", json_body=body
        )

    # -- 검토 큐 -----------------------------------------------------------

    def list_review_queue(
        self, status: str | None = None, trial_id: str | None = None
    ) -> JsonList:
        return self._request(
            "GET",
            "/api/v1/review-queue",
            params={"status": status, "trial_id": trial_id},
        )

    def decide_review(
        self,
        ticket_id: str,
        decision: str,
        decided_by: str,
        note: str | None = None,
    ) -> JsonDict:
        body: JsonDict = {"decision": decision, "decided_by": decided_by}
        if note:
            body["note"] = note
        return self._request(
            "PATCH", f"/api/v1/review-queue/{ticket_id}", json_body=body
        )

    # -- 감사 로그 ---------------------------------------------------------

    def get_run_audit(self, run_id: str) -> JsonList:
        return self._request("GET", f"/api/v1/audit/{run_id}")

    def query_audit(
        self,
        person_id: int | None = None,
        trial_id: str | None = None,
        limit: int = 100,
    ) -> JsonList:
        return self._request(
            "GET",
            "/api/v1/audit",
            params={
                "person_id": person_id,
                "trial_id": trial_id,
                "limit": limit,
            },
        )
