"""Observability: Agent · Tool · Model 호출 Trace 수집.

AgentCore Observability → CloudWatch 로 보낼 스팬을 로컬에서 동일한 형태로 모은다.
실행별 trace 를 조회할 수 있어야 하므로 run_id 기준으로 보관한다.
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterator, Literal

SpanKind = Literal["AGENT", "TOOL", "MODEL", "GUARDRAIL", "STORE"]


@dataclass
class Span:
    """단일 호출 구간."""

    span_id: str
    run_id: str
    name: str
    kind: SpanKind
    started_at: str
    duration_ms: float
    status: Literal["OK", "ERROR"] = "OK"
    error: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


class TraceCollector:
    """run_id 별 스팬 보관소. CloudWatch EMF 로 내보낼 형태를 유지한다."""

    def __init__(self, *, max_runs: int = 500) -> None:
        self._spans: dict[str, list[Span]] = defaultdict(list)
        self._order: deque[str] = deque(maxlen=max_runs)

    @contextmanager
    def span(
        self,
        run_id: str,
        name: str,
        kind: SpanKind,
        **attributes: Any,
    ) -> Iterator[dict[str, Any]]:
        """with 블록 실행 시간을 측정해 스팬으로 기록한다.

        yield 된 dict 에 값을 넣으면 스팬 attribute 로 병합된다.
        """
        started = time.perf_counter()
        started_at = datetime.now(UTC).isoformat()
        extra: dict[str, Any] = {}
        status: Literal["OK", "ERROR"] = "OK"
        error: str | None = None
        try:
            yield extra
        except Exception as exc:  # noqa: BLE001 - 스팬 기록 후 그대로 전파
            status = "ERROR"
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            duration = (time.perf_counter() - started) * 1000
            if run_id not in self._spans:
                self._order.append(run_id)
            self._spans[run_id].append(
                Span(
                    span_id=f"SPAN-{uuid.uuid4().hex[:12]}",
                    run_id=run_id,
                    name=name,
                    kind=kind,
                    started_at=started_at,
                    duration_ms=round(duration, 3),
                    status=status,
                    error=error,
                    attributes={**attributes, **extra},
                )
            )
            self._evict()

    def _evict(self) -> None:
        """보관 한도를 넘은 run 의 스팬을 제거한다."""
        live = set(self._order)
        for run_id in list(self._spans):
            if run_id not in live:
                del self._spans[run_id]

    def spans_for(self, run_id: str) -> list[dict[str, Any]]:
        return [asdict(span) for span in self._spans.get(run_id, [])]

    def summary_for(self, run_id: str) -> dict[str, Any]:
        """지연시간·에러 집계. CloudWatch 메트릭에 대응한다."""
        spans = self._spans.get(run_id, [])
        if not spans:
            return {
                "run_id": run_id,
                "span_count": 0,
                "total_duration_ms": 0.0,
                "error_count": 0,
                "by_kind": {},
            }
        by_kind: dict[str, dict[str, Any]] = {}
        for span in spans:
            entry = by_kind.setdefault(
                span.kind, {"count": 0, "duration_ms": 0.0, "errors": 0}
            )
            entry["count"] += 1
            entry["duration_ms"] = round(entry["duration_ms"] + span.duration_ms, 3)
            if span.status == "ERROR":
                entry["errors"] += 1
        return {
            "run_id": run_id,
            "span_count": len(spans),
            "total_duration_ms": round(sum(s.duration_ms for s in spans), 3),
            "error_count": sum(1 for s in spans if s.status == "ERROR"),
            "by_kind": by_kind,
        }

    def recent_runs(self, limit: int = 20) -> list[str]:
        return list(self._order)[-limit:][::-1]
