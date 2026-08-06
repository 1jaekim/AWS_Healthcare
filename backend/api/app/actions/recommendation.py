"""여러 임상시험 스크리닝 결과를 결정론적으로 정렬한다.

A2A 결과는 추천 점수에만 반영한다. 규칙 엔진이 확정한 기준 상태와 종합
판정을 덮어쓰지 않으며, 확정 부적합 시험은 추천 목록에서 제외한다.
"""

from __future__ import annotations

from typing import Any, Iterable

from ..domain.states import CriterionStatus

_STATUS_SCORE = {
    CriterionStatus.EVIDENCE_FOUND: 1.0,
    CriterionStatus.CONTRADICTED: 0.0,
    CriterionStatus.UNKNOWN: 0.4,
    CriterionStatus.CONFLICTING: 0.25,
    CriterionStatus.REVIEW_REQUIRED: 0.25,
}
_A2A_SCORE = {"OK": 0.8, "NOT_OK": 0.1, "UNKNOWN": 0.4}
_DECISION_ORDER = {"OK": 0, "UNKNOWN": 1, "NOT_OK": 2}
_MIN_SCORABLE_CRITERIA = 3


class TrialRanker:
    """스크리닝 결과와 근거가 같은 입력이면 항상 같은 순위를 만든다."""

    def rank(
        self,
        outputs: Iterable[Any],
        *,
        trial_catalog: dict[str, dict[str, Any]],
        top_k: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        candidates = [
            self._candidate(output, trial_catalog[output.run.trial_id])
            for output in outputs
        ]
        candidates.sort(
            key=lambda item: (
                _DECISION_ORDER[item["recommendation_decision"]],
                -item["rank_score"],
                item["human_review_required"],
                item["trial_id"],
            )
        )

        recommendable = [
            item
            for item in candidates
            if item["recommendation_decision"] != "NOT_OK"
        ]
        recommended = recommendable[:top_k]
        for rank, item in enumerate(recommended, start=1):
            item["rank"] = rank

        excluded = [
            item
            for item in candidates
            if item["recommendation_decision"] == "NOT_OK"
        ]
        for item in excluded:
            item["rank"] = None
        return recommended, excluded

    def _candidate(
        self, output: Any, trial: dict[str, Any]
    ) -> dict[str, Any]:
        deliberation = output.run.metadata.get("deliberation", {})
        a2a_items = {
            str(item.get("criterion_id")): item
            for item in deliberation.get("items", [])
            if isinstance(item, dict) and item.get("criterion_id")
        }
        questions = {
            str(item.get("criterion_id")): item.get("question")
            for item in output.requests
            if isinstance(item, dict) and item.get("criterion_id")
        }

        criteria: list[dict[str, Any]] = []
        scores: list[float] = []
        unresolved: list[str] = []
        for result in output.run.results:
            screening_status = self._public_status(result.status)
            a2a = a2a_items.get(result.criterion_id)
            applied = self._is_grounded_consensus(a2a)
            recommendation_status = (
                str(a2a["recommendation"]) if applied else screening_status
            )
            if result.patient_reported and result.rule_satisfied is not None:
                # 자기보고 답변은 확정 판정으로 승격하지 않지만, 공고 조건과 실제로
                # 비교한 잠정 점수에는 방향성을 반영한다.
                score = 0.7 if result.rule_satisfied else 0.0
            else:
                score = (
                    _A2A_SCORE[recommendation_status]
                    if applied
                    else _STATUS_SCORE[result.status]
                )
            scores.append(score)
            if recommendation_status == "UNKNOWN":
                unresolved.append(result.criterion_id)

            criteria.append(
                {
                    "criterion_id": result.criterion_id,
                    "criterion_type": result.criterion_type,
                    "label": result.label,
                    "screening_status": screening_status,
                    "recommendation_status": recommendation_status,
                    "reason": result.explanation,
                    "evidence_ids": list(result.source_ids),
                    "a2a_applied": applied,
                    "next_question": questions.get(result.criterion_id),
                }
            )

        screening_decision = output.outcome.screening_decision
        recommendation_statuses = [
            item["recommendation_status"] for item in criteria
        ]
        if "NOT_OK" in recommendation_statuses:
            recommendation_decision = "NOT_OK"
        elif recommendation_statuses and all(
            status == "OK" for status in recommendation_statuses
        ):
            recommendation_decision = "OK"
        else:
            recommendation_decision = "UNKNOWN"
        criteria_sufficient = len(criteria) >= _MIN_SCORABLE_CRITERIA
        if not criteria_sufficient and recommendation_decision != "NOT_OK":
            recommendation_decision = "UNKNOWN"
        overall_status = {
            "OK": "MATCHED",
            "NOT_OK": "EXCLUDED",
            "UNKNOWN": "NEEDS_MORE_INFO",
        }[recommendation_decision]
        # 사람 확인 필요 신호는 오케스트레이터가 판정에 담아준다. 예전에는 검토 큐
        # 티켓 존재 여부로 판단했는데, 그 큐는 휘발성이라 신호가 사라졌다.
        human_review_required = bool(output.human_review_criteria)

        return {
            "rank": None,
            "run_id": output.run.run_id,
            "trial_id": output.run.trial_id,
            "title": trial["trial_name"],
            "description": trial.get("description", ""),
            "rank_score": (
                round(sum(scores) / len(scores), 3)
                if scores and criteria_sufficient
                else 0.0
            ),
            "overall_status": overall_status,
            "screening_decision": screening_decision,
            "recommendation_decision": recommendation_decision,
            "criteria_met": output.outcome.criteria_met,
            "criteria_total": output.outcome.criteria_total,
            "unresolved_criteria": unresolved,
            "human_review_required": human_review_required or not criteria_sufficient,
            # 검토 큐 제거로 `review_ticket_id` 가 없어졌다. 사람 확인이 필요한
            # 기준 목록이 그 자리를 대신한다.
            "human_review_criteria": list(output.human_review_criteria),
            "selection_reason": (
                "승인된 기준이 3개 미만이라 적합도를 계산하지 않았습니다."
                if not criteria_sufficient
                else self._selection_reason(
                    screening_decision,
                    recommendation_decision,
                )
            ),
            "a2a": deliberation,
            "criteria": criteria,
        }

    @staticmethod
    def _is_grounded_consensus(item: dict[str, Any] | None) -> bool:
        if not item:
            return False
        return bool(
            item.get("agreement")
            and item.get("grounded")
            and item.get("recommendation") in {"OK", "NOT_OK"}
        )

    @staticmethod
    def _public_status(status: CriterionStatus) -> str:
        if status is CriterionStatus.EVIDENCE_FOUND:
            return "OK"
        if status is CriterionStatus.CONTRADICTED:
            return "NOT_OK"
        return "UNKNOWN"

    @staticmethod
    def _selection_reason(
        screening_decision: str,
        recommendation_decision: str,
    ) -> str:
        if recommendation_decision == "OK" and screening_decision == "UNKNOWN":
            return "A2A 근거 합의가 추천 기준을 충족했지만 규칙 판정은 보존됩니다."
        if recommendation_decision == "OK":
            return "모든 기준이 확인된 적합 후보입니다."
        if recommendation_decision == "NOT_OK" and screening_decision == "UNKNOWN":
            return "A2A 근거 합의가 NOT_OK여서 추천에서 제외했지만 규칙 판정은 보존됩니다."
        if recommendation_decision == "NOT_OK":
            return "확정된 미충족 기준이 있어 추천에서 제외했습니다."
        return "확정 부적합 근거는 없지만 추가 확인이 필요한 후보입니다."


__all__ = ["TrialRanker"]
