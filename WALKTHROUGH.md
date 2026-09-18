# Backend walkthrough

Every snippet in `app/`, in the order that makes it make sense. The README says what the
project is; this says how each line of it works.
[`API_FLOW.md`](API_FLOW.md) is the companion: the same system as call-flow diagrams, if
you would rather see what calls what before reading why.

Six files, about 510 lines of Python excluding docstrings. Read them in this order — each one only uses the ones
above it:

| | file | what it is |
|---|---|---|
| 1 | [`models.py`](app/models.py) | the shapes. No behaviour, just the vocabulary everything else speaks |
| 2 | [`detector.py`](app/detector.py) | the actual logic: read a document, compare documents. Plain sync functions |
| 3 | [`graph.py`](app/graph.py) | the pipeline that calls (2), as a `pydantic-graph` |
| 4 | [`cases.py`](app/cases.py) | where a running case lives, and the task that drives (3) |
| 5 | [`events.py`](app/events.py) | the Socket.IO layer that pushes (4) to a browser |
| 6 | [`main.py`](app/main.py) | FastAPI: the routes, and the wiring that holds it together |

## First, the map

One trip through the system, so the snippets below have somewhere to hang:

```
  browser                     FastAPI              CaseRunner          the graph
     │                           │                     │                   │
     ├─ POST /v1/cases ─────────▶│                     │                   │
     │                           ├─ runner.submit() ──▶│                   │
     │                           │                     ├─ create task ─────┤
     │◀── 202 {case_id} ─────────┤◀─ CaseRecord ───────┤                   │
     │   (queued)                                      │                   │
     │                                                 ├─ graph.iter() ───▶│ ingest
     ├─ socket: subscribe {case_id} ──────────────────▶│                   │ ├─ read ─┐
     │◀── 'case' {whole record} ── store.watch() ──────┤◀── drain events ──┤ ├─ read ─┤ at once
     │◀── 'case' {whole record} ───────────────────────┤◀──────────────────┤ ├─ read ─┘
     │◀── 'case' {record + report} ────────────────────┤◀── succeed() ─────┤ compare
     │◀── 'done' ──────────────────────────────────────┤                   │
```

The one thing to hold on to: **the POST returns before any work happens**, and everything
after that is the browser being told about a `CaseRecord` that keeps changing.

---

# 1 · `models.py` — the shapes

Nothing here does anything. It defines every object that crosses a boundary, so that the
files below can be read without guessing what they are passing around.

## The three enums

```python
class JobStatus(StrEnum):
    QUEUED = 'queued'
    RUNNING = 'running'
    SUCCEEDED = 'succeeded'
    FAILED = 'failed'

    @property
    def is_terminal(self) -> bool:
        return self in {JobStatus.SUCCEEDED, JobStatus.FAILED}
```

`StrEnum` (Python 3.11+) means `JobStatus.QUEUED == 'queued'` is `True` and it serialises
to JSON as the plain string `"queued"` — so the browser gets `"status": "running"`, not
`"status": "JobStatus.RUNNING"`.

```python
>>> JobStatus.QUEUED == 'queued'
True
>>> json.dumps({'status': JobStatus.RUNNING})
'{"status": "running"}'
>>> JobStatus.RUNNING.is_terminal, JobStatus.SUCCEEDED.is_terminal
(False, True)
```

`is_terminal` is the single definition of "this will never change again". Three different
files ask that question, and none of them re-derives it: the store uses it to stop
watching, the socket uses it to send `done`, the record forwards it.

```python
class Severity(StrEnum):
    CRITICAL = 'critical'
    WARNING = 'warning'

class Verdict(StrEnum):
    CLEAN = 'clean'
    NEEDS_REVIEW = 'needs_review'
    BLOCKED = 'blocked'
```

`Severity` is per finding; `Verdict` is per case. Keeping them as separate types is
deliberate — a case is `blocked` *because* a finding is `critical`, and conflating the two
is how you end up with a "critical case" that nobody can define.

## What comes in

```python
class DocumentInput(BaseModel):
    model_config = ConfigDict(extra='forbid')

    text: str = Field(min_length=1)
    name: str | None = Field(default=None, description='Generated from position if omitted.')
```

Three things are being bought here, all by FastAPI reading this class:

- `extra='forbid'` — a request with a typo'd key (`"txt"`) is rejected with a 422 naming
  the offending field, instead of being silently accepted with `text` empty.
- `min_length=1` — an empty document can't be submitted.
- `description=` — shows up in the generated docs at `/docs`.

What that buys, on the wire — the typo is caught twice, once as a missing field and once
as an unexpected one, both with the exact path to it:

```jsonc
// POST /v1/cases  {"documents": [{"txt": "a"}, {"text": "b"}]}
422
{"detail": [
  {"type": "missing",         "loc": ["body", "documents", 0, "text"], "msg": "Field required"},
  {"type": "extra_forbidden", "loc": ["body", "documents", 0, "txt"],  "msg": "Extra inputs are not permitted"}
]}
```

```python
class CaseRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    documents: list[DocumentInput] = Field(min_length=2, description='At least two, or there is nothing to compare.')
    case_id: str | None = Field(default=None, description='Generated if omitted.')
```

`min_length=2` on the list is the one business rule expressed in the schema: comparing
documents needs at least two of them. It is enforced before any of your code runs — which
is why `POST` with one document returns 422 and never reaches the runner.

```jsonc
// POST /v1/cases  {"documents": [{"text": "INVOICE"}]}
422
{"detail": [{
  "type": "too_short",
  "loc":  ["body", "documents"],
  "msg":  "List should have at least 2 items after validation, not 1",
  "ctx":  {"field_type": "List", "min_length": 2, "actual_length": 1}
}]}
```

```python
    def to_case_input(self, case_id: str) -> CaseInput:
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
```

The boundary between "what the caller sent" and "what the system works with". Ids are
assigned **once, here**, because everything downstream keys on them: a finding cites
`doc-2`, and the page turns that back into the name you typed. `enumerate(..., start=1)`
gives `doc-1`, `doc-2`, `doc-3` rather than zero-based ids that read oddly in a UI.

`document.name or f'Document {position}'` — `or` catches both `None` and `''`, so a user
who submits a blank name box still gets a usable label.

In and out — note the second document, submitted without a name, coming back as
`Document 2`:

```python
CaseRequest(documents=[
    DocumentInput(text='INVOICE\nTotal: 5', name='Invoice'),
    DocumentInput(text='ORDER\nTotal: 6'),
]).to_case_input('case-3d04cd6b')
```

```json
{
  "case_id": "case-3d04cd6b",
  "documents": [
    {"doc_id": "doc-1", "name": "Invoice",    "text": "INVOICE\nTotal: 5"},
    {"doc_id": "doc-2", "name": "Document 2", "text": "ORDER\nTotal: 6"}
  ]
}
```

```python
class RawDocument(BaseModel):
    doc_id: str
    name: str
    text: str


class CaseInput(BaseModel):
    case_id: str
    documents: list[RawDocument] = Field(default_factory=list)
```

`CaseInput` is what the graph takes as input. Note `default_factory=list` rather than
`= []`: a bare `[]` would be one list shared by every instance ever created — the classic
mutable-default bug. Pydantic actually guards against this, but the habit is right.

## What the stages produce

```python
class ParsedDocument(BaseModel):
    doc_id: str
    name: str
    title: str
    fields: dict[str, str] = Field(default_factory=dict)
    note: str
```

The whole of "what we understood about a document". `fields` being a plain
`dict[str, str]` — not a schema per document type — is what makes the detector generic.
There is no `LetterOfCredit` class anywhere.

`note` is the stub model's sentence about the document. It is on the model rather than
thrown away so that the page can show where a real model's output *would* appear.

