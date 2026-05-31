import asyncio
import contextvars
import hashlib
import hmac
import json
import logging
import os
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Cookie, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from jose import JWTError, jwt

load_dotenv()

# ── In-memory SSE broadcast (keyed by user_id) ──────────────────────────────
_sse_clients: dict[str, list[asyncio.Queue]] = {}
_log_buffer: dict[str, deque] = {}

# ContextVar so each background task carries its own user_id into the log handler
_current_user_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_user_id_var", default=None
)

RUNS_LOG_PATH = Path("devloop_runs.json")  # legacy fallback for demo trigger

# ── Config ────────────────────────────────────────────────────────────────────
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")
SLACK_CLIENT_ID = os.getenv("SLACK_CLIENT_ID", "")
SLACK_CLIENT_SECRET = os.getenv("SLACK_CLIENT_SECRET", "")
JWT_SECRET = os.getenv("JWT_SECRET", "devloop-secret-change-in-prod")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
SENTRY_WEBHOOK_SECRET = os.getenv("SENTRY_WEBHOOK_SECRET", "")
MOCK_PAYLOAD_PATH = Path("mock_sentry_payload.json")

# ── Logging setup ─────────────────────────────────────────────────────────────


class UserSSELogHandler(logging.Handler):
    """Broadcasts log lines to SSE queues for the current task's user_id."""

    def emit(self, record):
        msg = self.format(record)
        uid = _current_user_id_var.get(None)
        if uid:
            if uid not in _log_buffer:
                _log_buffer[uid] = deque(maxlen=500)
            _log_buffer[uid].append(msg)
            dead = []
            for q in _sse_clients.get(uid, []):
                try:
                    q.put_nowait(msg)
                except asyncio.QueueFull:
                    dead.append(q)
            for q in dead:
                try:
                    _sse_clients[uid].remove(q)
                except ValueError:
                    pass


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
_sse_handler = UserSSELogHandler()
_sse_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logging.getLogger().addHandler(_sse_handler)
logger = logging.getLogger("devloop.main")


async def _run_orchestrator_with_context(parsed: dict, user_id: str, demo: bool = False):
    """Set user_id in ContextVar so logs route to that user's SSE stream."""
    _current_user_id_var.set(user_id)
    from agent.orchestrator import run_orchestrator
    await run_orchestrator(parsed, user_id, demo=demo)


def broadcast_log(user_id: str, message: str):
    """Push a log line to a specific user's SSE stream + Supabase."""
    if user_id not in _log_buffer:
        _log_buffer[user_id] = deque(maxlen=500)
    _log_buffer[user_id].append(message)
    for q in _sse_clients.get(user_id, []):
        try:
            q.put_nowait(message)
        except asyncio.QueueFull:
            pass


# ── JWT session helpers ───────────────────────────────────────────────────────

def create_session_token(user_id: str) -> str:
    return jwt.encode({"sub": user_id}, JWT_SECRET, algorithm="HS256")


def decode_session_token(token: str) -> str:
    payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    return payload["sub"]


def get_current_user_id(session: str | None) -> str | None:
    if not session:
        return None
    try:
        return decode_session_token(session)
    except JWTError:
        return None


# ── App setup ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("DevLoop agent starting up")
    yield
    logger.info("DevLoop agent shutting down")


