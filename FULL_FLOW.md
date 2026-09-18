# Full flow

The whole system as one sequence diagram — from the browser loading the page to the verdict
appearing on screen. The frontend is in it: `app.js`, the WebSocket client, and every DOM
update.

- [`API_FLOW.md`](API_FLOW.md) — the same system as 39 small diagrams, one per function.
- [`WALKTHROUGH.md`](WALKTHROUGH.md) — what each line of code means.

## The short version

1. The page asks for sample documents and shows them in editable boxes.
2. You click **Compare**. The page POSTs and gets a `case_id` back in milliseconds — no
   analysis has happened yet.
3. The page renders that empty record straight away, *then* opens a WebSocket and
   subscribes.
4. The run happens on a background task. Every time anything changes, the store wakes the
   subscription and a **complete** record is pushed.
5. The page re-renders from scratch on every push. There is no state machine in the
   browser — `render(record)` is the whole client.
6. When the record is terminal, the stream ends by itself and the socket closes.

The one idea everything rests on: **every push is the whole record, never a delta.** That
is why connecting late loses nothing and why the page can be this simple.

## Who is who

| participant | is |
|---|---|
| `app.js` | the page — `submit`, `watch`, `render`, `renderReport` |
| `socketio.js` | a tiny hand-rolled Socket.IO client, no library |
| `main.py` | FastAPI — the routes |
| `CaseRunner` | accepts a case, owns the background task |
| `CaseStore` | holds records, wakes watchers |
| `_run task` | drives the graph, forwards events |
| `case_graph` | the pipeline — ingest, read, compare |
| `detector.py` | the pure logic — `read_document`, `compare_documents` |
| `events.pump` | one task per subscription, pushing frames |

---

## The whole thing, end to end

Five acts, colour-banded. Read it top to bottom.

