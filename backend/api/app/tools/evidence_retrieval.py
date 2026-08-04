"""Evidence Retrieval Tool 어댑터.

로컬 개발에서는 키워드 검색을 사용하고, Knowledge Base ID가 설정된 환경에서는
Bedrock Retrieve를 호출한다. 두 구현은 동일한 NarrativeSnippet 계약을 반환한다.
"""

from __future__ import annotations

import re
from typing import Any

from ..domain.models import NarrativeSnippet
from ..repository import DatasetRepository
from .base import Action, BaseTool, DataStore, Permission, ToolContext

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_NEGATION_HINTS = ("없", "부인", "아니", "미확인", "해당하지")
_EVENT_REFERENCE = re.compile(r"이벤트\s*참조\s*:\s*(evt_[a-f0-9]+)", re.IGNORECASE)
_MAX_TOP_K = 5


def _split_sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT.split(text) if part.strip()]


class EvidenceRetrievalTool(BaseTool):
    """로컬 정제 EMR에서 조건 관련 문장을 검색한다."""

    name = "evidence_retrieval_tool"
    permissions = (Permission(DataStore.KNOWLEDGE_BASE, Action.READ),)
    retrieval_mode = "local_keyword"

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


class BedrockKnowledgeBaseEvidenceRetrievalTool(BaseTool):
    """환자 격리 필터를 강제하는 Bedrock Knowledge Base Retrieve 어댑터."""

    name = "evidence_retrieval_tool"
    permissions = (Permission(DataStore.KNOWLEDGE_BASE, Action.READ),)
    retrieval_mode = "bedrock_graphrag"

    def __init__(
        self,
        *,
        knowledge_base_id: str,
        region: str = "us-east-1",
        client: Any | None = None,
    ) -> None:
        if not knowledge_base_id.strip():
            raise ValueError("knowledge_base_id is required")
        self._knowledge_base_id = knowledge_base_id
        self._region = region
        self._client = client

    def invoke(
        self, context: ToolContext, /, **kwargs: Any
    ) -> list[NarrativeSnippet]:
        self.assert_allowed(Permission(DataStore.KNOWLEDGE_BASE, Action.READ))
        if not context.patient_key or not context.patient_key.startswith("pt_"):
            raise ValueError(
                "Bedrock patient evidence retrieval requires a pseudonymous patient_key"
            )

        terms = tuple(
            str(term).strip() for term in kwargs.get("terms", ()) if str(term).strip()
        )
        if not terms:
            return []
        top_k = max(1, min(int(kwargs.get("top_k", 3)), _MAX_TOP_K))
        query = self._query_text(terms)
        response = self._bedrock_client().retrieve(
            knowledgeBaseId=self._knowledge_base_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "vectorSearchConfiguration": {
                    "numberOfResults": top_k,
                    "filter": {
                        "andAll": [
                            {
                                "equals": {
                                    "key": "patient_key",
                                    "value": context.patient_key,
                                }
                            },
                            {
                                "equals": {
                                    "key": "document_type",
                                    "value": "patient_evidence",
                                }
                            },
                        ]
                    },
                }
            },
        )
        return self._snippets(response, terms, top_k, context.patient_key)

    def _bedrock_client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client(
                "bedrock-agent-runtime", region_name=self._region
            )
        return self._client

    @staticmethod
    def _query_text(terms: tuple[str, ...]) -> str:
        joined = ", ".join(terms)
        return (
            "다음 임상시험 기준 표현과 직접 관련된 환자 임상 근거를 찾아라. "
            f"조건: {joined}. 측정값, 부정 표현, 진단 또는 상태 문장을 우선한다."
        )

    @classmethod
    def _snippets(
        cls,
        response: dict[str, Any],
        terms: tuple[str, ...],
        top_k: int,
        patient_key: str,
    ) -> list[NarrativeSnippet]:
        snippets: list[NarrativeSnippet] = []
        for rank, item in enumerate(response.get("retrievalResults", [])[:top_k], start=1):
            metadata = item.get("metadata") or {}
            if metadata.get("patient_key") != patient_key:
                continue
            if metadata.get("document_type") != "patient_evidence":
                continue
            text = str(item.get("content", {}).get("text") or "").strip()
            if not text:
                continue
            event_match = _EVENT_REFERENCE.search(text)
            location = cls._location(item.get("location") or {})
            source_id = event_match.group(1) if event_match else location
            if not source_id:
                source_id = f"kb-result-{rank}"
            matched = tuple(term for term in terms if term.casefold() in text.casefold())
            score = item.get("score", 0.0)
            try:
                numeric_score = max(0.0, min(float(score), 1.0))
            except (TypeError, ValueError):
                numeric_score = 0.0
            snippets.append(
                NarrativeSnippet(
                    note_id=source_id,
                    encounter_id=source_id,
                    note_date="",
                    snippet=text[:800],
                    matched_terms=matched or terms,
                    score=round(numeric_score, 3),
                )
            )
        return snippets

    @staticmethod
    def _location(location: dict[str, Any]) -> str:
        for location_type in (
            "s3Location",
            "webLocation",
            "confluenceLocation",
            "salesforceLocation",
            "sharePointLocation",
        ):
            value = location.get(location_type)
            if not isinstance(value, dict):
                continue
            for key in ("uri", "url"):
                if value.get(key):
                    return str(value[key])
        return ""


__all__ = [
    "BedrockKnowledgeBaseEvidenceRetrievalTool",
    "EvidenceRetrievalTool",
]