```python
class Value(BaseModel):
    doc_id: str
    value: str


class Mismatch(BaseModel):
    field: str
    severity: Severity
    explanation: str
    values: list[Value] = Field(default_factory=list)
```

A `Mismatch` carries `values` — every document's version of the disputed field, each
tagged with its `doc_id`. That is what lets the page render the little table of "Purchase
order said 50,000.00, Invoice said 51,000.00" without going back to fetch anything.

```python
class Report(BaseModel):
    verdict: Verdict
    summary: str
    mismatches: list[Mismatch] = Field(default_factory=list)
    matched_fields: list[str] = Field(default_factory=list)
    documents: list[ParsedDocument] = Field(default_factory=list)

    @computed_field
    @property
    def critical_count(self) -> int:
        return sum(1 for m in self.mismatches if m.severity is Severity.CRITICAL)
```

`@computed_field` on top of `@property` is the important bit.

A normal field — `summary`, `mismatches` — is something you fill in: Pydantic stores it,
and it shows up automatically when the model is exported to JSON. A plain `@property` is
ordinary Python, not a Pydantic idea at all: it lets you write `report.critical_count` and
have the value calculated on the fly, with nothing stored. But Pydantic does not know your
property exists. When `model_dump()` turns the model into a dict, it walks the *declared
fields* and nothing else — so `critical_count` would work perfectly in Python code and
vanish from the JSON the browser receives.

`@computed_field` is Pydantic's way of being told: this is not a stored field, but treat it
as one when exporting.

Side by side:

```python
# Without @computed_field
class Report(BaseModel):
    mismatches: list = []

    @property
    def critical_count(self) -> int:
        return len(self.mismatches)

r = Report(mismatches=[1, 2, 3])
r.critical_count   # 3 — works in Python
r.model_dump()     # {"mismatches": [1, 2, 3]} — critical_count is missing
```

```python
# With @computed_field
class Report(BaseModel):
    mismatches: list = []

    @computed_field
    @property
    def critical_count(self) -> int:
        return len(self.mismatches)

r = Report(mismatches=[1, 2, 3])
r.critical_count   # 3 — works in Python
r.model_dump()     # {"mismatches": [1, 2, 3], "critical_count": 3} — included
```

In one line: `@property` alone is a calculated value visible only inside Python;
`@computed_field` + `@property` is a calculated value that also appears in the dict and
JSON output.

It lands in the OpenAPI schema too, and it stays derived — there is no way for
`critical_count` to disagree with `mismatches`, because it is recomputed every time.

Note the order: `@computed_field` must be the outer decorator.

`matched_fields` exists because a report that only lists problems is not trustworthy. It
says what was checked and agreed, so you can tell "nothing was wrong" from "nothing was
looked at".

## What the caller polls or watches

```python
class CaseEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    stage: str
    message: str
    at: datetime
    doc_id: str | None = None
```

`frozen=True` makes instances immutable (and hashable). An audit trail entry that could be
edited after the fact is not an audit trail. It also makes the event safe to hand to
several watchers at once.

```python
>>> event = CaseEvent(stage='ingest', message='3 documents received', at=datetime.now(UTC))
>>> event.message = 'edited'
ValidationError: 1 validation error for CaseEvent
message
  Instance is frozen [type=frozen_instance, input_value='edited', input_type=str]
```

```python
class CaseRecord(BaseModel):
    case_id: str
    status: JobStatus = JobStatus.QUEUED
    document_count: int = 0
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    events: list[CaseEvent] = Field(default_factory=list)
    report: Report | None = None
    error: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal
```

**This is the most important class in the project.** It is the only thing the API ever
returns — from the POST, from the GET, and from every socket push.

Three details:

- `default_factory=lambda: datetime.now(UTC)` — a lambda, because `datetime.now(UTC)`
  written directly would be evaluated once at import time and every case would claim to
  have been submitted when the server booted.
- `report` and `error` are both optional and mutually exclusive in practice: `report` is
  set when `status` is `succeeded`, `error` when it is `failed`. The type system does not
  enforce that; the runner does.
- `is_terminal` is a plain `@property`, not `@computed_field` — deliberately. The browser
  can derive it from `status`, so shipping it would be duplication.

The same record at the two ends of a run. This is the entire API surface — the POST
returns the first, the last socket push is the second, and a GET at any moment returns
something between them:

```json
// straight out of POST /v1/cases
{
  "case_id": "case-demo",
  "status": "queued",
  "document_count": 3,
  "submitted_at": "2026-09-17T10:14:58.159696Z",
  "finished_at": null,
  "events": [],
  "report": null,
  "error": null
}
```

```json
// the same case once it is done
{
  "case_id": "case-demo",
  "status": "succeeded",
  "document_count": 3,
  "submitted_at": "2026-09-17T10:14:58.159696Z",
  "finished_at": "2026-09-17T10:14:58.165653Z",
  "events": [
    {"stage": "ingest",  "message": "3 documents received",        "at": "...", "doc_id": null},
    {"stage": "read",    "message": "PURCHASE ORDER — 7 fields",   "at": "...", "doc_id": "doc-1"},
    {"stage": "read",    "message": "INVOICE — 7 fields",          "at": "...", "doc_id": "doc-2"},
    {"stage": "read",    "message": "DELIVERY NOTE — 7 fields",    "at": "...", "doc_id": "doc-3"},
    {"stage": "compare", "message": "3 mismatches, verdict blocked", "at": "...", "doc_id": null}
  ],
  "report": { "verdict": "blocked", "critical_count": 3, "...": "..." },
  "error": null
}
```

Nothing is added to the *shape* between those two — the fields are all present from the
first push, just empty. That is what lets the page render the same way whenever it looks.

---

# 2 · `detector.py` — the logic

Plain synchronous functions. No `async`, no framework, no I/O — which means every one of
them can be called from a REPL with a string and checked by eye. That is the payoff of
keeping the logic out of the pipeline.

## The stand-in for the model

```python
_POOLS: dict[str, tuple[str, ...]] = {
    'read': (
        'Read cleanly; the labelled fields came through without trouble.',
        ...
    ),
    'summary': (
        'Full detail is listed below, most serious first.',
        ...
    ),
}


def default_reply(prompt: str, *, kind: str = 'read') -> str:
    pool = _POOLS.get(kind, _POOLS['read'])
    return pool[crc32(prompt.encode()) % len(pool)]
```

This is the whole of the "AI". It takes a prompt and returns words, which is the same
shape a real call has — so replacing it is a change here and nowhere else.

`crc32(prompt.encode()) % len(pool)` picks a line by checksum of the prompt. Two
consequences, both wanted:

- **The same prompt always gives the same line**, like a real call at temperature zero.
- **It survives a restart.** Python's built-in `hash()` on a string is salted per process
  (a security measure against hash-collision attacks), so `hash('x') % 4` gives a different
  answer in each run. `crc32` is a fixed algorithm and does not move.

The three sample documents, every run, on any machine — and note that two of them land on
the same line, which four canned answers across three documents makes likely:

```python
>>> default_reply(SAMPLE[0]['text'], kind='read')   # purchase order
'Nothing unusual on this one; the headings were where you would expect them.'
>>> default_reply(SAMPLE[1]['text'], kind='read')   # invoice
'Scanned it end to end and found the fields well formed.'
>>> default_reply(SAMPLE[2]['text'], kind='read')   # delivery note
'Nothing unusual on this one; the headings were where you would expect them.'
```

`*, kind` — the `*` makes `kind` keyword-only, so calls read as
`default_reply(text, kind='read')` and never as an unexplained positional `True`-style
argument.

## Reading one document

```python
FIELD_LINE = re.compile(r'^\s*([^:\n]{1,40}?)\s*:\s*(.+?)\s*$')
```