```mermaid
%%{init:{'theme':'base','themeVariables':{'darkMode':true,'background':'#282a36','mainBkg':'#44475a','primaryColor':'#44475a','primaryTextColor':'#f8f8f2','primaryBorderColor':'#bd93f9','lineColor':'#6272a4','textColor':'#f8f8f2','nodeBorder':'#bd93f9','clusterBkg':'#21222c','clusterBorder':'#6272a4','edgeLabelBackground':'#282a36','actorBkg':'#44475a','actorBorder':'#bd93f9','actorTextColor':'#f8f8f2','actorLineColor':'#6272a4','signalColor':'#f8f8f2','signalTextColor':'#f8f8f2','noteBkgColor':'#414458','noteBorderColor':'#ffb86c','noteTextColor':'#f8f8f2','activationBkgColor':'#6272a4','activationBorderColor':'#bd93f9','labelBoxBkgColor':'#44475a','labelBoxBorderColor':'#bd93f9','labelTextColor':'#f8f8f2','loopTextColor':'#f8f8f2','sequenceNumberColor':'#282a36'}}}%%
sequenceDiagram
    autonumber
    actor User
    participant Page as app.js
    participant WS as socketio.js
    participant API as main.py
    participant Runner as CaseRunner
    participant Store as CaseStore
    participant Task as _run task
    participant Graph as case_graph
    participant Detect as detector.py
    participant Pump as events.pump

    rect rgba(98, 114, 164, 0.18)
    Note over User,Page: ACT 1 — the page loads
    Page->>API: GET /v1/sample
    API-->>Page: 3 documents of name and text
    Page->>Page: addDocument(name, text) x3
    User->>Page: edits the boxes, clicks Compare
    end

    rect rgba(189, 147, 249, 0.18)
    Note over Page,Runner: ACT 2 — submit, and get a case id back
    Page->>Page: collect() — read the boxes, drop empty ones
    alt fewer than 2 documents
        Page->>Page: show the error box, stop here
    end
    Page->>Page: $('submit').disabled = true
    Page->>API: POST /v1/cases with the documents
    API->>API: pydantic validates CaseRequest
    alt body invalid
        API-->>Page: 422 with detail
        Page->>Page: show the error box with the detail
    end
    Note over Page: finally — $('submit').disabled = false<br/>runs on both paths
    API->>Runner: CaseRunner.submit(request)
    Runner->>Store: CaseStore.create(document_count, case_id)
    Store->>Store: _evict(), then refuse a duplicate id
    Store-->>Runner: CaseRecord status=queued
    Runner->>Runner: to_case_input() assigns doc-1 doc-2 doc-3
    Runner->>Task: asyncio.create_task(_run)
    Runner-->>API: the queued record
    API-->>Page: 202 Accepted
    end

    rect rgba(80, 250, 123, 0.15)
    Note over Page,WS: ACT 3 — start watching, before any work has happened
    Page->>Page: watch(record)
    Page->>Page: socket?.close() — drop any previous case
    Page->>Page: hide idle, show live, hide the report panel
    Page->>Page: render(record) — pill says queued, trail empty
    Page->>Page: scrollIntoView so the run is on screen
    Page->>WS: connect(location.origin)
    WS->>API: WebSocket /socket.io/?EIO=4&transport=websocket
    API-->>WS: 0 engine open
    WS->>API: 40 join the namespace
    API-->>WS: 40 namespace joined, ready = true
    Page->>WS: emit subscribe with case_id
    Note over WS: emitted before ready is queued,<br/>then flushed on handshake
    WS->>API: 42 subscribe with case_id
    API->>Pump: asyncio.create_task(pump(sid, case_id))
    Pump->>Store: CaseStore.watch(case_id)
    Store-->>Pump: CaseRecord queued, yielded immediately
    Pump-->>WS: 42 case with the whole record
    WS-->>Page: on case
    Page->>Page: render(record)
    end

    rect rgba(255, 184, 108, 0.18)
    Note over Task,Detect: ACT 4 — the run, with the page watching
    Note over WS,Page: every case frame below still goes<br/>Pump to socketio.js to app.js — the hop is<br/>drawn once above and elided from here on
    Task->>Store: update(status=RUNNING)
    Store->>Store: _publish, model_copy, swap the Event, wake watchers
    Store-->>Pump: CaseRecord running
    Pump-->>Page: case frame 2, status running
    Page->>Page: render — pill turns running

    Task->>Graph: case_graph.iter(state, deps, inputs)
    Graph->>Graph: ingest, sleep, RunState.record
    Task->>Store: append_event(ingest)
    Store-->>Pump: CaseRecord events=1
    Pump-->>Page: case frame 3
    Page->>Page: render — one trail line appears

    par read doc-3 finishes at 1.139s
        Graph->>Detect: read_document(doc-3)
        Detect-->>Graph: ParsedDocument
    and read doc-1 at 1.371s
        Graph->>Detect: read_document(doc-1)
        Detect-->>Graph: ParsedDocument
    and read doc-2 at 1.644s
        Graph->>Detect: read_document(doc-2)
        Detect-->>Graph: ParsedDocument
    end

    Task->>Store: append_event(read doc-3)
    Pump-->>Page: case frame 4, events=2
    Task->>Store: append_event(read doc-1)
    Pump-->>Page: case frame 5, events=3
    Task->>Store: append_event(read doc-2)
    Pump-->>Page: case frame 6, events=4
    Note over Page: trail fills in COMPLETION order:<br/>doc-3, doc-1, doc-2

    Graph->>Detect: compare_documents(sorted by doc_id)
    Detect-->>Graph: Report, verdict blocked
    Task->>Store: append_event(compare)
    Pump-->>Page: case frame 7, events=5
    end

    rect rgba(139, 233, 253, 0.15)
    Note over Task,Page: ACT 5 — the answer
    Graph-->>Task: run.output is the Report
    Task->>Store: CaseStore.succeed(case_id, report)
    Store->>Store: status=succeeded, report, finished_at
    Store-->>Pump: CaseRecord succeeded, is_terminal True
    Pump-->>Page: case frame 8, with the report
    Page->>Page: render, then renderReport(report)
    Page->>User: verdict Blocked, 3 findings, matched fields, documents

    Note over Store,Pump: watch() sees is_terminal and returns
    Pump-->>WS: 42 done
    WS-->>Page: on done
    Page->>WS: socket.close(), sends 41
    end
```

Two things to notice in Act 3.

**The page renders before it connects.** `watch(record)` calls `render(record)` with the
202 response, so the status pill says `queued` and the panel is on screen before the socket
exists. If the connection were to fail, you would still see the case.

