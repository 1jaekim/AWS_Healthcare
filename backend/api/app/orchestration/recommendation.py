"""환자 한 명을 여러 임상시험과 비교하는 추천 오케스트레이터."""

from __future__ import annotations

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
    ) -> None:
        self._screening = screening
        self._repository = repository
        self._trial_catalog = trial_catalog
        self._runs = run_store
        self._audit = audit
        self._ranker = ranker or TrialRanker()
        self._a2a_max_criteria = min(5, max(1, a2a_max_criteria))

    def run(
        self,
        *,
        person_id: int,
        trial_ids: Sequence[str],
        top_k: int,
        actor: str,
    ) -> dict[str, Any]:
        recommendation_id = self._runs.new_recommendation_id()
        outputs = [
            self._screening.run(
                person_id=person_id,
                trial_id=trial_id,
                actor=actor,
            )
            for trial_id in trial_ids
        ]
        recommended, excluded = self._ranker.rank(
            outputs,
            trial_catalog=(
                self._trial_catalog.trial_catalog(list(trial_ids))
                if self._trial_catalog is not None
                else self._repository.trials
            ),
            top_k=top_k,
        )
        payload = {
            "recommendation_id": recommendation_id,
            "person_id": person_id,
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
            },
        }
        self._runs.save_recommendation(recommendation_id, payload)
        self._audit.record(
            "RECOMMENDATION_COMPLETED",
            actor=actor,
            run_id=recommendation_id,
            person_id=person_id,
            evaluated_trials=len(outputs),
            recommended_trial_ids=[item["trial_id"] for item in recommended],
            excluded_trial_ids=[item["trial_id"] for item in excluded],
        )
        return payload


__all__ = ["RecommendationOrchestrator"]