Worth taking apart, because it is the only regex in the project:

| piece | meaning |
|---|---|
| `^\s*` | allow leading indentation |
| `([^:\n]{1,40}?)` | **group 1, the label**: 1–40 characters, none of them a colon |
| `\s*:\s*` | the colon, with optional space either side |
| `(.+?)` | **group 2, the value**: at least one character |
| `\s*$` | trailing space ignored |

The `{1,40}` ceiling is the interesting constraint. It is what stops prose from being
mistaken for data — a line like

```
The buyer confirmed on Tuesday that delivery is fine: see attached
```

has 52 characters before its colon, so it fails to match and is skipped. Drop the ceiling
and it parses as a field named *"The buyer confirmed on Tuesday that delivery is fine"*.
Capping the label length means a label has to look like a label.

The lazy `?` on `(.+?)` does a smaller job: paired with `\s*$` it keeps trailing spaces out
of the **value**. Greedy, `Total Amount: 51,000.00   ` captures `'51,000.00   '`; lazy, it
captures `'51,000.00'` and lets `\s*$` absorb the rest. (The `?` on `{1,40}?` is redundant
— `[^:\n]` already forbids colons in the label, so the match stops at the first one
either way. Harmless, but it is not doing the work it looks like it is doing.)

A colon in the *value* is fine, because the label class excludes colons: `Time: 10:30`
reads as `Time` → `10:30`.

All of that, run:

| input line | `(label, value)` |
|---|---|
| `'Total Amount: 51,000.00   '` | `('Total Amount', '51,000.00')` — trailing space gone |
| `'Time: 10:30'` | `('Time', '10:30')` — the second colon is value, not label |
| `'  Order   No : PO-1042'` | `('Order   No', 'PO-1042')` — inner spacing kept, `_key` sorts it out later |
| `'PURCHASE ORDER'` | `None` — no colon, so it becomes the title instead |
| `'The buyer confirmed on Tuesday that delivery is fine: see attached'` | `None` — 52 characters before the colon |

```python
def read_document(doc_id: str, name: str, text: str) -> ParsedDocument:
    fields: dict[str, str] = {}
    title = ''

    for line in text.splitlines():
        if match := FIELD_LINE.match(line):
            label, value = match.group(1), match.group(2)
            fields.setdefault(label, value)  # First mention wins; later ones are echoes.
        elif not title and line.strip():
            title = line.strip()

    return ParsedDocument(
        doc_id=doc_id,
        name=name,
        title=title or name,
        fields=fields,
        note=default_reply(text, kind='read'),
    )
```

- `if match := FIELD_LINE.match(line)` — the walrus operator assigns *and* tests in one
  step. Without it you would write `match = ...` then `if match:` on the next line.
- `fields.setdefault(label, value)` rather than `fields[label] = value`: **first mention
  wins**. On a real document a label often recurs in a footer or a repeated header, and the
  first occurrence is the authoritative one.
- `elif not title and line.strip()` — the first non-empty line that *isn't* a field becomes
  the title. `not title` means only the first such line qualifies; every later one is
  ignored.
- `title or name` — a document that is nothing but `KEY: VALUE` lines has no title line, so
  it falls back to the name it was submitted under.

One document through it, start to finish:

```python
read_document('doc-1', 'Purchase order', '''PURCHASE ORDER
Order No: PO-1042
Buyer: Northwind Imports
Seller: Acme Trading
Currency: USD
Total Amount: 50,000.00
Delivery Date: 2026-03-01
Ship To: Hamburg''')
```

```json
{
  "doc_id": "doc-1",
  "name": "Purchase order",
  "title": "PURCHASE ORDER",
  "fields": {
    "Order No": "PO-1042",
    "Buyer": "Northwind Imports",
    "Seller": "Acme Trading",
    "Currency": "USD",
    "Total Amount": "50,000.00",
    "Delivery Date": "2026-03-01",
    "Ship To": "Hamburg"
  },
  "note": "Nothing unusual on this one; the headings were where you would expect them."
}
```

`PURCHASE ORDER` had no colon, so it became the `title` rather than a field — which is the
whole of the title rule, visible in one line of output.

## Comparing them

```python
WATCHED = (
    'amount', 'total', 'price', 'value', 'sum',
    'date', 'expiry', 'shipped', 'due',
    'currency', 'quantity', 'qty', 'weight', 'count',
    'buyer', 'seller', 'shipper', 'consignee', 'beneficiary', 'applicant', 'party',
)
```

The entire severity model. A disagreement is `critical` if the field's *name* contains any
of these words. It is crude and it is honest about being crude — the real project has UCP
600 article references in its place.

```python
def _key(label: str) -> str:
    return ' '.join(label.lower().split())
```

The form two labels are matched on. `.split()` with no argument splits on *any* run of
whitespace and drops empties, so `'  Order   No '` and `'order no'` both become
`'order no'`. This is why `Order No` on one document matches `ORDER NO` on another.

```python
>>> _key('  Order   No ')
'order no'
>>> _key('ORDER NO')
'order no'
>>> _key('Total Amount')
'total amount'
```

```python
def _comparable(value: str) -> str:
    value = value.lower().strip().rstrip('.')
    value = re.sub(r'(?<=\d),(?=\d{3}\b)', '', value)
    return ' '.join(value.split())
```

The form two *values* are compared as. The middle line deletes thousands separators:
`(?<=\d)` is a lookbehind (a digit before the comma) and `(?=\d{3}\b)` a lookahead
(exactly three digits after it). Neither consumes characters — they only assert context —
so only a comma sitting between `1` and `000` is removed.

That is what makes `USD 51,000.00` and `usd 51000.00` compare equal, while a comma in
`Hamburg, Germany` is left alone because `Germany` is not three digits.

```python
>>> _comparable('USD 51,000.00')
'usd 51000.00'
>>> _comparable('usd 51000.00 ')      # same string out — so these two agree
'usd 51000.00'
>>> _comparable('Hamburg, Germany')   # comma survives: not a thousands separator
'hamburg, germany'
>>> _comparable('Acme Trading Ltd.')  # the trailing full stop goes
'acme trading ltd'
```

Worth seeing what it does *not* do: `'Acme Trading'` and `'acme trading ltd'` are still
different strings, which is why the sample's `Seller` comes back as a mismatch.

```python
def _severity(key: str) -> Severity:
    return Severity.CRITICAL if any(word in key for word in WATCHED) else Severity.WARNING
```

`any(...)` with a generator short-circuits on the first hit. Note it takes the *normalised*
key, so the watchlist only has to be written in lower case.

```python
>>> _severity('total amount')    # 'total' and 'amount' both hit
Severity.CRITICAL
>>> _severity('delivery date')   # 'date'
Severity.CRITICAL
>>> _severity('seller')
Severity.CRITICAL
>>> _severity('order no')        # nothing on the list
Severity.WARNING
>>> _severity('ship to')
Severity.WARNING
```

It is substring matching, not word matching — which is why `'delivery date'` is caught by
`'date'`, and also why a field called `'Valuation Method'` would be caught by `'value'`.
Crude in both directions.

```python
def compare_documents(documents: list[ParsedDocument]) -> Report:
    seen: dict[str, list[tuple[ParsedDocument, str, str]]] = {}
    for document in documents:
        for label, value in document.fields.items():
            seen.setdefault(_key(label), []).append((document, label, value))
```

The inversion that the whole comparison rests on. Input is *documents, each with fields*;
this turns it into **fields, each with documents**:

```
{'order no':     [(po, 'Order No', 'PO-1042'), (inv, 'Order No', 'PO-1042'), ...],
 'total amount': [(po, 'Total Amount', '50,000.00'), (inv, 'Total Amount', '51,000.00')],
 'carrier':      [(dn, 'Carrier', 'Pacific Lines')]}
```

