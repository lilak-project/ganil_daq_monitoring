"""GANIL DAQ control service.

Serves a JSON API plus a WebSocket status stream, and (once the frontend is
built) the single-page UI from the same origin and port.

Two halves. Monitoring reads the run-control logs and the run directory, and
asks the run control (RCC) for its state over SOAP. Control sends the RCC Init,
Start, Stop and storage on/off, over the same SOAP -- only for a portal manager
or an admin of this service (access.py), each command gated on the state and
written to the audit log (rcc.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from . import access, elog, rcc, settings
from .config import BROADCAST_INTERVAL, DEFAULT_RUN_LIMIT, STATIC_DIR
from .monitor import monitor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ganil")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings.load()
    elog.install(monitor)
    monitor.start()
    rcc.watch.start()
    if not elog.registered():
        log.info("not registered with elog yet; run boundaries will be watched but "
                 "not pushed. Register portal://ganil_daq_monitoring/api/elog.")
    try:
        yield
    finally:
        rcc.watch.stop()
        monitor.stop()


app = FastAPI(
    title="GANIL DAQ Control",
    description=(
        "Run status for the GANIL acquisition: which run is going, the note it "
        "was started with, what it is writing and how fast, and every run "
        "already taken; and, for a controller, the run control's Init, Start, "
        "Stop and storage on/off."
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

@app.middleware("http")
async def control_gate(request: Request, call_next):
    """Monitoring callers may read everything and command nothing (access.py)."""
    if access.needs_control(request):
        caller = await run_in_threadpool(access.who, request)
        if not caller["control"]:
            return JSONResponse(status_code=403, content={
                "detail": "Monitoring only: commanding the run control needs a portal manager or an admin of this service."})
    return await call_next(request)


def status_payload() -> dict:
    return {**monitor.snapshot(), "rcc": rcc.watch.last()}


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


@app.get("/api/status", tags=["run"], summary="The run going now, the DAQ's components, the run control's state")
async def status() -> dict:
    return status_payload()


@app.get("/api/me", tags=["meta"])
async def me(request: Request) -> dict:
    """Who is looking, and whether they may control (the page's mode switch)."""
    return await run_in_threadpool(access.who, request)


@app.post("/api/rcc/{verb}", tags=["control"], summary="init | start | stop | storage-on | storage-off")
async def command(verb: str, request: Request) -> dict:
    """Send one command to the run control, if its state allows it. Refused
    commands come back ok=false with the reason, and are audited too."""
    if verb not in rcc.ALLOWED:
        raise HTTPException(status_code=404, detail=f"not a command: {verb}")
    caller = await run_in_threadpool(access.who, request)
    return await run_in_threadpool(rcc.watch.command, verb, caller.get("name") or "standalone")


@app.get("/api/settings", tags=["control"], summary="The settings, the experiments to pick from, the first values")
async def read_settings() -> dict:
    return {"values": settings.current(), "defaults": settings.DEFAULTS, "saved": settings.saved(),
            "experiments": settings.experiments(), "experiments_dir": str(settings.EXPERIMENTS_DIR)}


@app.put("/api/settings", tags=["control"], summary="Save and apply at once")
async def write_settings(body: dict, request: Request) -> dict:
    try:
        values, changed = await run_in_threadpool(settings.save, body)
    except (ValueError, TypeError) as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    if "acq_root" in changed:
        await run_in_threadpool(monitor.reset)
    if {"rcc_host", "rcc_port"} & set(changed):
        rcc.watch.forget_equipment()
    caller = await run_in_threadpool(access.who, request)
    rcc.record(caller.get("name") or "standalone", "settings", True,
               ", ".join(f"{k}={values[k]}" for k in changed) or "nothing changed")
    return {"values": values, "changed": changed}


@app.get("/api/audit", tags=["control"], summary="Commands sent, newest first")
async def audit(limit: int = 50) -> dict:
    return {"entries": rcc.audit(max(1, min(limit, 500)))}


@app.get("/api/runs", tags=["run"], summary="Every run attempt, newest first")
async def runs(limit: int = DEFAULT_RUN_LIMIT) -> dict:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    attempts = monitor.attempts(limit=max(1, min(limit, 5000)))
    from . import config
    return {
        "run_dir": str(config.RUN_DIR),
        "attempts": [a.as_dict(now) for a in attempts],
        "orphan_files": [f.as_dict() for f in monitor.orphans()],
    }


@app.get("/api/errors", tags=["run"], summary="Recent run-control errors, newest first")
async def errors(limit: int = 50) -> dict:
    return {"errors": [{"time": t.isoformat(), "message": m}
                       for t, m in monitor.errors(max(1, min(limit, 500)))]}


@app.websocket("/ws/status")
async def status_socket(socket: WebSocket) -> None:
    """Push the live view. Commands go over POST, never this socket."""
    await socket.accept()
    try:
        while True:
            await socket.send_json(status_payload())
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
