"""환자 한 명을 여러 임상시험과 비교하는 추천 오케스트레이터."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Sequence

from ..actions.recommendation import TrialRanker
from ..persistence.audit import AuditTrail
from ..persistence.run_store import RunStore
from ..repository import DatasetRepository
from .runtime import ScreeningOrchestrator


class RecommendationOrchestrator:
    """후보별 스크리닝을 실행한 뒤 확정 판정과 A2A 결과로 순위를 만든다."""

    def __init__(
        self,
        *,
        screening: ScreeningOrchestrator,
        repository: DatasetRepository,
        trial_catalog: Any | None = None,
        run_store: RunStore,
        audit: AuditTrail,
        ranker: TrialRanker | None = None,
        a2a_max_criteria: int = 5,
        max_workers: int = 2,
    ) -> None:
        self._screening = screening
        self._repository = repository
        self._trial_catalog = trial_catalog
        self._runs = run_store
        self._audit = audit
        self._ranker = ranker or TrialRanker()
        self._a2a_max_criteria = min(5, max(1, a2a_max_criteria))
        self._max_workers = min(5, max(1, max_workers))

    def run(
        self,
        *,
        person_id: int | None,
        trial_ids: Sequence[str],
        top_k: int,
        actor: str,
        supplements_by_trial: dict[str, dict[str, Any]] | None = None,
        application_id: str | None = None,
        owner_sub: str | None = None,
        interest_areas: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        recommendation_id = self._runs.new_recommendation_id()
        def screen(trial_id: str) -> Any:
            kwargs: dict[str, Any] = {}
            supplement = (supplements_by_trial or {}).get(trial_id)
            if supplement is not None:
                kwargs["supplements"] = supplement
            if application_id:
                kwargs["application_id"] = application_id
                kwargs["owner_sub"] = owner_sub
            return self._screening.run(
                person_id=person_id,
                trial_id=trial_id,
                actor=actor,
                **kwargs,
            )

        # executor.map은 입력 순서대로 결과를 돌려준다. 따라서 병렬 실행으로
        # 지연시간을 줄이면서 기존의 결정론적 정렬·감사 계약은 유지한다.
        worker_count = min(self._max_workers, len(trial_ids))
        if worker_count <= 1:
            outputs = [screen(trial_id) for trial_id in trial_ids]
        else:
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="trial-screening",
            ) as executor:
                outputs = list(executor.map(screen, trial_ids))
        recommended, excluded = self._ranker.rank(
            outputs,
            trial_catalog=(
                self._trial_catalog.trial_catalog(list(trial_ids))
                if self._trial_catalog is not None
                else self._repository.trials
            ),
            top_k=top_k,
            interest_areas=interest_areas,
        )
        payload = {
            "recommendation_id": recommendation_id,
            "person_id": person_id,
            "application_id": application_id,
            # API 응답 모델에는 노출하지 않고 저장소 소유권 검사에만 쓴다.
            "owner_sub": owner_sub,
            "evaluated_trials": len(outputs),
            "recommended_trials": recommended,
            "excluded_trials": excluded,
            "remaining_candidate_count": max(
                0,
                len(outputs) - len(excluded) - len(recommended),
            ),
            "limits": {
                "top_k": top_k,
                "a2a_max_rounds": 2,
                "a2a_max_criteria_per_trial": self._a2a_max_criteria,
                "recommendation_max_workers": worker_count,
            },
        }
        self._runs.save_recommendation(recommendation_id, payload)
        self._audit.record(
            "RECOMMENDATION_COMPLETED",
            actor=actor,
            run_id=recommendation_id,
            person_id=person_id,
            application_id=application_id,
            evaluated_trials=len(outputs),
            recommended_trial_ids=[item["trial_id"] for item in recommended],
            excluded_trial_ids=[item["trial_id"] for item in excluded],
        )
        return payload


__all__ = ["RecommendationOrchestrator"]
