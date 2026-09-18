"""The pipeline, as a Pydantic Graph.

    start -> ingest -> (fan out, one branch per document)
                          read
                       (join)
                    -> compare -> end

Three steps and two interesting edges. The `map` edge out of `ingest` forks the run into
one branch per document, so they are read concurrently rather than one after another; the
`collect` join waits for all of them and hands `compare` the full list.

Why a graph at all, for three steps? Because of what comes with it: the fan-out and the
join are declared rather than hand-written, `graph.iter()` gives progress between nodes
without the steps knowing anything about websockets, and `graph.render()` draws the
diagram at the bottom of the README from the same wiring that actually runs — so it can
never quietly go out of date.

`RunState` is what one run accumulates (its audit trail); `RunDeps` is what it is handed
and never changes. Steps read both off their `StepContext` and take nothing else from the
outside world, which is what makes them testable on their own.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zlib import crc32

from pydantic_graph import Graph, GraphBuilder, StepContext, reduce_list_append

from app.detector import compare_documents, read_document
from app.models import CaseEvent, CaseInput, ParsedDocument, RawDocument, Report

FAN_OUT_ID = 'fan_out_documents'
COLLECT_ID = 'collect_documents'

LATENCY_SPREAD = 0.9
"""How much one read's pretend latency may vary from another's.

Without it every branch of the fan-out would finish in the same millisecond and the whole
thing would look sequential from outside. Real calls never do that, so neither does this.
"""


@dataclass
class RunState:
    """What one run accumulates as it goes.

    The fan-out runs as concurrent tasks on a single event loop, and `record` contains no
    `await`, so the appends below are atomic with respect to each other and need no lock.
    """

    case_id: str
    events: list[CaseEvent] = field(default_factory=list)
    """The audit trail so far. The runner drains this between nodes and pushes what it
    finds to whoever is watching — which is why no step here knows what a socket is."""

    def record(self, stage: str, message: str, doc_id: str | None = None) -> None:
        """Append one line to the trail."""
        self.events.append(
            CaseEvent(stage=stage, message=message, at=datetime.now(UTC), doc_id=doc_id)
        )


@dataclass(frozen=True)
class RunDeps:
    """What a run is handed from outside. In the real service this is where the model
    client, the OCR client and the settings live."""

    stage_delay: float = 0.0
    """The unit of pretend latency, in seconds. Every step waits some multiple of it,
    standing in for the model round trip the real service waits on. Zero makes a run
    instant, which is what the tests want and what watching it happen does not."""


builder = GraphBuilder(
    name='document_mismatch',
    state_type=RunState,
    deps_type=RunDeps,
    input_type=CaseInput,
    output_type=Report,
)

collect = builder.join(
    reduce_list_append,
    initial_factory=list[ParsedDocument],
    node_id=COLLECT_ID,
)
"""Gathers one `ParsedDocument` per submitted document, in completion order."""


@builder.step
async def ingest(ctx: StepContext[RunState, RunDeps, CaseInput]) -> list[RawDocument]:
    """Accept the case and hand its documents to the fan-out.

    The only step that may fail the whole case, because what it checks is a property of
    the submission rather than of any one document.
    """
    case = ctx.inputs
    if len(case.documents) < 2:
        raise ValueError('a case needs at least two documents, or there is nothing to compare')

    await asyncio.sleep(ctx.deps.stage_delay * 0.5)
    ctx.state.record('ingest', f'{len(case.documents)} documents received')
    return case.documents


def _read_delay(deps: RunDeps, text: str) -> float:
    """How long this read pretends to take.

    Two things move it, both the way a real call moves: a longer document costs more, and
    no two calls take quite the same time. The variation is derived from the text rather
    than from `random`, so a given document always takes the same time and a demo is
    repeatable.
    """
    jitter = 1 + LATENCY_SPREAD * (crc32(text.encode()) % 100) / 100
    return deps.stage_delay * (1 + len(text) / 2_000) * jitter


@builder.step
async def read(ctx: StepContext[RunState, RunDeps, RawDocument]) -> ParsedDocument:
    """Read one document. Runs once per document, all at the same time.

    All three branches start together and finish apart, which is the thing worth
    watching: the trail fills in *completion* order, not submission order.
    """
    raw = ctx.inputs
    await asyncio.sleep(_read_delay(ctx.deps, raw.text))

    document = read_document(doc_id=raw.doc_id, name=raw.name, text=raw.text)
    ctx.state.record('read', f'{document.title} — {len(document.fields)} fields', raw.doc_id)
    return document


@builder.step
async def compare(ctx: StepContext[RunState, RunDeps, list[ParsedDocument]]) -> Report:
    """Cross-check every document against every other, once they are all in."""
    await asyncio.sleep(ctx.deps.stage_delay)
    documents = sorted(ctx.inputs, key=lambda document: document.doc_id)
    report = compare_documents(documents)
    ctx.state.record(
        'compare', f'{len(report.mismatches)} mismatches, verdict {report.verdict}'
    )
    return report


builder.add(
    builder.edge_from(builder.start_node).to(ingest),
    builder.edge_from(ingest)
    .label('per document')
    .map(fork_id=FAN_OUT_ID, downstream_join_id=COLLECT_ID)
    .to(read),
    builder.edge_from(read).to(collect),
    builder.edge_from(collect).label('all documents').to(compare),
    builder.edge_from(compare).to(builder.end_node),
)

case_graph: Graph[RunState, RunDeps, CaseInput, Report] = builder.build()
"""The built graph. Stateless, and safe to share across concurrent runs."""


def render_mermaid(title: str | None = None) -> str:
    """The pipeline as a Mermaid diagram, drawn from the wiring above."""
    return case_graph.render(title=title)
