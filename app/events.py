"""The Socket.IO half: watching a case as it runs.

Submitting a case and collecting its answer are separate acts, because the analysis takes
longer than a request. `GET /v1/cases/{id}` is the polling half; this is the pushing one.

    client                                server
      |  connect /socket.io                 |
      |  emit 'subscribe' {case_id}         |
      |                                     |  store.watch(case_id)
      |  <-- 'case'  {whole CaseRecord}     |  once immediately, then on every change
      |  <-- 'case'  {whole CaseRecord}     |
      |  <-- 'done'  {case_id}              |  the case reached a terminal status

Every `case` event carries the **whole record** — the same thing the GET returns — rather
than a delta. So there is no race between the POST returning and the socket opening, a
reconnect needs no cursor, and the client is a single `render(record)` with no state
machine in it.

One task per subscription does the pumping, tracked per session so that a disconnect
cancels it rather than leaving it emitting into a socket nobody is reading.
"""

from __future__ import annotations

import asyncio
from typing import Any

import socketio
from fastapi import FastAPI

from app.cases import CaseNotFound, CaseRunner

MOUNT_PATH = '/socket.io'


def build_socket_app(app: FastAPI) -> socketio.ASGIApp:
    """The Socket.IO server as an ASGI app, to mount on `app` at `MOUNT_PATH`.

    The runner is read off `app.state` at event time rather than captured now, because
    the lifespan handler builds it after this app has been assembled.
    """
    server = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
    streams: dict[str, dict[str, asyncio.Task[None]]] = {}
    """Running subscriptions, as `{session id: {case id: task}}`."""

    async def pump(sid: str, case_id: str) -> None:
        """Push every version of one case's record to one client, until it is terminal."""
        runner: CaseRunner = app.state.runner
        try:
            async for record in runner.store.watch(case_id):
                await server.emit('case', record.model_dump(mode='json'), to=sid)
            await server.emit('done', {'case_id': case_id}, to=sid)
        except CaseNotFound as exc:
            await server.emit('error', {'detail': str(exc)}, to=sid)
        except asyncio.CancelledError:
            raise  # The client went away, or the server is shutting down.

    @server.event
    async def subscribe(sid: str, data: Any) -> None:
        """Start streaming one case to this client."""
        case_id = data.get('case_id') if isinstance(data, dict) else None
        if not isinstance(case_id, str) or not case_id:
            await server.emit('error', {'detail': "expected {'case_id': '...'}"}, to=sid)
            return

        stop(sid, case_id)  # Re-subscribing replaces the stream rather than doubling it.
        task = asyncio.create_task(pump(sid, case_id), name=f'stream:{sid}:{case_id}')
        streams.setdefault(sid, {})[case_id] = task
        task.add_done_callback(lambda _: streams.get(sid, {}).pop(case_id, None))

    def stop(sid: str, case_id: str) -> None:
        """Cancel one subscription, if it is running."""
        if task := streams.get(sid, {}).pop(case_id, None):
            task.cancel()

    @server.event
    async def unsubscribe(sid: str, data: Any) -> None:
        """Stop streaming one case to this client."""
        case_id = data.get('case_id') if isinstance(data, dict) else None
        if isinstance(case_id, str) and case_id:
            stop(sid, case_id)

    @server.event
    async def disconnect(sid: str) -> None:
        """Cancel everything this client was watching."""
        for task in streams.pop(sid, {}).values():
            task.cancel()

    # `socketio_path=''` because Starlette strips the mount prefix before the sub-app sees
    # the request, so the path left to match is empty.
    return socketio.ASGIApp(server, socketio_path='')
