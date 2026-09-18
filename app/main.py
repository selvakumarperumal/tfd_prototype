"""The application: four routes, one socket, and the page they serve.

The shape follows from one fact — analysis takes longer than a request should. So
submitting a case and collecting its answer are two separate acts:

    POST   /v1/cases          the documents   -> 202 with a case id
    GET    /v1/cases/{id}     poll it
    DELETE /v1/cases/{id}     give up on it
    GET    /v1/sample         documents to try it with
    GET    /v1/graph          the pipeline as a Mermaid diagram

All three case routes answer with the same `CaseRecord`, and so does every Socket.IO
push. Watching one live is the other half, in `app/events.py`.

Run it with `uv run tfd` (or `uv run app/main.py`), then open http://localhost:8000.
Set `TFD_PORT` to use another port.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.cases import STAGE_DELAY, CaseExists, CaseNotFound, CaseRunner
from app.events import MOUNT_PATH, build_socket_app
from app.graph import render_mermaid
from app.models import CaseRecord, CaseRequest
from app.sample import SAMPLE

FRONTEND = Path(__file__).resolve().parent.parent / 'frontend'


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Build the runner once, and cancel whatever it still holds on the way out."""
    app.state.runner = CaseRunner()
    try:
        yield
    finally:
        await app.state.runner.aclose()


app = FastAPI(
    title='Document Mismatch Detector (prototype)',
    description=__doc__,
    version='0.1.0',
    lifespan=lifespan,
)


@app.exception_handler(CaseNotFound)
async def _case_not_found(request: Request, exc: CaseNotFound) -> JSONResponse:
    """A case that has expired or never existed is a 404, not a 500."""
    return JSONResponse({'detail': str(exc)}, status_code=status.HTTP_404_NOT_FOUND)


@app.exception_handler(CaseExists)
async def _case_exists(request: Request, exc: CaseExists) -> JSONResponse:
    """Re-submitting a case id that is already taken is a 409, not a 500."""
    return JSONResponse({'detail': str(exc)}, status_code=status.HTTP_409_CONFLICT)


@app.get('/health', tags=['system'])
async def health() -> dict[str, object]:
    """Liveness, and the one setting worth knowing at a glance."""
    return {'status': 'ok', 'stage_delay_seconds': STAGE_DELAY}


@app.get('/v1/graph', tags=['system'], response_class=PlainTextResponse)
async def graph() -> str:
    """The pipeline as a Mermaid diagram, drawn from the wiring that actually runs."""
    return render_mermaid(title='Document mismatch pipeline')


@app.get('/v1/sample', tags=['cases'])
async def sample() -> list[dict[str, str]]:
    """Documents that disagree on purpose, so the page has something to run."""
    return SAMPLE


@app.post('/v1/cases', status_code=status.HTTP_202_ACCEPTED, tags=['cases'])
async def submit(request: CaseRequest) -> CaseRecord:
    """Accept a set of documents and start comparing them.

    Answers immediately with a `queued` record; the run continues in the background.
    Follow it over Socket.IO, or by polling the route below.
    """
    runner: CaseRunner = app.state.runner
    return await runner.submit(request)


@app.get('/v1/cases/{case_id}', tags=['cases'])
async def get_case(case_id: str) -> CaseRecord:
    """Where a submitted case has got to, and its report once there is one."""
    runner: CaseRunner = app.state.runner
    return await runner.store.get(case_id)


@app.delete('/v1/cases/{case_id}', status_code=status.HTTP_204_NO_CONTENT, tags=['cases'])
async def forget_case(case_id: str) -> None:
    """Drop a case and release anyone watching it."""
    runner: CaseRunner = app.state.runner
    await runner.store.forget(case_id)


# Socket.IO is its own ASGI app rather than a route, so it is mounted rather than included.
app.mount(MOUNT_PATH, build_socket_app(app))

# Last, because a mount at '/' matches everything the routes above did not: the API keeps
# its paths and the page gets the rest.
app.mount('/', StaticFiles(directory=FRONTEND, html=True), name='frontend')


def main() -> None:
    """Start the server. This is what `uv run tfd` and `uv run app/main.py` call."""
    import uvicorn

    port = int(os.getenv('TFD_PORT', '8000'))
    print(f'Document Mismatch Detector -> http://localhost:{port}')
    uvicorn.run('app.main:app', host='127.0.0.1', port=port, reload=True)


if __name__ == '__main__':
    main()
