"""GANIL DAQ monitoring service.

Serves a JSON API plus a WebSocket status stream, and (once the frontend is
built) the single-page UI from the same origin and port.

There is no control surface here and there cannot be one: the only way to start
or stop a GANIL run is the run-control GUI inside the container, over X11. This
service reads the logs that GUI writes. Read-only is not a policy it enforces --
it is the shape of the thing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from . import elog
from .config import BROADCAST_INTERVAL, DEFAULT_RUN_LIMIT, STATIC_DIR
from .monitor import monitor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ganil")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    elog.install(monitor)
    monitor.start()
    if not elog.registered():
        log.info("not registered with elog yet; run boundaries will be watched but "
                 "not pushed. Register portal://ganil_daq_monitoring/api/elog.")
    try:
        yield
    finally:
        monitor.stop()


app = FastAPI(
    title="GANIL DAQ Monitoring",
    description=(
        "Run status for the GANIL acquisition: which run is going, the note it "
        "was started with, what it is writing and how fast, and every run "
        "already taken. Read-only — it watches the run-control logs and cannot "
        "start, stop or configure anything."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# The Vite dev server runs on another port; in production the UI is same-origin
# and these headers are simply unused.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=(
        r"http://(localhost|127\.0\.0\.1"
        r"|10\.\d+\.\d+\.\d+"
        r"|192\.168\.\d+\.\d+"
        r"|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+)(:\d+)?"
    ),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# The LILAK elog handshake, and the credentials it posts back; see app/elog.py.
app.include_router(elog.router)


@app.get("/api/health", tags=["meta"])
async def health() -> dict:
    snapshot = monitor.snapshot()
    return {
        "ok": snapshot["log_error"] is None,
        "daq_up": snapshot["daq_up"],
        "live_run": (snapshot["live"] or {}).get("run"),
        "attempts": snapshot["attempts"],
        "elog_registered": elog.registered(),
        "error": snapshot["log_error"],
    }


@app.get("/api/live", tags=["meta"], summary="Compact numbers for the portal's live mode")
async def live() -> dict:
    """The run going (or the last one), and the write rate with its recent
    history behind the number -- the same samples rate_bytes_per_s averages,
    turned into a rate per step so the shape is the rate's, not the file's."""
    snap = monitor.snapshot()
    live_run = snap.get("live")
    if live_run:
        items = [{"label": "run", "value": str(live_run.get("run")), "unit": "", "state": "running",
                  "sub": f"{int(live_run.get('elapsed_s') or 0) // 60} min"}]
        rate = snap.get("rate_bytes_per_s")
        if rate:
            items.append({"label": "rate", "value": f"{float(rate) / 1e6:.2f}", "unit": "MB/s", "state": "running",
                          "spark": monitor.rate_history(), "spark_window": "run"})
    else:
        last = next(iter(monitor.attempts(limit=1)), None)
        items = [{"label": "last run", "value": str(getattr(last, "run", None) or "—"), "unit": "",
                  "state": "idle" if snap.get("daq_up") else "down", "sub": "DAQ up" if snap.get("daq_up") else "DAQ down"}]
    return {"ok": True, "items": items}


@app.get("/api/status", tags=["run"], summary="The run going now, and the DAQ's components")
async def status() -> dict:
    return monitor.snapshot()


@app.get("/api/runs", tags=["run"], summary="Every run attempt, newest first")
async def runs(limit: int = DEFAULT_RUN_LIMIT) -> dict:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    attempts = monitor.attempts(limit=max(1, min(limit, 5000)))
    return {
        "attempts": [a.as_dict(now) for a in attempts],
        "orphan_files": [f.as_dict() for f in monitor.orphans()],
    }


@app.get("/api/errors", tags=["run"], summary="Recent run-control errors, newest first")
async def errors(limit: int = 50) -> dict:
    return {"errors": [{"time": t.isoformat(), "message": m}
                       for t, m in monitor.errors(max(1, min(limit, 500)))]}


@app.websocket("/ws/status")
async def status_socket(socket: WebSocket) -> None:
    """Push the live view. Nothing is read from the browser: there is nothing
    it could ask this service to do."""
    await socket.accept()
    try:
        while True:
            await socket.send_json(monitor.snapshot())
            await asyncio.sleep(BROADCAST_INTERVAL)
    except (WebSocketDisconnect, RuntimeError):
        return
    except Exception:
        log.exception("status socket failed")
        with contextlib.suppress(Exception):
            await socket.close()


class UiFiles(StaticFiles):
    """Serve the built UI, revalidating the entry point on every load.

    Vite fingerprints the asset filenames, so those can be cached hard. The HTML
    that points at them must not be, or a browser keeps running the previous
    build after a redeploy and calls endpoints that no longer exist.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if path.endswith(".html") or path in ("", "."):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        elif response.status_code == 200 and "/assets/" in scope.get("path", ""):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


if STATIC_DIR.is_dir():
    # Mounted last so it never shadows /api or /ws.
    app.mount("/", UiFiles(directory=STATIC_DIR, html=True), name="ui")
else:
    log.warning("Frontend build not found at %s - run `npm run build` in frontend/", STATIC_DIR)

    @app.get("/", tags=["meta"])
    async def missing_ui() -> dict:
        return {
            "ok": False,
            "error": "Frontend is not built.",
            "hint": "cd frontend && npm install && npm run build",
            "api_docs": "/docs",
        }
