import asyncio
import hashlib
import hmac
import json
import logging
import os
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from agent.orchestrator import run_orchestrator

load_dotenv()

# In-memory log buffer — last 500 lines, broadcast to SSE clients
_log_buffer: deque = deque(maxlen=500)
_sse_clients: list = []

RUNS_LOG_PATH = Path("devloop_runs.json")


class SSELogHandler(logging.Handler):
    def emit(self, record):
        msg = self.format(record)
        _log_buffer.append(msg)
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            try:
                _sse_clients.remove(q)
            except ValueError:
                pass


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
sse_handler = SSELogHandler()
sse_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logging.getLogger().addHandler(sse_handler)

logger = logging.getLogger("devloop.main")

SENTRY_WEBHOOK_SECRET = os.getenv("SENTRY_WEBHOOK_SECRET", "")
MOCK_PAYLOAD_PATH = Path("mock_sentry_payload.json")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("DevLoop agent starting up")
    yield
    logger.info("DevLoop agent shutting down")


app = FastAPI(title="DevLoop", description="Production incident resolution agent", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def verify_sentry_signature(body: bytes, sentry_hook_signature: str) -> bool:
    if not SENTRY_WEBHOOK_SECRET:
        logger.warning("No SENTRY_WEBHOOK_SECRET set — skipping signature verification")
        return True
    expected = hmac.new(
        SENTRY_WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sentry_hook_signature or "")


def parse_sentry_payload(payload: dict) -> dict:
    event = payload.get("event", {})
    exception = event.get("exception", {})
    values = exception.get("values", [{}])
    exc_value = values[0] if values else {}

    error_message = exc_value.get("value", event.get("title", "Unknown error"))
    exc_type = exc_value.get("type", "Exception")

    frames = exc_value.get("stacktrace", {}).get("frames", [])
    stack_trace_lines = []
    filename = None
    lineno = None

    for frame in frames:
        f = frame.get("filename", "unknown")
        ln = frame.get("lineno", "?")
        func = frame.get("function", "?")
        stack_trace_lines.append(f'  File "{f}", line {ln}, in {func}')
        if frame.get("in_app", False):
            filename = f
            lineno = ln

    if not filename and frames:
        last = frames[-1]
        filename = last.get("filename", "unknown")
        lineno = last.get("lineno", None)

    stack_trace = "\n".join(stack_trace_lines) or "No stack trace available"
    environment = event.get("environment", payload.get("environment", "production"))

    return {
        "error_message": f"{exc_type}: {error_message}",
        "stack_trace": stack_trace,
        "filename": filename or "unknown",
        "lineno": lineno,
        "environment": environment,
        "event_id": event.get("event_id", ""),
        "project": payload.get("project", ""),
    }


@app.post("/webhook/sentry")
async def sentry_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    sentry_hook_signature: str = Header(default="", alias="Sentry-Hook-Signature"),
):
    body = await request.body()

    if SENTRY_WEBHOOK_SECRET and not verify_sentry_signature(body, sentry_hook_signature):
        logger.warning("Invalid Sentry webhook signature — rejecting request")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse Sentry webhook payload: %s", e)
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    parsed = parse_sentry_payload(payload)
    logger.info("Received Sentry webhook: %s in %s", parsed["error_message"], parsed["filename"])

    background_tasks.add_task(run_orchestrator, parsed)

    return {"status": "accepted", "message": "DevLoop processing started"}


@app.post("/trigger")
async def manual_trigger(background_tasks: BackgroundTasks):
    """Fire the mock Sentry payload — for dashboard manual trigger."""
    if not MOCK_PAYLOAD_PATH.exists():
        raise HTTPException(status_code=404, detail="mock_sentry_payload.json not found")
    try:
        payload = json.loads(MOCK_PAYLOAD_PATH.read_text())
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid mock payload: {e}")

    parsed = parse_sentry_payload(payload)
    logger.info("Manual trigger fired: %s in %s", parsed["error_message"], parsed["filename"])
    background_tasks.add_task(run_orchestrator, parsed)
    return {"status": "accepted", "message": "DevLoop pipeline started"}


@app.get("/runs")
async def get_runs():
    """Return all run records from devloop_runs.json."""
    if not RUNS_LOG_PATH.exists():
        return []
    try:
        runs = json.loads(RUNS_LOG_PATH.read_text(encoding="utf-8"))
        return list(reversed(runs))  # newest first
    except (json.JSONDecodeError, OSError):
        return []


@app.get("/logs/stream")
async def log_stream(request: Request):
    """SSE endpoint — streams live log lines to dashboard."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    _sse_clients.append(queue)

    # Send buffered history first
    history = list(_log_buffer)

    async def event_generator():
        try:
            for line in history:
                yield f"data: {line}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            try:
                _sse_clients.remove(queue)
            except ValueError:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/health")
async def health():
    return {"status": "ok", "service": "devloop"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