Each entry keeps three things: the document (for its `doc_id`), the label *as that document
wrote it* (for display), and the value. `setdefault(key, []).append(...)` is the
one-line "append to a list that may not exist yet".

```python
    for key, entries in seen.items():
        if len(entries) < 2:
            continue

        label = entries[0][1]
        distinct = {_comparable(value) for _, _, value in entries}
        if len(distinct) == 1:
            matched.append(label)
            continue
```

- `len(entries) < 2` → skip. A field only one document mentions is not a disagreement;
  reporting it would bury the real findings under `Invoice No` and `Carrier`.
- `label = entries[0][1]` — display the label the way the *first* document spelled it.
- `distinct = {...}` is a **set comprehension**, so it collapses duplicates. If all
  entries normalise to one string, `len(distinct) == 1` and everyone agrees. This is
  three-or-more-document agreement for free: no pairwise loop.

```python
        mismatches.append(
            Mismatch(
                field=label,
                severity=_severity(key),
                explanation=(
                    f'{len(entries)} documents state {label!r} and '
                    f'{len(distinct)} different values were found.'
                ),
                values=[Value(doc_id=doc.doc_id, value=value) for doc, _, value in entries],
            )
        )
```

`{label!r}` uses `repr()`, which puts quotes round the field name in the sentence
(`'Total Amount'`). The `values` list keeps **every** document's version, including the
ones that agreed with each other — an examiner wants to see the full picture, not only the
odd one out.

The three sample documents in, the whole `Report` out (`documents` elided — it is the three
`ParsedDocument`s from above, unchanged):

```json
{
  "verdict": "blocked",
  "summary": "Checked 3 documents on 7 shared fields: 4 agreed, 3 did not (3 critical). Anything only one document mentioned was left out of the comparison.",
  "mismatches": [
    {
      "field": "Delivery Date",
      "severity": "critical",
      "explanation": "2 documents state 'Delivery Date' and 2 different values were found.",
      "values": [
        {"doc_id": "doc-1", "value": "2026-03-01"},
        {"doc_id": "doc-3", "value": "2026-03-04"}
      ]
    },
    {
      "field": "Seller",
      "severity": "critical",
      "explanation": "3 documents state 'Seller' and 2 different values were found.",
      "values": [
        {"doc_id": "doc-1", "value": "Acme Trading"},
        {"doc_id": "doc-2", "value": "Acme Trading"},
        {"doc_id": "doc-3", "value": "Acme Trading Ltd"}
      ]
    },
    {
      "field": "Total Amount",
      "severity": "critical",
      "explanation": "2 documents state 'Total Amount' and 2 different values were found.",
      "values": [
        {"doc_id": "doc-1", "value": "50,000.00"},
        {"doc_id": "doc-2", "value": "51,000.00"}
      ]
    }
  ],
  "matched_fields": ["Buyer", "Currency", "Order No", "Ship To"],
  "documents": ["..."],
  "critical_count": 3
}
```

Three things to read off it. `Seller` carries **three** values though only two are
distinct — doc-1 and doc-2 agreeing is part of the evidence. `Invoice No` and `Carrier`
are nowhere, because one document each mentioned them. And `matched_fields` has four
entries, so 7 shared fields were examined in total: the number the summary quotes.

```python
    mismatches.sort(key=lambda m: (m.severity is not Severity.CRITICAL, m.field.lower()))
    matched.sort(key=str.lower)
```

Sorting by a tuple sorts by the first element, then the second. `is not CRITICAL` is
`False` (= 0) for critical findings and `True` (= 1) for warnings, so criticals sort first;
ties break alphabetically. The result is stable between runs, which matters because
dictionary iteration order follows insertion order and insertion order here follows
whichever branch of the fan-out finished first.

```python
def _verdict(mismatches: list[Mismatch]) -> Verdict:
    if any(m.severity is Severity.CRITICAL for m in mismatches):
        return Verdict.BLOCKED
    return Verdict.NEEDS_REVIEW if mismatches else Verdict.CLEAN
```

One critical blocks. Otherwise, any disagreement at all needs a human. Otherwise clean.

```python
>>> _verdict([critical_mismatch, warning_mismatch])
Verdict.BLOCKED
>>> _verdict([warning_mismatch])
Verdict.NEEDS_REVIEW
>>> _verdict([])
Verdict.CLEAN
```

> **A rough edge, left in on purpose:** a case where no field appears on two documents
> also returns `clean` — nothing disagreed, but nothing was checked either.

```python
def _summary(mismatches: list[Mismatch], matched: list[str], documents: list[ParsedDocument]) -> str:
    critical = sum(1 for m in mismatches if m.severity is Severity.CRITICAL)
    counted = (
        f'Checked {len(documents)} documents on {len(matched) + len(mismatches)} shared fields: '
        f'{len(matched)} agreed, {len(mismatches)} did not ({critical} critical).'
    )
    return f'{counted} {default_reply(counted, kind="summary")}'
```

The seam, made visible in one line. `counted` is computed from the data and is always
true. `default_reply(...)` is the stub talking. The summary is literally the two
concatenated — the half a model would write, and the half you would never let it near.

On the sample, with the seam marked:

```
Checked 3 documents on 7 shared fields: 4 agreed, 3 did not (3 critical). Anything only one document mentioned was left out of the comparison.
└────────────────────── counted, from the data ───────────────────────┘ └──────────────────── default_reply(), the stub ────────────────────┘
```

Every number in the first half is arithmetic over `mismatches` and `matched`. The second
half is a canned sentence chosen by checksum. Swap `default_reply` for a real model and
the first half does not change.

---

# 3 · `graph.py` — the pipeline

The three functions above, wired into a `pydantic-graph`. This is the file to read slowly
if the library is new to you.

```
start → ingest → ⑂ fan_out_documents → read → ⑃ collect_documents → compare → end
```

## State and deps

A graph run has two pieces of context, and the split matters:

```python
@dataclass
class RunState:
    case_id: str
    events: list[CaseEvent] = field(default_factory=list)

    def record(self, stage: str, message: str, doc_id: str | None = None) -> None:
        self.events.append(
            CaseEvent(stage=stage, message=message, at=datetime.now(UTC), doc_id=doc_id)
        )
```

**State is what one run accumulates.** One instance per case, mutated as it goes.

```python
>>> state = RunState(case_id='case-3d04cd6b')
>>> state.record('ingest', '3 documents received')
>>> state.events
[CaseEvent(stage='ingest', message='3 documents received',
           at=datetime(2026, 9, 17, 10, 12, 52, 594594, tzinfo=UTC), doc_id=None)]
```

`doc_id` is `None` here because `ingest` is about the case, not about a document. The
`read` step passes one, which is how the page attributes a line to a document.

The concurrency question this raises: three `read` branches run at once and all three call
`record()` — is that safe without a lock? Yes, and for a specific reason. They are
coroutines on a single event loop, not threads, so they only interleave at an `await`.
`record()` contains no `await`, so once it starts it runs to completion before any other
branch gets the loop back. `list.append` is never interrupted here.

```python
@dataclass(frozen=True)
class RunDeps:
    stage_delay: float = 0.0
```

**Deps are what the run is handed and never changes.** `frozen=True` enforces that. In the
real service this is where the model client, the OCR client and the settings live — the
things you want to swap in a test.

The rule that falls out: a step takes everything from its `StepContext` and nothing from
module scope. That is what makes a step callable in a test with a hand-made context.

## Declaring the graph

```python
builder = GraphBuilder(
    name='document_mismatch',
    state_type=RunState,
    deps_type=RunDeps,
    input_type=CaseInput,
    output_type=Report,
)
```

