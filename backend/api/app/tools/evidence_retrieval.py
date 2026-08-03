"""Evidence Retrieval Tool: 자유서술 EMR 에서 근거 문장을 찾는다.

Bedrock Knowledge Bases + OpenSearch 어댑터로 교체할 지점.
지금은 동일한 반환 계약(문장 + 출처 ID + 점수)을 로컬 키워드 검색으로 구현한다.
"""

from __future__ import annotations

import re
from typing import Any

from ..domain.models import NarrativeSnippet
from ..repository import DatasetRepository
from .base import Action, BaseTool, DataStore, Permission, ToolContext

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_NEGATION_HINTS = ("없", "부인", "아니", "미확인", "해당하지")


def _split_sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT.split(text) if part.strip()]


class EvidenceRetrievalTool(BaseTool):
    """정제된 EMR 본문에서 조건 관련 문장을 검색한다."""

    name = "evidence_retrieval_tool"
    permissions = (Permission(DataStore.KNOWLEDGE_BASE, Action.READ),)

    def __init__(self, repository: DatasetRepository) -> None:
        self._repository = repository

    def invoke(
        self, context: ToolContext, /, **kwargs: Any
    ) -> list[NarrativeSnippet]:
        """질의어가 포함된 문장을 점수 순으로 반환한다."""
        self.assert_allowed(Permission(DataStore.KNOWLEDGE_BASE, Action.READ))

        terms: tuple[str, ...] = tuple(kwargs.get("terms", ()))
        top_k: int = int(kwargs.get("top_k", 3))
        if not terms:
            return []

        candidates: list[NarrativeSnippet] = []
        for row in self._repository.timelines.get(context.person_id, []):
            encounter_id = row["encounter_id"]
            note = self._repository.notes.get(encounter_id)
            if not note:
                continue
            candidates.extend(
                self._match_sentences(
                    note=note,
                    encounter_id=encounter_id,
                    note_date=row["encounter_date"],
                    terms=terms,
                )
            )

        candidates.sort(key=lambda item: (-item.score, item.note_date))
        return candidates[:top_k]

    def _match_sentences(
        self,
        *,
        note: str,
        encounter_id: str,
        note_date: str,
        terms: tuple[str, ...],
    ) -> list[NarrativeSnippet]:
        """문장 단위로 질의어 일치를 계산한다."""
        found: list[NarrativeSnippet] = []
        for sentence in _split_sentences(note):
            matched = tuple(term for term in terms if term in sentence)
            if not matched:
                continue
            score = len(matched) / len(terms)
            if any(hint in sentence for hint in _NEGATION_HINTS):
                score *= 0.6
            found.append(
                NarrativeSnippet(
                    note_id=f"NOTE-{encounter_id}",
                    encounter_id=encounter_id,
                    note_date=note_date,
                    snippet=sentence[:400],
                    matched_terms=matched,
                    score=round(score, 3),
                )
            )
        return found

    @staticmethod
    def implies_negative(snippets: list[NarrativeSnippet]) -> bool | None:
        """검색된 문장이 부정 진술인지 추정한다.

        임신 여부처럼 자유서술이 구조화 값과 충돌하는지 볼 때 사용한다.
        판단 근거가 부족하면 None 을 반환한다.
        """
        if not snippets:
            return None
        negative = sum(
            1
            for item in snippets
            if any(hint in item.snippet for hint in _NEGATION_HINTS)
        )
        if negative == len(snippets):
            return True
        if negative == 0:
            return False
        return None