app = FastAPI(title="DevLoop", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "http://localhost:3000", "https://devloop-frontend.vercel.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Sentry helpers ────────────────────────────────────────────────────────────

def verify_sentry_signature(body: bytes, sig: str) -> bool:
    if not SENTRY_WEBHOOK_SECRET:
        return True
    expected = hmac.new(SENTRY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig or "")


def parse_sentry_payload(payload: dict) -> dict:
    event = payload.get("event", {})
    exception = event.get("exception", {})
    values = exception.get("values", [{}])
    exc_value = values[0] if values else {}
    error_message = exc_value.get("value", event.get("title", "Unknown error"))
    exc_type = exc_value.get("type", "Exception")
    frames = exc_value.get("stacktrace", {}).get("frames", [])
    stack_lines = []
    filename = None
    lineno = None
    for frame in frames:
        f = frame.get("filename", "unknown")
        ln = frame.get("lineno", "?")
        func = frame.get("function", "?")
        stack_lines.append(f'  File "{f}", line {ln}, in {func}')
        if frame.get("in_app", False):
            filename = f
            lineno = ln
    if not filename and frames:
        last = frames[-1]
        filename = last.get("filename", "unknown")
        lineno = last.get("lineno", None)
    return {
        "error_message": f"{exc_type}: {error_message}",
        "stack_trace": "\n".join(stack_lines) or "No stack trace",
        "filename": filename or "unknown",
        "lineno": lineno,
        "environment": event.get("environment", payload.get("environment", "production")),
        "event_id": event.get("event_id", ""),
        "project": payload.get("project", ""),
    }


# ── GitHub OAuth ──────────────────────────────────────────────────────────────

@app.get("/auth/github")
async def github_login():
    if not GITHUB_CLIENT_ID:
        raise HTTPException(500, "GITHUB_CLIENT_ID not configured")
    url = (
        f"https://github.com/login/oauth/authorize"
        f"?client_id={GITHUB_CLIENT_ID}"
        f"&scope=repo,read:user"
        f"&redirect_uri={BACKEND_URL}/auth/github/callback"
    )
    return RedirectResponse(url)


@app.get("/auth/github/callback")
async def github_callback(code: str):
    if not code:
        raise HTTPException(400, "Missing code")
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://github.com/login/oauth/access_token",
            json={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code,
            },
            headers={"Accept": "application/json"},
        )
        token_data = token_resp.json()
        github_token = token_data.get("access_token")
        if not github_token:
            raise HTTPException(400, f"GitHub OAuth failed: {token_data}")

        user_resp = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {github_token}", "Accept": "application/json"},
        )
        github_user = user_resp.json()

    try:
        from db.users import upsert_user
        user = upsert_user(
            github_id=str(github_user["id"]),
            github_login=github_user["login"],
            github_token=github_token,
        )
        user_id = user["id"]
    except Exception as e:
        logger.warning("Supabase unavailable, using ephemeral session: %s", e)
        user_id = f"gh_{github_user['id']}"

    session_token = create_session_token(user_id)
    response = RedirectResponse(f"{FRONTEND_URL}/dashboard?connected=github")
    is_secure = BACKEND_URL.startswith("https")
    response.set_cookie(
        "session", session_token,
        httponly=True,
        samesite="none" if is_secure else "lax",
        secure=is_secure,
        max_age=60 * 60 * 24 * 30,
    )
    return response


# ── Slack OAuth ───────────────────────────────────────────────────────────────

@app.get("/auth/slack")
async def slack_login(session: str | None = Cookie(default=None)):
    user_id = get_current_user_id(session)
    if not user_id:
        raise HTTPException(401, "Login with GitHub first")
    if not SLACK_CLIENT_ID:
        raise HTTPException(500, "SLACK_CLIENT_ID not configured")
    url = (
        f"https://slack.com/oauth/v2/authorize"
        f"?client_id={SLACK_CLIENT_ID}"
        f"&scope=incoming-webhook"
        f"&redirect_uri={BACKEND_URL}/auth/slack/callback"
        f"&state={create_session_token(user_id)}"
    )
    return RedirectResponse(url)


@app.get("/auth/slack/callback")
async def slack_callback(code: str, state: str):
    user_id = get_current_user_id(state)
    if not user_id:
        raise HTTPException(400, "Invalid state")
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://slack.com/api/oauth.v2.access",
            data={
                "client_id": SLACK_CLIENT_ID,
                "client_secret": SLACK_CLIENT_SECRET,
                "code": code,
                "redirect_uri": f"{BACKEND_URL}/auth/slack/callback",
            },
        )
        data = resp.json()
    if not data.get("ok"):
        raise HTTPException(400, f"Slack OAuth failed: {data.get('error')}")

    webhook_url = data.get("incoming_webhook", {}).get("url")
    if webhook_url:
        try:
            from db.users import update_user
            update_user(user_id, slack_webhook_url=webhook_url)
        except Exception as e:
            logger.warning("Could not save Slack webhook to Supabase: %s", e)

    return RedirectResponse(f"{FRONTEND_URL}/dashboard?connected=slack")


# ── Session info ──────────────────────────────────────────────────────────────

@app.get("/me")
async def me(session: str | None = Cookie(default=None)):
    user_id = get_current_user_id(session)
    if not user_id:
        return {"authenticated": False}
    try:
        from db.users import get_user
        user = get_user(user_id)
        if user:
            return {
                "authenticated": True,
                "user_id": user_id,
                "github_login": user.get("github_login"),
                "github_repo": user.get("github_repo"),
                "slack_connected": bool(user.get("slack_webhook_url")),
            }
    except Exception:
        pass
    return {"authenticated": True, "user_id": user_id}


@app.get("/me/repos")
async def list_repos(session: str | None = Cookie(default=None)):
    user_id = get_current_user_id(session)
    if not user_id:
        raise HTTPException(401, "Not authenticated")
    try:
        from db.repos import list_repos as db_list_repos
        return db_list_repos(user_id)
    except Exception as e:
        raise HTTPException(500, f"Could not list repos: {e}")