Four type parameters, and the builder type-checks the wiring against them: a step whose
input type does not match what the previous step returns is an error at `build()` time,
not at 3am.

```python
collect = builder.join(
    reduce_list_append,
    initial_factory=list[ParsedDocument],
    node_id=COLLECT_ID,
)
```

The join. `reduce_list_append` is a reducer supplied by the library — each branch that
arrives gets appended to an accumulator. `initial_factory=list[ParsedDocument]` is what the
accumulator starts as; it is a factory (called per run) rather than a value, for the same
mutable-default reason as before.

Note that `list[ParsedDocument]` is *callable* — `list[X]()` returns `[]`. The generic
alias works as a factory and documents the element type at the same time.

## The steps

```python
@builder.step
async def ingest(ctx: StepContext[RunState, RunDeps, CaseInput]) -> list[RawDocument]:
    case = ctx.inputs
    if len(case.documents) < 2:
        raise ValueError('a case needs at least two documents, or there is nothing to compare')

    await asyncio.sleep(ctx.deps.stage_delay * 0.5)
    ctx.state.record('ingest', f'{len(case.documents)} documents received')
    return case.documents
```

`@builder.step` registers the function as a node. The three type parameters on
`StepContext[State, Deps, Input]` are how the builder knows what this node consumes; the
return annotation is how it knows what it produces. `ctx` carries exactly three things:
`ctx.inputs`, `ctx.state`, `ctx.deps`.

This is the only step allowed to fail the whole case, because what it checks is a property
of the submission rather than of one document. (The same rule is already enforced by
`CaseRequest.min_length=2`, so this is the belt to that braces — it also guards a caller
who builds a `CaseInput` directly.)

Returning `list[RawDocument]` is what makes the next edge able to fan out: you can only
map over something iterable.

```python
def _read_delay(deps: RunDeps, text: str) -> float:
    jitter = 1 + LATENCY_SPREAD * (crc32(text.encode()) % 100) / 100
    return deps.stage_delay * (1 + len(text) / 2_000) * jitter
```

How long a read pretends to take. Two factors, both mimicking a real call:

- `1 + len(text) / 2_000` — a longer document costs more.
- `jitter`, in `[1.0, 1.891]` (that is `1 + LATENCY_SPREAD * 0..99/100`) — no two calls
  take quite the same time.

`crc32` again rather than `random`, so a given document always takes the same time and a
demo is repeatable. **Without the jitter the fan-out is invisible**: the sample documents
are all about 150 characters, so all three branches would finish in the same millisecond
and concurrent work would look sequential from outside.

The three sample documents at `stage_delay=1.0`:

| document | `len(text)` | delay |
|---|---|---|
| Purchase order | 159 | 1.371s |
| Invoice | 147 | 1.644s |
| Delivery note | 161 | 1.139s |

The longest document is not the slowest — jitter dominates at this size. And those three
delays, sorted, are exactly the order the audit trail comes back in:

```
ingest   doc_id=None    3 documents received
read     doc_id=doc-3   DELIVERY NOTE — 7 fields      ← 1.139s, finished first
read     doc_id=doc-1   PURCHASE ORDER — 7 fields     ← 1.371s
read     doc_id=doc-2   INVOICE — 7 fields            ← 1.644s
compare  doc_id=None    3 mismatches, verdict blocked
```

Submission order was doc-1, doc-2, doc-3. The trail is doc-3, doc-1, doc-2 — that
reordering *is* the fan-out, visible from outside. Set `TFD_STAGE_DELAY=0` and every delay
collapses to zero, so the reads complete in submission order and the same run looks
sequential.

```python
@builder.step
async def read(ctx: StepContext[RunState, RunDeps, RawDocument]) -> ParsedDocument:
    raw = ctx.inputs
    await asyncio.sleep(_read_delay(ctx.deps, raw.text))

    document = read_document(doc_id=raw.doc_id, name=raw.name, text=raw.text)
    ctx.state.record('read', f'{document.title} — {len(document.fields)} fields', raw.doc_id)
    return document
```

Note the input type: `RawDocument`, singular. `ingest` returned a *list*, but this step
receives *one* — because the edge between them maps. The step has no idea it is one of
three; it is written as if it were the only one.

The `await asyncio.sleep(...)` is also what lets the other two branches run. Take it away
and there is no suspension point, so the three coroutines would effectively run one after
another.

```python
@builder.step
async def compare(ctx: StepContext[RunState, RunDeps, list[ParsedDocument]]) -> Report:
    await asyncio.sleep(ctx.deps.stage_delay)
    documents = sorted(ctx.inputs, key=lambda document: document.doc_id)
    report = compare_documents(documents)
    ctx.state.record(
        'compare', f'{len(report.mismatches)} mismatches, verdict {report.verdict}'
    )
    return report
```

Back to a list — this is downstream of the join, so it gets all of them.

`sorted(..., key=lambda d: d.doc_id)` matters: the join appends in **completion** order, so
without this the report's document list would be in whatever order the reads happened to
finish, and the page would reshuffle between runs. Sorting by `doc_id` restores submission
order for display.

Returning `Report` — the graph's declared `output_type` — is what ends the run.

## The wiring

```python
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
```

Five edges. Four are ordinary `A → B`. The second one is the whole reason this is a graph:

- `.map(...)` turns one edge into *N* branches, one per element of the list `ingest`
  returned. They run concurrently.
- `fork_id` names the fork and `downstream_join_id` says which join closes it — that
  pairing is what the library needs to know how many branches to wait for.
- `.label(...)` only affects the rendered diagram.

```python
case_graph: Graph[RunState, RunDeps, CaseInput, Report] = builder.build()
```

`build()` validates the structure — every node reachable, every join paired with its fork,
every edge's types lining up — and returns an immutable graph. It holds no per-run state,
so one instance is shared by every concurrent case.

```python
def render_mermaid(title: str | None = None) -> str:
    return case_graph.render(title=title)
```

The diagram, generated from the wiring above rather than drawn by hand. Served at
`GET /v1/graph`, which is why the picture in the README cannot quietly go stale.

```
---
title: Document mismatch pipeline
---
stateDiagram-v2
  ingest
  state fan_out_documents <<fork>>
  read
  state collect_documents <<join>>
  compare

  [*] --> ingest
  ingest --> fan_out_documents: per document
  fan_out_documents --> read
  read --> collect_documents
  collect_documents --> compare: all documents
  compare --> [*]
```

Every line of that came from the five `builder.edge_from(...)` calls above — including the
two labels, and the `<<fork>>` / `<<join>>` markers that the `.map()` edge implies.

---

# 4 · `cases.py` — the store and the runner

The hardest file in the project, for one reason: the POST answers in milliseconds but the
work takes seconds, so the answer is ready long after the request that asked for it has
gone. It has to wait somewhere, and somebody has to be woken when it changes.

Two classes. `CaseStore` holds records and wakes watchers; `CaseRunner` drives the graph.

## Module setup

```python
STAGE_DELAY = float(os.getenv('TFD_STAGE_DELAY', '1.0'))
MAX_CASES = 50


class CaseNotFound(KeyError):
    def __str__(self) -> str:
        return f'no case {self.args[0]!r}'


class CaseExists(ValueError):
    def __str__(self) -> str:
        return f'case {self.args[0]!r} already exists'
```

`CaseNotFound` subclasses `KeyError` because that is what it is — a missing dictionary key.
Subclassing rather than raising `KeyError` directly is what lets `main.py` register one
exception handler that turns it into a 404 wherever it is raised.

The `__str__` override exists because `KeyError`'s default `str()` adds its own quotes, so
the message would come out as `"no case 'x'"` wrapped in another set. `self.args[0]` is
the value passed to the constructor.

## One entry in the store

