"""Where a case lives between `202 Accepted` and the caller collecting it.

It is a dictionary — `{case_id: CaseRecord}` — and it exists because of one awkward fact:
the POST answers in milliseconds but the analysis takes longer than that, so the answer
is ready after the request that asked for it has gone. It has to wait somewhere.

Two details are worth knowing, and both come from the same place: the browser that
submitted a case is usually not connected yet when the interesting things start
happening.

*Late subscribers.* The page POSTs, gets a case id, and only then opens the socket. By
then a document or two may already be read. So every record is a complete snapshot and a
watcher is handed the current one immediately — subscribing late loses nothing, and
neither does reconnecting.

*Several watchers at once.* Waking them is a broadcast, done by **replacing** an
`asyncio.Event` rather than setting and clearing one. With a single shared event,
whichever watcher woke first would clear it and the rest would sleep through the update.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from app.graph import RunDeps, RunState, case_graph
from app.models import CaseEvent, CaseInput, CaseRecord, CaseRequest, JobStatus, Report

STAGE_DELAY = float(os.getenv('TFD_STAGE_DELAY', '1.0'))
"""The unit of pretend latency, in seconds — every graph step waits some multiple of it,
so a run is slow enough to watch happen. `TFD_STAGE_DELAY=0` makes runs instant."""

MAX_CASES = 50
"""How many finished cases to keep. A prototype's memory is not infinite either."""


class CaseNotFound(KeyError):
    """No case with that id, or it has already been dropped."""

    def __str__(self) -> str:
        return f'no case {self.args[0]!r}'


class CaseExists(ValueError):
    """That case id is already taken.

    Refusing is the safe answer: overwriting would destroy a report the first caller has
    not collected yet, and there is no way from here to tell a retry from a clash.
    """

    def __str__(self) -> str:
        return f'case {self.args[0]!r} already exists'


@dataclass(slots=True)
class _Live:
    """One entry in the store: the record, and the handle watchers wait on."""

    record: CaseRecord
    changed: asyncio.Event = field(default_factory=asyncio.Event)


class CaseStore:
    """Submitted cases, and the machinery for telling anyone watching that one changed."""

    def __init__(self) -> None:
        self._cases: dict[str, _Live] = {}
        self._lock = asyncio.Lock()

    async def get(self, case_id: str) -> CaseRecord:
        """The current record."""
        async with self._lock:
            return self._live(case_id).record

    async def watch(self, case_id: str) -> AsyncIterator[CaseRecord]:
        """Yield the record now, and again after every change, until it is terminal.

        Each yield is the whole state, so a client renders the latest one and is correct
        whenever it connected. Iteration ends by itself at a terminal status, which lets
        a subscription simply run the loop to completion and close.
        """
        while True:
            async with self._lock:
                live = self._live(case_id)
                waiter, record = live.changed, live.record

            # Yielded outside the lock, so a slow client cannot hold up the run writing
            # to the store.
            yield record
            if record.is_terminal:
                return
            await waiter.wait()

    async def create(self, document_count: int, case_id: str | None = None) -> CaseRecord:
        """Register a newly submitted case."""
        case_id = case_id or f'case-{uuid4().hex[:8]}'
        async with self._lock:
            self._evict()
            if case_id in self._cases:
                raise CaseExists(case_id)
            live = _Live(record=CaseRecord(case_id=case_id, document_count=document_count))
            self._cases[case_id] = live
            return live.record

    async def append_event(self, case_id: str, event: CaseEvent) -> None:
        """Add one line to the audit trail and wake every watcher."""
        async with self._lock:
            live = self._live(case_id)
            self._publish(live, events=[*live.record.events, event])

    async def update(self, case_id: str, **fields: object) -> None:
        """Change one case's record under the lock."""
        async with self._lock:
            self._publish(self._live(case_id), **fields)

    async def succeed(self, case_id: str, report: Report) -> None:
        """The run finished and produced an answer."""
        await self.update(
            case_id, status=JobStatus.SUCCEEDED, report=report, finished_at=datetime.now(UTC)
        )

    async def fail(self, case_id: str, detail: str) -> None:
        """The run stopped on an error. A case must always reach a terminal state, or a
        caller polls it forever."""
        await self.update(
            case_id, status=JobStatus.FAILED, error=detail, finished_at=datetime.now(UTC)
        )

    async def forget(self, case_id: str) -> None:
        """Drop a case now, and release anyone watching it."""
        async with self._lock:
            live = self._cases.pop(case_id, None)
            if live is None:
                raise CaseNotFound(case_id)
            live.changed.set()

    # --- internals ------------------------------------------------------------

    def _live(self, case_id: str) -> _Live:
        """The entry for `case_id`, or `CaseNotFound`. Callers must hold the lock."""
        try:
            return self._cases[case_id]
        except KeyError as exc:
            raise CaseNotFound(case_id) from exc

    def _publish(self, live: _Live, **fields: object) -> None:
        """Replace the record with an updated copy and wake everyone watching.

        Records are replaced rather than mutated, so one already handed to a watcher stays
        exactly what that watcher was shown.
        """
        live.record = live.record.model_copy(update=fields)

        # Hand the watchers waiting right now a set event, and leave a fresh one behind
        # for the next change. Replacing rather than clearing is what makes this reach
        # every watcher instead of only the first to wake.
        woken, live.changed = live.changed, asyncio.Event()
        woken.set()

    def _evict(self) -> None:
        """Drop the oldest finished cases once there are too many."""
        finished = sorted(
            (live.record.finished_at, case_id)
            for case_id, live in self._cases.items()
            if live.record.finished_at is not None
        )
        for _, case_id in finished[: len(self._cases) - MAX_CASES]:
            del self._cases[case_id]