**Subscribing late costs nothing.** By the time the socket is up, a document may already
have been read. `watch()` yields the *current* record immediately, so the page catches up
in one frame rather than missing what it slept through.

---

## Zoom 1 · How one status change reaches the screen

This is the loop the whole design is built around. It runs eight times per case.

```mermaid
%%{init:{'theme':'base','themeVariables':{'darkMode':true,'background':'#282a36','mainBkg':'#44475a','primaryColor':'#44475a','primaryTextColor':'#f8f8f2','primaryBorderColor':'#bd93f9','lineColor':'#6272a4','textColor':'#f8f8f2','nodeBorder':'#bd93f9','clusterBkg':'#21222c','clusterBorder':'#6272a4','edgeLabelBackground':'#282a36','actorBkg':'#44475a','actorBorder':'#bd93f9','actorTextColor':'#f8f8f2','actorLineColor':'#6272a4','signalColor':'#f8f8f2','signalTextColor':'#f8f8f2','noteBkgColor':'#414458','noteBorderColor':'#ffb86c','noteTextColor':'#f8f8f2','activationBkgColor':'#6272a4','activationBorderColor':'#bd93f9','labelBoxBkgColor':'#44475a','labelBoxBorderColor':'#bd93f9','labelTextColor':'#f8f8f2','loopTextColor':'#f8f8f2','sequenceNumberColor':'#282a36'}}}%%
sequenceDiagram
    autonumber
    participant Task as _run task
    participant Store as CaseStore
    participant Watch as watch() generator
    participant Pump as events.pump
    participant WS as socketio.js
    participant Page as app.js
    participant DOM

    Note over Watch: parked on await waiter.wait()

    Task->>Store: append_event(case_id, event)
    Store->>Store: _publish(live, events=[*old, new])
    Store->>Store: live.record = live.record.model_copy(update=fields)
    Store->>Store: woken, live.changed = live.changed, asyncio.Event()
    Store->>Watch: woken.set()

    Watch->>Watch: wakes, loops, takes the lock
    Watch->>Watch: grabs the NEW Event and the NEW record
    Watch-->>Pump: yield record — the whole snapshot

    Pump->>Pump: record.model_dump(mode='json')
    Pump-->>WS: 42["case", {...}]
    WS->>WS: JSON.parse, look up the "case" handler
    WS-->>Page: render(record)

    Page->>DOM: case-id and status pill class<br/>raw JSON pretty-printed<br/>trail rebuilt from record.events
    opt record.error is set
        Page->>DOM: the error box is shown
    end
    opt record.report is set
        Page->>DOM: renderReport — verdict, summary,<br/>mismatches, matched, documents
    end

    Note over Watch: back to await waiter.wait()<br/>on the NEW Event
```

The two subtle steps are 4 and 5.

**Step 4 swaps the Event rather than setting and clearing it.** Every watcher parked on the
old Event wakes. The obvious alternative — `set()` then `clear()` — is broken with more
than one browser tab open: whichever wakes first clears the flag and the rest sleep through
the update.

**Step 7 grabs the new Event *before* yielding.** If a change lands while the record is
being handed over, it sets the Event the watcher is about to wait on, so the wait returns
immediately instead of missing it.

---

## Zoom 2 · The WebSocket handshake

`socketio.js` is 60 lines and implements just enough of the protocol. Worth its own diagram
because of the ordering problem it solves.