```python
@dataclass(slots=True)
class _Live:
    record: CaseRecord
    changed: asyncio.Event = field(default_factory=asyncio.Event)
```

The leading underscore says "internal". `slots=True` swaps the instance `__dict__` for a
fixed layout — smaller and faster, and it makes a typo'd attribute an error rather than a
silently created one.

Two fields: the current record, and **the handle watchers wait on**. Everything
interesting below is about that second one.

## Reading

```python
    async def get(self, case_id: str) -> CaseRecord:
        async with self._lock:
            return self._live(case_id).record
```

The lock is an `asyncio.Lock`, not a threading one — it guards against interleaving at
`await` points on a single loop, not against other threads.

```python
    async def watch(self, case_id: str) -> AsyncIterator[CaseRecord]:
        while True:
            async with self._lock:
                live = self._live(case_id)
                waiter, record = live.changed, live.record

            yield record
            if record.is_terminal:
                return
            await waiter.wait()
```

Read this one twice — it is the core of the live update.

**Why grab `waiter` *before* yielding.** The order is: take hold of the current event
object, release the lock, hand the record over, then wait on the event you took. If an
update lands during the `yield` (while the lock is free), it sets *that very event* — the
one this watcher is about to wait on — so `waiter.wait()` returns immediately instead of
blocking. Grab the event after yielding instead and you have a race: the update fires, then
you start waiting for the next one, and the change you just missed never arrives.

**Why yield outside the lock.** A slow client — one on a bad connection — must not be able
to hold a lock that the running case needs in order to record its next event.

**Why it ends by itself.** `if record.is_terminal: return` ends the generator after
yielding the final state. The socket layer can therefore just run the loop to completion
and then send `done`; it needs no separate signal.

This is an **async generator** — a function with both `async` and `yield`. Callers consume
it with `async for`.

One full subscription, from `async for record in store.watch('case-demo')`:

```
status=queued     events=0   report=None   terminal=False   ← yielded immediately, before any wait
status=running    events=0   report=None   terminal=False
status=running    events=1   report=None   terminal=False   ← ingest
status=running    events=2   report=None   terminal=False   ← a read
status=running    events=3   report=None   terminal=False   ← a read
status=running    events=4   report=None   terminal=False   ← a read
status=running    events=5   report=None   terminal=False   ← compare
status=succeeded  events=5   report=yes    terminal=True    ← loop ends here, by itself
```

Eight yields, one per change, each one a whole record. The first arrives before anything
has happened, which is the late-subscriber guarantee in action: a client that connects at
the sixth line gets `events=4` immediately and has missed nothing it needs.

## Writing

```python
    async def create(self, document_count: int, case_id: str | None = None) -> CaseRecord:
        case_id = case_id or f'case-{uuid4().hex[:8]}'
        async with self._lock:
            self._evict()
            if case_id in self._cases:
                raise CaseExists(case_id)
            live = _Live(record=CaseRecord(case_id=case_id, document_count=document_count))
            self._cases[case_id] = live
            return live.record
```

`uuid4().hex[:8]` gives a short readable id like `case-3d04cd6b`. Eight hex characters is
4 billion combinations — plenty when at most 50 are held at once.

Refusing a duplicate id rather than overwriting is the safe default: overwriting would
destroy a report its owner has not collected yet, and there is no way from here to tell a
retry from a clash. `CaseExists` is its own class for the same reason `CaseNotFound` is —
`main.py` registers one handler that turns it into a 409 wherever it is raised.

```python
    async def append_event(self, case_id: str, event: CaseEvent) -> None:
        async with self._lock:
            live = self._live(case_id)
            self._publish(live, events=[*live.record.events, event])
```

`[*old, new]` builds a **new list** rather than appending to the existing one. That matters
because of `_publish` below — a record already handed to a watcher must not change under
them.

```python
    async def succeed(self, case_id: str, report: Report) -> None:
        await self.update(
            case_id, status=JobStatus.SUCCEEDED, report=report, finished_at=datetime.now(UTC)
        )

    async def fail(self, case_id: str, detail: str) -> None:
        await self.update(
            case_id, status=JobStatus.FAILED, error=detail, finished_at=datetime.now(UTC)
        )
```

Both terminal transitions stamp `finished_at`, which `_evict` later sorts on.

## The two internals that carry the design

```python
    def _publish(self, live: _Live, **fields: object) -> None:
        live.record = live.record.model_copy(update=fields)

        woken, live.changed = live.changed, asyncio.Event()
        woken.set()
```

Six words of code and the two least obvious decisions in the codebase.

**`model_copy(update=...)` instead of mutation.** The record is *replaced* with an updated
copy. A watcher that was handed the previous record still holds exactly what it was shown —
it cannot observe a record changing mid-render. Immutable snapshots are why "every push is
a complete snapshot" is actually true and not just a slogan.

```python
>>> old = CaseRecord(case_id='case-demo', document_count=3)
>>> new = old.model_copy(update={'status': JobStatus.RUNNING})
>>> old.status, new.status
(<JobStatus.QUEUED: 'queued'>, <JobStatus.RUNNING: 'running'>)
>>> old is new
False
```

A watcher still holding `old` sees `queued` forever, which is correct: that is what it was
handed. It gets `new` on its next turn round the loop.

**Replacing the event instead of setting and clearing it.** The tuple assignment
`woken, live.changed = live.changed, asyncio.Event()` swaps in a fresh event and keeps the
old one, then sets the old one. Every watcher currently waiting is holding the old object,
so all of them wake.

The obvious alternative — `live.changed.set()` then `live.changed.clear()` — is broken with
more than one watcher: whichever wakes first clears the flag and the rest sleep through the
update. Replacing has no window in which that can happen, because a waiter's own event is
never reused.

```python
    def _evict(self) -> None:
        finished = sorted(
            (live.record.finished_at, case_id)
            for case_id, live in self._cases.items()
            if live.record.finished_at is not None
        )
        for _, case_id in finished[: len(self._cases) - MAX_CASES]:
            del self._cases[case_id]
```

Sorting `(finished_at, case_id)` tuples orders oldest first, so the slice takes the ones
that have been finished longest.

The `if ... is not None` filter means **running cases are never evicted**, only finished
ones. Called from `create` rather than by a background sweeper: eviction only matters when
the store is being used, and a sweeper is one more task to own and cancel.

> **A bug, not a rough edge.** `[: len(self._cases) - MAX_CASES]` is meant to read "how
> many need to go, and nothing when that is zero or less". It does not. When the store
> holds fewer than `MAX_CASES` the count is *negative*, and a negative stop index does not
> mean "empty" — it counts backwards from the end of `finished`, so `finished[:-2]` keeps
> the newest two and deletes everything before them.
>
> ```python
> >>> store = CaseStore()                     # 30 cases, 25 of them finished
> >>> len(store._cases)                       # MAX_CASES is 50, so nothing should go
> 30
> >>> await store.create(document_count=2, case_id='case-new')
> >>> len(store._cases)                       # expected 31
> 26
> ```
>
> Five finished cases deleted with the store at 60% of its limit. The slice is empty only
> while `len(finished) + len(self._cases) <= MAX_CASES`; past that it evicts early and in
> bulk, and a caller that comes back for its report gets a 404.
>
> The fix is to clamp the count rather than trust the slice:
>
> ```python
> for _, case_id in finished[: max(0, len(self._cases) - MAX_CASES)]:
> ```

## The runner

```python
    async def submit(self, request: CaseRequest) -> CaseRecord:
        record = await self.store.create(len(request.documents), request.case_id)
        case = request.to_case_input(record.case_id)

        task = asyncio.create_task(self._run(case), name=f'case:{case.case_id}')
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return record
```

The handoff. `create_task` schedules `_run` on the event loop and returns *immediately* —
which is what lets the route reply 202 while the work continues.

