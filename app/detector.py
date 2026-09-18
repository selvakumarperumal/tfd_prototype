"""Reading documents and comparing them. The whole of the domain logic lives here.

Two steps, and both are deliberately simple:

    read(...)     one document's `KEY: VALUE` lines -> a dict of fields
    compare(...)  every field that appears in two or more documents -> agree, or don't

There is no rulebook. A field is "critical" only because its name looks like it is about
money, dates or parties (`WATCHED` below); everything else is a warning. That is enough
to make the pipeline do something visible, which is what a prototype is for.

`default_reply` is where a language model would go. The real service sends the document
text to one and gets back a classification, a structured extraction and a written
summary. Here it returns canned words, so the prototype runs with no key, no network and
no bill — and swapping it for a real call is a change in this file and nowhere else.
"""

from __future__ import annotations

import re
from zlib import crc32

from app.models import (
    Mismatch,
    ParsedDocument,
    Report,
    Severity,
    Value,
    Verdict,
)

# --- the stand-in for the model call -----------------------------------------

_POOLS: dict[str, tuple[str, ...]] = {
    'read': (
        'Read cleanly; the labelled fields came through without trouble.',
        'Layout was plain enough — every labelled line parsed on the first pass.',
        'Nothing unusual on this one; the headings were where you would expect them.',
        'Scanned it end to end and found the fields well formed.',
    ),
    'summary': (
        'Full detail is listed below, most serious first.',
        'The breakdown below shows which document said what.',
        'Each field below was compared on its own, and reported on its own.',
        'Anything only one document mentioned was left out of the comparison.',
    ),
}
"""What the stub can say, per stage. Real prose would come from the model."""


def default_reply(prompt: str, *, kind: str = 'read') -> str:
    """Return canned words instead of calling a language model.

    Deterministic on purpose: the same prompt always picks the same line, the way a real
    call at temperature zero would. `crc32` rather than `hash()` because Python salts
    string hashing per process, and a prototype that says something different on every
    restart is annoying to demo.
    """
    pool = _POOLS.get(kind, _POOLS['read'])
    return pool[crc32(prompt.encode()) % len(pool)]


# --- reading ------------------------------------------------------------------

FIELD_LINE = re.compile(r'^\s*([^:\n]{1,40}?)\s*:\s*(.+?)\s*$')
"""A labelled line. The 40-character ceiling on the label is what stops a sentence that
happens to contain a colon from being mistaken for a field."""


def read_document(doc_id: str, name: str, text: str) -> ParsedDocument:
    """Turn one document's text into a title and a bag of fields.

    Lines that are not `KEY: VALUE` are ignored — including table rows, which a real
    reader would handle and this one does not.
    """
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


# --- comparing ----------------------------------------------------------------

WATCHED = (
    'amount', 'total', 'price', 'value', 'sum',
    'date', 'expiry', 'shipped', 'due',
    'currency', 'quantity', 'qty', 'weight', 'count',
    'buyer', 'seller', 'shipper', 'consignee', 'beneficiary', 'applicant', 'party',
)
"""Field names that make a disagreement critical rather than merely worth a look."""


def _key(label: str) -> str:
    """The form two labels are matched on: case and spacing don't count."""
    return ' '.join(label.lower().split())


def _comparable(value: str) -> str:
    """The form two values are compared as.

    Case, spacing, thousands separators and a trailing full stop are noise — `USD
    51,000.00` and `usd 51000.00` are the same number written twice.
    """
    value = value.lower().strip().rstrip('.')
    value = re.sub(r'(?<=\d),(?=\d{3}\b)', '', value)
    return ' '.join(value.split())


def _severity(key: str) -> Severity:
    """Critical if the field name is about money, dates or who the parties are."""
    return Severity.CRITICAL if any(word in key for word in WATCHED) else Severity.WARNING


def compare_documents(documents: list[ParsedDocument]) -> Report:
    """Check every field that more than one document mentions, and report the answer.

    A field only one document carries is skipped rather than reported: documents
    legitimately hold different things, and flagging that would bury the real findings.
    """
    seen: dict[str, list[tuple[ParsedDocument, str, str]]] = {}
    for document in documents:
        for label, value in document.fields.items():
            seen.setdefault(_key(label), []).append((document, label, value))

    mismatches: list[Mismatch] = []
    matched: list[str] = []

    for key, entries in seen.items():
        if len(entries) < 2:
            continue

        label = entries[0][1]
        distinct = {_comparable(value) for _, _, value in entries}
        if len(distinct) == 1:
            matched.append(label)
            continue

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

    # Most severe first, then alphabetically, so the list is stable between runs.
    mismatches.sort(key=lambda m: (m.severity is not Severity.CRITICAL, m.field.lower()))
    matched.sort(key=str.lower)

    return Report(
        verdict=_verdict(mismatches),
        summary=_summary(mismatches, matched, documents),
        mismatches=mismatches,
        matched_fields=matched,
        documents=documents,
    )


def _verdict(mismatches: list[Mismatch]) -> Verdict:
    """One critical finding blocks; anything else disagreeing needs a human."""
    if any(m.severity is Severity.CRITICAL for m in mismatches):
        return Verdict.BLOCKED
    return Verdict.NEEDS_REVIEW if mismatches else Verdict.CLEAN


def _summary(mismatches: list[Mismatch], matched: list[str], documents: list[ParsedDocument]) -> str:
    """The counts, which are real, plus a closing line from the stub, which is not.

    Written this way on purpose: it shows exactly which half of a report a language model
    would be responsible for, and which half you would never hand to one.
    """
    critical = sum(1 for m in mismatches if m.severity is Severity.CRITICAL)
    counted = (
        f'Checked {len(documents)} documents on {len(matched) + len(mismatches)} shared fields: '
        f'{len(matched)} agreed, {len(mismatches)} did not ({critical} critical).'
    )
    return f'{counted} {default_reply(counted, kind="summary")}'