```mermaid
%%{init:{'theme':'base','themeVariables':{'darkMode':true,'background':'#282a36','mainBkg':'#44475a','primaryColor':'#44475a','primaryTextColor':'#f8f8f2','primaryBorderColor':'#bd93f9','lineColor':'#6272a4','textColor':'#f8f8f2','nodeBorder':'#bd93f9','clusterBkg':'#21222c','clusterBorder':'#6272a4','edgeLabelBackground':'#282a36','actorBkg':'#44475a','actorBorder':'#bd93f9','actorTextColor':'#f8f8f2','actorLineColor':'#6272a4','signalColor':'#f8f8f2','signalTextColor':'#f8f8f2','noteBkgColor':'#414458','noteBorderColor':'#ffb86c','noteTextColor':'#f8f8f2','activationBkgColor':'#6272a4','activationBorderColor':'#bd93f9','labelBoxBkgColor':'#44475a','labelBoxBorderColor':'#bd93f9','labelTextColor':'#f8f8f2','loopTextColor':'#f8f8f2','sequenceNumberColor':'#282a36'}}}%%
sequenceDiagram
    autonumber
    participant Page as app.js
    participant WS as socketio.js
    participant Server as Socket.IO server

    Page->>WS: connect(location.origin)
    WS->>Server: WebSocket /socket.io/?EIO=4&transport=websocket

    Page->>WS: emit('subscribe', {case_id})
    Note over WS: ready is false, so the frame<br/>is pushed onto queued[]

    Server-->>WS: "0" engine open
    WS->>Server: "40" join the default namespace
    Server-->>WS: "40" namespace joined
    WS->>WS: ready = true, queued.splice(0).forEach(send)
    WS->>Server: 42["subscribe",{"case_id":"case-demo"}]

    loop for the life of the connection
        Server-->>WS: "2" ping
        WS->>Server: "3" pong
    end

    Server-->>WS: 42["case",{...}]
    WS-->>Page: handlers.get("case") called with the payload

    Server-->>WS: 42["done",{"case_id":"case-demo"}]
    WS-->>Page: handlers.get("done")
    Page->>WS: socket.close()
    WS->>Server: "41" leave the namespace, then close
```

`app.js` calls `emit('subscribe', ...)` immediately after `connect()`, long before the
namespace handshake finishes. The client queues the frame and flushes it on `ready` — so
the page never has to know about handshake timing.

The frame prefixes: Engine.IO uses the first character (`0` open, `1` close, `2` ping,
`3` pong, `4` message) and Socket.IO adds a second inside a `4` (`0` connect, `1`
disconnect, `2` event). So `42["case",{...}]` reads as *message · event · named "case"*.

---

## What the page shows, frame by frame

Eight `case` frames, then `done`. The status pill and the trail are driven entirely by this.

| # | `status` | `events` | what you see change |
|---|---|---|---|
| 1 | `queued` | 0 | pill grey, trail empty, panel scrolls into view |
| 2 | `running` | 0 | pill turns to running |
| 3 | `running` | 1 | first trail line — `ingest · 3 documents received` |
| 4 | `running` | 2 | `read · DELIVERY NOTE — 7 fields` |
| 5 | `running` | 3 | `read · PURCHASE ORDER — 7 fields` |
| 6 | `running` | 4 | `read · INVOICE — 7 fields` |
| 7 | `running` | 5 | `compare · 3 mismatches, verdict blocked` |
| 8 | `succeeded` | 5 | pill green, **report panel appears** |

Frames 4, 5 and 6 arrive in *completion* order — doc-3, doc-1, doc-2 — not the order you
submitted them. That reordering is the fan-out, visible from the outside.

## What `render` touches

Every frame, from scratch. No diffing, no accumulation.

| element | set from |
|---|---|
| `#case-id` | `record.case_id` |
| `#status` | `record.status`, also as the pill's CSS class |
| `#raw` | the whole record, pretty-printed |
| `#trail` | rebuilt from `record.events` |
| `#error` | shown only if `record.error` |
| `#report-panel` | shown only once `record.report` exists |

And `renderReport`, once, on the final frame:

| element | set from |
|---|---|
| `#verdict` | `report.verdict`, mapped to a sentence |
| `#summary` | `report.summary` |
| `#mismatches` | one card per `Mismatch`, with every document's value |
| `#matched` | `report.matched_fields` joined |
| `#documents` | one card per `ParsedDocument` — title, note, fields |

`renderReport` builds a `doc_id → name` map first, because a finding cites `doc-2` but a
person wants to read *Invoice*.

## The end result

For the three sample documents:

```
verdict        blocked — important fields disagree
critical_count 3

Total Amount   Purchase order  50,000.00
               Invoice         51,000.00
Seller         Purchase order  Acme Trading
               Invoice         Acme Trading
               Delivery note   Acme Trading Ltd
Delivery Date  Purchase order  2026-03-01
               Delivery note   2026-03-04

matched        Buyer · Currency · Order No · Ship To
```

`Seller` lists three values although only two are distinct — doc-1 and doc-2 agreeing is
part of the evidence, not noise to be dropped.