```python
>>> await runner.submit(request)        # returns in microseconds
CaseRecord(case_id='case-demo', status=<JobStatus.QUEUED: 'queued'>, document_count=3,
           events=[], report=None, error=None, ...)
```

`status` is still `queued` and `events` is still empty — the run has been scheduled but has
not had the loop yet. Everything the caller learns after this comes from polling or the
socket.

`self._tasks.add(task)` is not bookkeeping, it is required. `asyncio` holds only a **weak**
reference to running tasks, so a task nobody else references can be garbage-collected
mid-run. Keeping a strong reference until it finishes is the documented fix;
`add_done_callback(self._tasks.discard)` is what removes it afterwards so the set does not
grow forever.

```python
    async def aclose(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
```

`tuple(self._tasks)` takes a snapshot first, because cancelling can trigger the done
callback that mutates the set you are iterating. `return_exceptions=True` means one task
failing during shutdown does not stop the others being awaited.

```python
    async def _run(self, case: CaseInput) -> None:
        state = RunState(case_id=case.case_id)
        delivered = 0

        async def drain() -> None:
            nonlocal delivered
            while delivered < len(state.events):
                event = state.events[delivered]
                delivered += 1
                await self.store.append_event(case.case_id, event)
```

`drain` forwards everything recorded since the last call. `delivered` is a high-water mark
and `nonlocal` lets the inner function rebind the outer variable (without it, `delivered +=
1` would create a new local and the count would reset every call).

Reading by index rather than iterating is deliberate: the list is still being appended to
by the running branches, and indexing up to a length captured at the top of each loop
iteration is safe against that.

```python
        try:
            await self.store.update(case.case_id, status=JobStatus.RUNNING)

            async with case_graph.iter(
                state=state, deps=RunDeps(stage_delay=STAGE_DELAY), inputs=case
            ) as run:
                async for _ in run:
                    await drain()
                await drain()  # Whatever the final node recorded on its way out.

            report: Report | None = run.output
```

**`iter()` rather than `run()`** is the whole reason the middle panel of the page works.
`run()` would give the report and nothing else; `iter()` hands back control between nodes,
and each time it does, `drain()` forwards whatever the run has recorded.

The steps know nothing about this. They append to `state.events`; something outside decides
that appending means "tell the browser".

The second `drain()` after the loop is a guard for whatever the final node recorded on its
way out. On *this* graph it forwards nothing: `__end__` is itself a node boundary, so
`compare`'s event is already picked up by the last in-loop `drain()`. It costs one
comparison and removes a whole class of "the last event never arrived" bug from any future
graph shape where the loop would end sooner. [`API_FLOW.md`](API_FLOW.md) has the
iteration-by-iteration trace.

```python
        except asyncio.CancelledError:
            raise
        except CaseNotFound:
            return
        except Exception as exc:  # noqa: BLE001 - a case must always reach a terminal state
            await self.store.fail(case.case_id, f'{type(exc).__name__}: {exc}')
```

Three clauses, three different meanings:

- **`CancelledError` is re-raised.** Swallowing it would break cancellation — the task
  would refuse to die. It is not an error; it is an instruction.
- **`CaseNotFound` returns quietly.** Someone deleted the case mid-run. There is nothing
  left to report an outcome to, and that is exactly what the caller asked for.
- **Everything else is recorded as a failure.** Deliberately broad, and the `noqa` says so:
  a case that never reaches a terminal state is a case somebody polls forever. Better a
  recorded `failed` than a record stuck at `running` for eternity.

---

# 5 · `events.py` — the Socket.IO layer

Turns `store.watch()` into something a browser can subscribe to. Three events go out —
`case`, `done`, `error` — and two come in: `subscribe`, `unsubscribe`.

```python
def build_socket_app(app: FastAPI) -> socketio.ASGIApp:
    server = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
    streams: dict[str, dict[str, asyncio.Task[None]]] = {}
```

A **function** rather than module-level code, because it needs the `app` to read the runner
off later. Everything below is a closure over `server` and `streams`.

`streams` is `{session id: {case id: task}}` — two levels, because one browser tab may
watch several cases at once, and a disconnect has to cancel all of that tab's tasks
without touching anyone else's.

`cors_allowed_origins='*'` is fine for a prototype served from its own origin; a real
deployment names its origins.

```python
    async def pump(sid: str, case_id: str) -> None:
        runner: CaseRunner = app.state.runner
        try:
            async for record in runner.store.watch(case_id):
                await server.emit('case', record.model_dump(mode='json'), to=sid)
            await server.emit('done', {'case_id': case_id}, to=sid)
        except CaseNotFound as exc:
            await server.emit('error', {'detail': str(exc)}, to=sid)
        except asyncio.CancelledError:
            raise
```

Six lines, and they are the entire streaming protocol.

`app.state.runner` is read **here, at event time**, not captured when `build_socket_app`
ran — because the lifespan handler creates the runner *after* this app has been assembled.
Capturing it at build time would capture nothing.

`model_dump(mode='json')` converts the Pydantic model to JSON-safe primitives — crucially
turning `datetime` into an ISO string, which plain `model_dump()` would leave as a
`datetime` object that the JSON encoder then rejects.

```python
>>> record.model_dump()['submitted_at']
datetime.datetime(2026, 9, 17, 10, 13, 57, 390861, tzinfo=datetime.timezone.utc)
>>> record.model_dump(mode='json')['submitted_at']
'2026-09-17T10:13:57.390861Z'

>>> json.dumps(record.model_dump())
TypeError: Object of type datetime is not JSON serializable
>>> json.dumps(record.model_dump(mode='json'))     # fine
```

The FastAPI routes never need this because returning a model lets FastAPI serialise it.
Socket.IO is handed a plain dict, so the conversion has to happen here.

The `async for` ends on its own when the record is terminal, which is when `done` goes out.
No flag, no check — the generator's own ending *is* the signal.

`CancelledError` re-raised for the same reason as in the runner: the client went away, and
the task must actually stop.

```python
    @server.event
    async def subscribe(sid: str, data: Any) -> None:
        case_id = data.get('case_id') if isinstance(data, dict) else None
        if not isinstance(case_id, str) or not case_id:
            await server.emit('error', {'detail': "expected {'case_id': '...'}"}, to=sid)
            return

        stop(sid, case_id)
        task = asyncio.create_task(pump(sid, case_id), name=f'stream:{sid}:{case_id}')
        streams.setdefault(sid, {})[case_id] = task
        task.add_done_callback(lambda _: streams.get(sid, {}).pop(case_id, None))
```

`@server.event` registers the handler under the function's own name, so this handles the
client's `subscribe` event.

`data` is `Any` because it is whatever the client sent — a socket payload gets none of the
validation a FastAPI route body does. Hence the two-step check: is it a dict, and is
`case_id` a non-empty string. Untrusted input, checked before use.

What each kind of payload gets back:

| client emits | server emits |
|---|---|
| `subscribe {"case_id": "case-demo"}` | `case` …, `case` …, `done {"case_id": "case-demo"}` |
| `subscribe {"case_id": "nope"}` | `error {"detail": "no case 'nope'"}` |
| `subscribe "case-demo"` (not a dict) | `error {"detail": "expected {'case_id': '...'}"}` |
| `subscribe {"case_id": ""}` | `error {"detail": "expected {'case_id': '...'}"}` |
| `subscribe {}` | `error {"detail": "expected {'case_id': '...'}"}` |

Note that the missing-case error comes from `pump`, not from here — the id looked fine, and
only `store.watch` could know there was nothing behind it.

`stop(sid, case_id)` first means **re-subscribing replaces the stream rather than doubling
it**. Without it, a client that subscribes twice gets every event twice, forever.