class CaseRunner:
    """Accepts cases, runs them through the graph, records what happens.

    This is the only object the API layer talks to. `submit` returns as soon as the case
    is registered; the run continues on a task that outlives the request.
    """

    def __init__(self, store: CaseStore | None = None) -> None:
        self.store = store or CaseStore()
        self._tasks: set[asyncio.Task[None]] = set()

    async def submit(self, request: CaseRequest) -> CaseRecord:
        """Accept a case and hand back its record straight away, still `queued`."""
        record = await self.store.create(len(request.documents), request.case_id)
        case = request.to_case_input(record.case_id)

        task = asyncio.create_task(self._run(case), name=f'case:{case.case_id}')
        # Hold a reference until the task ends, or the loop may collect it mid-run.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return record

    async def aclose(self) -> None:
        """Cancel anything still running. Called by the FastAPI lifespan on the way out."""
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _run(self, case: CaseInput) -> None:
        """Run one case through the graph, and record the outcome exactly once.

        `graph.iter()` rather than `graph.run()` because of the middle column of the
        page: iterating hands back control between nodes, and everything the run has
        recorded since the last look is forwarded to whoever is watching. The graph
        steps stay unaware that anybody is — they only append to `state.events`.
        """
        state = RunState(case_id=case.case_id)
        delivered = 0

        async def drain() -> None:
            """Forward every event the run has produced since the last call."""
            nonlocal delivered
            while delivered < len(state.events):
                event = state.events[delivered]
                delivered += 1
                await self.store.append_event(case.case_id, event)

        try:
            await self.store.update(case.case_id, status=JobStatus.RUNNING)

            async with case_graph.iter(
                state=state, deps=RunDeps(stage_delay=STAGE_DELAY), inputs=case
            ) as run:
                async for _ in run:
                    await drain()
                await drain()  # Whatever the final node recorded on its way out.

            report: Report | None = run.output
            if report is None:  # pragma: no cover - the graph always ends at `compare`
                raise RuntimeError(f'case {case.case_id} finished without a report')
            await self.store.succeed(case.case_id, report)
        except asyncio.CancelledError:
            raise
        except CaseNotFound:
            return  # Deleted mid-run; there is nothing left to report to.
        except Exception as exc:  # noqa: BLE001 - a case must always reach a terminal state
            await self.store.fail(case.case_id, f'{type(exc).__name__}: {exc}')