@app.post("/me/repos")
async def add_repo(request: Request, session: str | None = Cookie(default=None)):
    user_id = get_current_user_id(session)
    if not user_id:
        raise HTTPException(401, "Not authenticated")
    body = await request.json()
    repo = body.get("repo", "").strip()
    branch = body.get("branch", "main").strip()
    sentry_secret = body.get("sentry_secret", "").strip() or None
    if not repo:
        raise HTTPException(400, "repo required")
    try:
        from db.repos import add_repo as db_add_repo
        result = db_add_repo(user_id, repo, branch, sentry_secret)
        return {"status": "ok", "repo": result}
    except Exception as e:
        raise HTTPException(500, f"Could not add repo: {e}")


@app.delete("/me/repos/{repo:path}")
async def delete_repo(repo: str, session: str | None = Cookie(default=None)):
    user_id = get_current_user_id(session)
    if not user_id:
        raise HTTPException(401, "Not authenticated")
    try:
        from db.repos import remove_repo
        remove_repo(user_id, repo)
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(500, f"Could not remove repo: {e}")


@app.post("/trigger/demo")
async def trigger_demo(background_tasks: BackgroundTasks, session: str | None = Cookie(default=None)):
    """Fire demo pipeline against rishikesh183/devloop-demo-app."""
    user_id = get_current_user_id(session) or "anonymous"
    if not MOCK_PAYLOAD_PATH.exists():
        raise HTTPException(404, "mock_sentry_payload.json not found")

    # Ensure demo repo is in user's repo list
    if user_id != "anonymous":
        try:
            from db.repos import ensure_demo_repo
            ensure_demo_repo(user_id)
        except Exception:
            pass

    payload = json.loads(MOCK_PAYLOAD_PATH.read_text())
    parsed = parse_sentry_payload(payload)
    parsed["_demo_repo"] = "rishikesh183/devloop-demo-app"
    logger.info("Demo trigger by user %s", user_id)
    background_tasks.add_task(_run_orchestrator_with_context, parsed, user_id, True)
    return {"status": "accepted", "message": "Demo pipeline started against rishikesh183/devloop-demo-app"}


@app.post("/auth/logout")
async def logout():
    response = JSONResponse({"status": "ok"})
    response.delete_cookie("session")
    return response


# ── Webhook ───────────────────────────────────────────────────────────────────

@app.post("/webhook/sentry")
async def sentry_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    sentry_hook_signature: str = Header(default="", alias="Sentry-Hook-Signature"),
    session: str | None = Cookie(default=None),
):
    body = await request.body()
    if SENTRY_WEBHOOK_SECRET and not verify_sentry_signature(body, sentry_hook_signature):
        raise HTTPException(401, "Invalid webhook signature")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")

    parsed = parse_sentry_payload(payload)
    user_id = get_current_user_id(session) or "anonymous"
    logger.info("Sentry webhook: %s in %s (user: %s)", parsed["error_message"], parsed["filename"], user_id)

    background_tasks.add_task(_run_orchestrator_with_context, parsed, user_id)
    return {"status": "accepted"}


# ── Manual trigger ────────────────────────────────────────────────────────────

@app.post("/trigger")
async def manual_trigger(
    request: Request,
    background_tasks: BackgroundTasks,
    session: str | None = Cookie(default=None),
):
    user_id = get_current_user_id(session) or "anonymous"
    if not MOCK_PAYLOAD_PATH.exists():
        raise HTTPException(404, "mock_sentry_payload.json not found")
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    repo_override = body.get("repo")
    payload = json.loads(MOCK_PAYLOAD_PATH.read_text())
    parsed = parse_sentry_payload(payload)
    if repo_override:
        parsed["_repo_override"] = repo_override
    logger.info("Manual trigger by user %s: %s", user_id, parsed["error_message"])
    background_tasks.add_task(_run_orchestrator_with_context, parsed, user_id)
    return {"status": "accepted", "message": "DevLoop pipeline started"}


# ── Runs API ──────────────────────────────────────────────────────────────────

@app.get("/runs")
async def get_runs(session: str | None = Cookie(default=None)):
    user_id = get_current_user_id(session)
    if not user_id:
        return []
    try:
        from db.runs import get_runs_for_user
        return get_runs_for_user(user_id)
    except Exception as e:
        logger.warning("Supabase unavailable for runs: %s", e)
        return []


# ── SSE log stream ────────────────────────────────────────────────────────────

@app.get("/logs/stream")
async def log_stream(request: Request, session: str | None = Cookie(default=None)):
    user_id = get_current_user_id(session) or "anonymous"
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)

    if user_id not in _sse_clients:
        _sse_clients[user_id] = []
    _sse_clients[user_id].append(queue)

    history = list(_log_buffer.get(user_id, []))

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
                _sse_clients[user_id].remove(queue)
            except ValueError:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/health")
async def health():
    return {"status": "ok", "service": "devloop"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