The `add_done_callback` removes the finished task from the table, so a long-lived
connection that watches many cases does not accumulate dead entries. `streams.get(sid, {})`
rather than `streams[sid]` because the session may already be gone by then.

```python
    @server.event
    async def disconnect(sid: str) -> None:
        for task in streams.pop(sid, {}).values():
            task.cancel()
```

`pop` removes and returns in one step, so the session's entry is gone before the loop
starts. Without this, a closed tab would leave tasks emitting into a socket nobody is
reading — for as long as the case runs.

```python
    return socketio.ASGIApp(server, socketio_path='')
```

`socketio_path=''` because Starlette strips the mount prefix before the sub-app sees the
request. The app is mounted at `/socket.io`, so by the time the request arrives here the
path left to match is empty — the default `'socket.io'` would look for
`/socket.io/socket.io`.

---

# 6 · `main.py` — the wiring

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    app.state.runner = CaseRunner()
    try:
        yield
    finally:
        await app.state.runner.aclose()
```

Everything before `yield` runs at startup, everything after at shutdown. The runner is
built **once per process** and hung on `app.state`, which is FastAPI's namespace for
exactly this.

`try/finally` guarantees `aclose()` even if the app is shutting down because of an error,
so in-flight tasks are always cancelled rather than left orphaned.

```python
@app.exception_handler(CaseNotFound)
async def _case_not_found(request: Request, exc: CaseNotFound) -> JSONResponse:
    return JSONResponse({'detail': str(exc)}, status_code=status.HTTP_404_NOT_FOUND)
```

One handler, registered once, and every route that touches a missing case returns a clean
404 — `get_case`, `forget_case`, and anything added later. `CaseExists` has a twin handler
returning 409. The alternative is a `try/except`
in each route, repeated and easy to forget. This is why `CaseNotFound` was worth defining
as its own class.

```jsonc
// GET /v1/cases/nope
404  {"detail": "no case 'nope'"}

// POST /v1/cases with a case_id that is already taken
409  {"detail": "case 'case-demo' already exists"}
```

Both bodies come from the exceptions' own `__str__`, which is why they read as sentences
rather than as `KeyError('nope')`.

```python
@app.post('/v1/cases', status_code=status.HTTP_202_ACCEPTED, tags=['cases'])
async def submit(request: CaseRequest) -> CaseRecord:
    runner: CaseRunner = app.state.runner
    return await runner.submit(request)
```

**202 Accepted, not 201 Created** — the correct code for "I have taken this and will work
on it, but it is not done". The whole async design is announced by that number.

The type annotations are load-bearing, not decoration. `request: CaseRequest` tells FastAPI
to parse and validate the body (a bad one never reaches this function); `-> CaseRecord`
tells it how to serialise the response and documents it at `/docs`.

Every route, with what it actually answers:

| request | status | body |
|---|---|---|
| `POST /v1/cases` `{"documents": [...3 docs]}` | `202` | the `CaseRecord`, `status: "queued"`, `events: []` |
| `POST /v1/cases` with 1 document | `422` | `{"detail": [{"type": "too_short", ...}]}` |
| `POST /v1/cases` with a taken `case_id` | `409` | `{"detail": "case 'case-demo' already exists"}` |
| `GET /v1/cases/case-demo` mid-run | `200` | same record, `status: "running"`, `report: null` |
| `GET /v1/cases/case-demo` after | `200` | same record, `status: "succeeded"`, `report: {...}` |
| `GET /v1/cases/nope` | `404` | `{"detail": "no case 'nope'"}` |
| `DELETE /v1/cases/case-demo` | `204` | empty |
| `GET /health` | `200` | `{"status": "ok", "stage_delay_seconds": 1.0}` |
| `GET /v1/graph` | `200` | the Mermaid source, as `text/plain` |

Four of those rows are the *same shape* — `CaseRecord`, at different moments. That is the
point of the design: the client has one renderer and no state machine.

```python
@app.get('/v1/graph', tags=['system'], response_class=PlainTextResponse)
async def graph() -> str:
    return render_mermaid(title='Document mismatch pipeline')
```

`response_class=PlainTextResponse` overrides the JSON default, so the Mermaid source comes
back as text rather than as a JSON-quoted string with `\n` everywhere.

```python
app.mount(MOUNT_PATH, build_socket_app(app))
app.mount('/', StaticFiles(directory=FRONTEND, html=True), name='frontend')
```

**Order matters, and this is the subtlest line in the file.** A mount at `/` matches
everything, so it must come last — the API keeps its paths, and the page gets whatever is
left. Move it above the routes and every API call would return the HTML page instead.

Socket.IO is *mounted*, not *included*, because it is a separate ASGI application rather
than a set of routes. One consequence worth knowing: FastAPI middleware does not see
requests that land inside a mount, which is why the socket server does its own CORS check.

`html=True` makes `StaticFiles` serve `index.html` for `/`.

```python
def main() -> None:
    import uvicorn

    port = int(os.getenv('TFD_PORT', '8000'))
    print(f'Document Mismatch Detector -> http://localhost:{port}')
    uvicorn.run('app.main:app', host='127.0.0.1', port=port, reload=True)


if __name__ == '__main__':
    main()
```

`import uvicorn` **inside** the function, not at the top: the server is only needed when
starting one. A test that imports `app.main` to get the FastAPI object should not pay for
importing a web server.

`uvicorn.run('app.main:app', ...)` passes the app as an **import string** rather than the
object. `reload=True` requires that — the reloader restarts a fresh process and needs to
know where to import the app from, which it cannot do from an object reference.

---

# 7 · `sample.py`

A list of three dicts, served by `GET /v1/sample` so the page has something to run. The
planted disagreements: `Total Amount` (50,000 vs 51,000), `Delivery Date` (2026-03-01 vs
2026-03-04), and `Seller` (`Acme Trading` vs `Acme Trading Ltd`). All three names are on
the `WATCHED` list, so all three come back critical and the verdict is `blocked`.

`Order No`, `Buyer`, `Currency` and `Ship To` agree everywhere. `Invoice No` and `Carrier`
appear on one document each, so they are skipped.

---

# Patterns worth naming

Five ideas that recur, so you recognise them the second time:

**1 · Snapshots, not deltas.** `CaseRecord` is always complete, and `_publish` replaces it
rather than mutating it. Everything else — late subscribers working, reconnects needing no
cursor, the client having no state machine — is downstream of that one choice.

**2 · Sync logic, async plumbing.** `detector.py` has no `async` anywhere. All the
concurrency lives in `graph.py` and `cases.py`. You can test the logic without an event
loop, and reason about the concurrency without the logic in the way.

**3 · Everything from the context.** Graph steps take `ctx.inputs`, `ctx.state`,
`ctx.deps` and nothing from module scope. Swapping `RunDeps` swaps the world the step runs
in.

**4 · A case must always end.** The runner's broad `except` exists to guarantee it. A run
that fails in an unexpected way still reaches `failed`; the only thing worse than a wrong
answer is no answer and a client polling forever.

**5 · Deterministic fakes.** `crc32` rather than `random`, both in the stub replies and in
the latency jitter. A demo that says something different on every restart is hard to
trust and harder to debug.

## Where a real model plugs in

Every place that would call one, in one list:

| now | would become |
|---|---|
| `read_document()` — regex over `KEY: VALUE` lines | a model extracting fields under a schema |
| `default_reply(text, kind='read')` — canned note | the model's own remarks on the document |
| `compare_documents()` — normalise and compare | keep it: a model should not be doing arithmetic |
| `default_reply(counted, kind='summary')` | the model's written summary |

The shape does not change. `read` is already an `async` step with a latency, `RunDeps` is
already where a client would live, and the pipeline already streams progress — because the
graph was built around the assumption that the steps are slow.
