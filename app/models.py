"""Every shape that crosses a boundary. This is the whole data model.

There is no document-specific schema anywhere: a document is a title and a bag of
`field -> value`, whatever the fields happen to be called. That is what keeps the
detector generic — it compares documents against each other, not against a rulebook.

`CaseRecord` is the only thing the API ever hands back: from the POST, from the GET, and
from every Socket.IO push. One shape to render, whenever you happen to be looking.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, computed_field


class JobStatus(StrEnum):
    """Where a submitted case has got to."""

    QUEUED = 'queued'
    RUNNING = 'running'
    SUCCEEDED = 'succeeded'
    FAILED = 'failed'

    @property
    def is_terminal(self) -> bool:
        """Whether this case will never change state again."""
        return self in {JobStatus.SUCCEEDED, JobStatus.FAILED}


class Severity(StrEnum):
    """How badly a disagreement hurts."""

    CRITICAL = 'critical'
    """A field on the watchlist disagrees — money, dates, parties."""

    WARNING = 'warning'
    """Some other field disagrees; worth a human glance."""


class Verdict(StrEnum):
    """The answer for a whole case."""

    CLEAN = 'clean'
    NEEDS_REVIEW = 'needs_review'
    BLOCKED = 'blocked'


# --- what comes in ------------------------------------------------------------


class DocumentInput(BaseModel):
    """One document as submitted: a name, and lines of `KEY: VALUE` text."""

    model_config = ConfigDict(extra='forbid')

    text: str = Field(min_length=1)
    name: str | None = Field(default=None, description='Generated from position if omitted.')


class CaseRequest(BaseModel):
    """A set of documents to check against each other, as the API receives them."""

    model_config = ConfigDict(extra='forbid')

    documents: list[DocumentInput] = Field(min_length=2, description='At least two, or there is nothing to compare.')
    case_id: str | None = Field(default=None, description='Generated if omitted.')

    def to_case_input(self, case_id: str) -> CaseInput:
        """The graph's input: the same documents, each now carrying an id.

        Ids are assigned here, once, because every later stage keys on them — a finding
        cites a `doc_id`, and the page turns it back into the name you typed.
        """
        return CaseInput(
            case_id=case_id,
            documents=[
                RawDocument(
                    doc_id=f'doc-{position}',
                    name=document.name or f'Document {position}',
                    text=document.text,
                )
                for position, document in enumerate(self.documents, start=1)
            ],
        )


class RawDocument(BaseModel):
    """One submitted document, with the id it will be known by from here on."""

    doc_id: str
    name: str
    text: str


class CaseInput(BaseModel):
    """What the graph runs on."""

    case_id: str
    documents: list[RawDocument] = Field(default_factory=list)


# --- what the stages produce --------------------------------------------------


class ParsedDocument(BaseModel):
    """One document after the reader has been over it."""

    doc_id: str
    name: str
    title: str
    """The first line, when it isn't a `KEY: VALUE` pair — documents tend to lead with
    what they are."""

    fields: dict[str, str] = Field(default_factory=dict)
    note: str
    """A line from the stub reader, standing in for whatever a model would say about the
    document. Decorative: only `fields` feeds the comparison."""


class Value(BaseModel):
    """One document's version of a field under comparison."""

    doc_id: str
    value: str


class Mismatch(BaseModel):
    """One field that two or more documents disagree about."""

    field: str
    severity: Severity
    explanation: str
    values: list[Value] = Field(default_factory=list)


class Report(BaseModel):
    """What the comparison found."""

    verdict: Verdict
    summary: str
    mismatches: list[Mismatch] = Field(default_factory=list)
    matched_fields: list[str] = Field(default_factory=list)
    """Fields that appeared in more than one document and agreed. Worth reporting: it
    says what was actually looked at, not only what went wrong."""

    documents: list[ParsedDocument] = Field(default_factory=list)

    @computed_field
    @property
    def critical_count(self) -> int:
        """How many disagreements were on watchlist fields."""
        return sum(1 for m in self.mismatches if m.severity is Severity.CRITICAL)


# --- what the caller polls or watches -----------------------------------------


class CaseEvent(BaseModel):
    """One thing that happened during a run, as the caller sees it."""

    model_config = ConfigDict(frozen=True)

    stage: str
    """Which step emitted it: `ingest`, `read` or `compare`."""

    message: str
    at: datetime
    doc_id: str | None = None


class CaseRecord(BaseModel):
    """Everything known about one submitted case, right now.

    A complete snapshot rather than a delta — which is the whole trick. A client that
    subscribes late, reconnects, or just polls once at the end all see the same shape and
    render it the same way, so there is nothing to accumulate and no race to lose.
    """

    case_id: str
    status: JobStatus = JobStatus.QUEUED
    document_count: int = 0
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    events: list[CaseEvent] = Field(default_factory=list)
    """The audit trail so far, in the order the run produced it."""

    report: Report | None = None
    """The finished answer. Present only once `status` is `succeeded`."""

    error: str | None = None

    @property
    def is_terminal(self) -> bool:
        """Whether this record will never change again."""
        return self.status.is_terminal
