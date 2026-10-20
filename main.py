import asyncio
import contextvars
import hashlib
import hmac
import json
import logging
import os
import random
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
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
_loop: asyncio.AbstractEventLoop | None = None  # set in lifespan; pipeline logs from worker threads

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
_DEFAULT_JWT_SECRET = "devloop-secret-change-in-prod"
JWT_SECRET = os.getenv("JWT_SECRET", _DEFAULT_JWT_SECRET)
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
SENTRY_WEBHOOK_SECRET = os.getenv("SENTRY_WEBHOOK_SECRET", "")
# Local dev only: accept Sentry webhooks that match no secret
ALLOW_UNSIGNED_WEBHOOKS = os.getenv("ALLOW_UNSIGNED_WEBHOOKS", "").lower() in ("1", "true", "yes")
MOCK_PAYLOAD_PATH = Path("mock_sentry_payload.json")
DEMO_REPO = os.getenv("DEMO_REPO") or "ManasaVeena1602/devloop-demo-app"
DEMO_COOLDOWN_SECONDS = int(os.getenv("DEMO_COOLDOWN_SECONDS", "120"))
DEMO_BASE_BRANCH = os.getenv("DEMO_BASE_BRANCH") or "main"
SESSION_MAX_AGE = 60 * 60 * 24 * 30
IS_SECURE = BACKEND_URL.startswith("https")
_last_demo_trigger: dict[str, float] = {}

# ── Logging setup ─────────────────────────────────────────────────────────────


class UserSSELogHandler(logging.Handler):
    """Broadcasts log lines to SSE queues for the current task's user_id."""

    def emit(self, record):
        uid = _current_user_id_var.get(None)
        if not uid:
            return
        msg = self.format(record)
        # Pipeline runs in a worker thread; asyncio.Queue is not thread-safe,
        # so hop back onto the event loop before touching the queues.
        if _loop is not None and _loop.is_running():
            try:
                if asyncio.get_running_loop() is _loop:
                    broadcast_log(uid, msg)
                    return
            except RuntimeError:
                pass
            _loop.call_soon_threadsafe(broadcast_log, uid, msg)
        else:
            broadcast_log(uid, msg)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
_sse_handler = UserSSELogHandler()
_sse_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logging.getLogger().addHandler(_sse_handler)
logger = logging.getLogger("devloop.main")


async def _run_orchestrator_with_context(parsed: dict, user_id: str, demo: bool = False):
    """Set user_id in ContextVar so logs route to that user's SSE stream.

    The pipeline is blocking (LLM, GitHub, Docker calls), so it runs in a worker
    thread; asyncio.to_thread copies the ContextVar into that thread.
    """
    _current_user_id_var.set(user_id)
    from agent.orchestrator import run_orchestrator
    await asyncio.to_thread(run_orchestrator, parsed, user_id, demo)


def _publish_event(user_id: str, event: dict) -> None:
    """Sink for agent.events: forward a structured pipeline event to the user's SSE stream."""
    if _loop is not None and _loop.is_running():
        _loop.call_soon_threadsafe(broadcast_log, user_id, event)


def _sse_frame(item: str | dict) -> str:
    if isinstance(item, dict):
        return f"event: devloop\ndata: {json.dumps(item, default=str)}\n\n"
    # Each line needs its own "data:" prefix or multi-line log messages break the stream
    return "".join(f"data: {line}\n" for line in str(item).split("\n")) + "\n"


def broadcast_log(user_id: str, message: str | dict):
    """Push a log line (str) or pipeline event (dict) to a user's SSE stream. Must run on the event loop."""
    if user_id not in _log_buffer:
        _log_buffer[user_id] = deque(maxlen=500)
    _log_buffer[user_id].append(message)
    for q in _sse_clients.get(user_id, []):
        try:
            q.put_nowait(message)
        except asyncio.QueueFull:
            pass


# ── JWT session helpers ───────────────────────────────────────────────────────

def create_token(sub: str, typ: str, ttl_seconds: int) -> str:
    claims = {"sub": sub, "typ": typ, "exp": int(time.time()) + ttl_seconds}
    return jwt.encode(claims, JWT_SECRET, algorithm="HS256")


def decode_token(token: str | None, typ: str) -> str | None:
    if not token:
        return None
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except JWTError:
        return None
    if payload.get("typ") != typ:
        return None
    return payload.get("sub")


def create_session_token(user_id: str) -> str:
    return create_token(user_id, "session", SESSION_MAX_AGE)


def get_current_user_id(session: str | None) -> str | None:
    return decode_token(session, "session")


def get_viewer_id(session: str | None, anon: str | None) -> str | None:
    """Logged-in user id, else this browser's anonymous id (never shared between visitors)."""
    return get_current_user_id(session) or decode_token(anon, "anon")


def _cookie_kwargs() -> dict:
    return {
        "httponly": True,
        "samesite": "none" if IS_SECURE else "lax",
        "secure": IS_SECURE,
    }


def _set_anon_cookie(response, anon_id: str) -> None:
    response.set_cookie("anon", create_token(anon_id, "anon", SESSION_MAX_AGE),
                        max_age=SESSION_MAX_AGE, **_cookie_kwargs())


# ── App setup ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _loop
    _loop = asyncio.get_running_loop()
    from agent import events
    events.set_sink(_publish_event)
    if JWT_SECRET == _DEFAULT_JWT_SECRET:
        logger.error("JWT_SECRET is the built-in default — set a random JWT_SECRET in production!")
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

def verify_sentry_signature(body: bytes, sig: str, secret: str) -> bool:
    if not secret or not sig:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def match_sentry_sender(body: bytes, sig: str) -> dict | None:
    """Work out who sent a webhook from its signature.

    Each user_repos row may carry its own Sentry secret; the one that verifies the
    HMAC identifies both the user and the repo. Falls back to the global secret.
    """
    if sig:
        try:
            from db.repos import list_repos_with_sentry_secret
            for row in list_repos_with_sentry_secret():
                if verify_sentry_signature(body, sig, row["sentry_secret"]):
                    return {"user_id": row["user_id"], "repo": row["repo"]}
        except Exception as e:
            logger.warning("Could not check per-repo Sentry secrets: %s", e)
    if verify_sentry_signature(body, sig, SENTRY_WEBHOOK_SECRET):
        return {"user_id": "anonymous", "repo": None}
    if ALLOW_UNSIGNED_WEBHOOKS:
        return {"user_id": "anonymous", "repo": None}
    return None


def sentry_should_ignore(payload: dict, resource: str) -> str | None:
    """Reason to skip a webhook (installs, resolved/assigned issues...), or None to run the pipeline."""
    if resource == "installation":
        return "installation event"
    action = payload.get("action")
    if action and action not in ("created", "triggered"):
        return f"action '{action}'"
    return None


def parse_sentry_payload(payload: dict) -> dict:
    # Legacy WebHooks plugin: top-level "event".
    # Internal Integration: data.event (alert rule action) / data.error (error.created) / data.issue.
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    event = payload.get("event") or data.get("event") or data.get("error") or {}
    if not event and data.get("issue"):
        issue = data["issue"]
        meta = issue.get("metadata") or {}
        event = {
            "title": issue.get("title"),
            "exception": {"values": [{
                "type": meta.get("type", "Exception"),
                "value": meta.get("value", issue.get("title", "Unknown error")),
                "stacktrace": {"frames": [{
                    "filename": meta.get("filename", "unknown"),
                    "function": meta.get("function", "?"),
                    "in_app": True,
                }]} if meta.get("filename") else {},
            }]},
        }
    exception = event.get("exception", {})
    values = exception.get("values", [{}])
    exc_value = values[-1] if values else {}   # last value is the exception actually raised
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
    response.set_cookie("session", session_token, max_age=SESSION_MAX_AGE, **_cookie_kwargs())
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
        f"&state={create_token(user_id, 'slack_state', 600)}"
    )
    return RedirectResponse(url)


@app.get("/auth/slack/callback")
async def slack_callback(code: str, state: str):
    user_id = decode_token(state, "slack_state")
    if not user_id:
        raise HTTPException(400, "Invalid or expired state")
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
                "preferred_model": user.get("preferred_model"),
                "has_user_key": bool(user.get("user_openrouter_key")),
            }
    except Exception:
        pass
    return {"authenticated": True, "user_id": user_id}


@app.patch("/me/settings")
async def update_settings(request: Request, session: str | None = Cookie(default=None)):
    user_id = get_current_user_id(session)
    if not user_id:
        raise HTTPException(401, "Not authenticated")
    body = await request.json()
    allowed = {"preferred_model", "user_openrouter_key"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        raise HTTPException(400, "No valid fields")
    try:
        from db.users import update_user
        update_user(user_id, **updates)
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(500, f"Could not update settings: {e}")


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


def _demo_viewer(session: str | None, anon: str | None) -> tuple[str, str | None]:
    """(user_id, new anon id to set as cookie or None). Applies the demo cooldown."""
    logged_in_id = get_current_user_id(session)
    user_id = get_viewer_id(session, anon)
    new_anon_id = None
    if not user_id:
        new_anon_id = f"anon_{uuid.uuid4().hex}"
        user_id = new_anon_id

    # Every demo opens a real PR with the server's token, so throttle it.
    # Anonymous visitors share one bucket (a fresh cookie would otherwise bypass it).
    now = time.monotonic()
    rate_key = logged_in_id or "anonymous"
    wait = DEMO_COOLDOWN_SECONDS - (now - _last_demo_trigger.get(rate_key, -1e9))
    if wait > 0:
        raise HTTPException(429, f"Demo is cooling down, try again in {int(wait) + 1}s")
    _last_demo_trigger[rate_key] = now

    # Ensure demo repo is in user's repo list
    if logged_in_id:
        try:
            from db.repos import ensure_demo_repo
            ensure_demo_repo(user_id, DEMO_REPO)
        except Exception:
            pass
    return user_id, new_anon_id


@app.post("/trigger/demo")
async def trigger_demo(
    background_tasks: BackgroundTasks,
    session: str | None = Cookie(default=None),
    anon: str | None = Cookie(default=None),
):
    """Fire demo pipeline against DEMO_REPO."""
    if not MOCK_PAYLOAD_PATH.exists():
        raise HTTPException(404, "mock_sentry_payload.json not found")
    user_id, new_anon_id = _demo_viewer(session, anon)

    from agent.scenarios import SOURCE_REL, source_path

    payload = json.loads(MOCK_PAYLOAD_PATH.read_text())
    parsed = parse_sentry_payload(payload)
    # Mock payload paths carry one repo's folder layout; re-root them on DEMO_APP_ROOT
    old_path = parsed["filename"]
    if old_path.endswith(SOURCE_REL) and old_path != source_path():
        parsed["filename"] = source_path()
        parsed["stack_trace"] = parsed["stack_trace"].replace(old_path, source_path())
    parsed["_demo_repo"] = DEMO_REPO
    logger.info("Demo trigger by user %s", user_id)
    background_tasks.add_task(_run_orchestrator_with_context, parsed, user_id, True)
    response = JSONResponse({"status": "accepted", "message": f"Demo pipeline started against {DEMO_REPO}", "repo": DEMO_REPO})
    if new_anon_id:
        _set_anon_cookie(response, new_anon_id)
    return response


@app.get("/demo/scenarios")
async def demo_scenarios():
    from agent.scenarios import SCENARIOS
    return [{"id": s.id, "title": s.title, "error": f"{s.exc_type}: {s.exc_value}"} for s in SCENARIOS]


def _break_prod_sync(scenario_id: str, user_id: str, run_key: str) -> None:
    """Bad deploy -> simulated Sentry alert -> normal pipeline, all on one timeline."""
    from agent import github_client
    from agent.events import RunEvents
    from agent.orchestrator import run_orchestrator
    from agent.scenarios import SCENARIOS_BY_ID, build_sentry_payload, source_path

    scenario = SCENARIOS_BY_ID[scenario_id]
    ev = RunEvents(user_id, run_key)
    now_iso = datetime.now(timezone.utc).isoformat()
    ev.emit("run", incident=True, incident_at=now_iso, started_at=now_iso, repo=DEMO_REPO,
            scenario=scenario.id, scenario_title=scenario.title, demo=True)
    logger.info("💥 Break prod: deploying '%s' to %s", scenario.title, DEMO_REPO)
    ev.step("deploy", "running", detail=scenario.title)
    token = os.getenv("GITHUB_TOKEN", "")
    branch = f"incident/{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{scenario.id}"
    try:
        github_client.create_branch(DEMO_REPO, branch, base_branch=DEMO_BASE_BRANCH, token=token)
        sha = github_client.commit_files(
            DEMO_REPO, branch, {source_path(): scenario.source}, scenario.commit_message, token=token,
        )
    except Exception as e:
        logger.error("❌ Could not push the bad deploy: %s", e)
        ev.step("deploy", "failed", detail=str(e)[:300])
        ev.emit("done", status="failed", failed_step="deploy", reason=str(e)[:300])
        return
    ev.step("deploy", "done", detail=branch, url=f"https://github.com/{DEMO_REPO}/commit/{sha}")

    payload = build_sentry_payload(scenario)
    parsed = parse_sentry_payload(payload)
    logger.info("🚨 Sentry alert (simulated): %s in %s", parsed["error_message"], parsed["filename"])
    ev.step("alert", "done", detail=parsed["error_message"])

    parsed.update(_demo_repo=DEMO_REPO, _base_branch=branch, _run_key=run_key, _prior_events=ev.events)
    run_orchestrator(parsed, user_id, True)


async def _run_break_prod_with_context(scenario_id: str, user_id: str, run_key: str):
    _current_user_id_var.set(user_id)
    await asyncio.to_thread(_break_prod_sync, scenario_id, user_id, run_key)


@app.post("/demo/break")
async def break_prod(
    request: Request,
    background_tasks: BackgroundTasks,
    session: str | None = Cookie(default=None),
    anon: str | None = Cookie(default=None),
):
    """Push a realistic bug to a fresh incident branch of the demo repo, then let DevLoop fix it."""
    from agent.scenarios import SCENARIOS, SCENARIOS_BY_ID
    try:
        body = await request.json()
    except Exception:
        body = {}
    scenario_id = (body or {}).get("scenario") or random.choice(SCENARIOS).id
    if scenario_id not in SCENARIOS_BY_ID:
        raise HTTPException(400, f"Unknown scenario {scenario_id!r}")
    user_id, new_anon_id = _demo_viewer(session, anon)
    run_key = uuid.uuid4().hex
    background_tasks.add_task(_run_break_prod_with_context, scenario_id, user_id, run_key)
    scenario = SCENARIOS_BY_ID[scenario_id]
    response = JSONResponse({
        "status": "accepted", "run": run_key, "scenario": scenario_id,
        "message": f"💥 Deploying '{scenario.title}' to {DEMO_REPO}...",
    })
    if new_anon_id:
        _set_anon_cookie(response, new_anon_id)
    return response


@app.post("/auth/logout")
async def logout():
    response = JSONResponse({"status": "ok"})
    response.delete_cookie("session", **_cookie_kwargs())
    return response


# ── Webhook ───────────────────────────────────────────────────────────────────

@app.post("/webhook/sentry")
async def sentry_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    sentry_hook_signature: str = Header(default="", alias="Sentry-Hook-Signature"),
    sentry_hook_resource: str = Header(default="", alias="Sentry-Hook-Resource"),
):
    body = await request.body()
    sender = match_sentry_sender(body, sentry_hook_signature)
    if not sender:
        raise HTTPException(401, "Invalid webhook signature")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")
    skip = sentry_should_ignore(payload, sentry_hook_resource)
    if skip:
        logger.info("Sentry webhook ignored: %s", skip)
        return {"status": "ignored", "reason": skip}

    parsed = parse_sentry_payload(payload)
    user_id = sender["user_id"]
    if sender["repo"]:
        parsed["_repo_override"] = sender["repo"]
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
    user_id = get_current_user_id(session)
    if not user_id:
        raise HTTPException(401, "Login with GitHub first")
    if not MOCK_PAYLOAD_PATH.exists():
        raise HTTPException(404, "mock_sentry_payload.json not found")
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    repo_override = body.get("repo")
    if repo_override:
        # Only repos the user has added, so nobody can aim the pipeline at arbitrary repos
        try:
            from db.repos import get_repo
            known = get_repo(user_id, repo_override)
        except Exception as e:
            raise HTTPException(503, f"Could not verify repo: {e}")
        if not known:
            raise HTTPException(403, "Repo not in your connected repos")
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
async def log_stream(
    request: Request,
    session: str | None = Cookie(default=None),
    anon: str | None = Cookie(default=None),
):
    user_id = get_viewer_id(session, anon)
    new_anon_id = None
    if not user_id:
        new_anon_id = f"anon_{uuid.uuid4().hex}"
        user_id = new_anon_id
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)

    if user_id not in _sse_clients:
        _sse_clients[user_id] = []
    _sse_clients[user_id].append(queue)

    history = list(_log_buffer.get(user_id, []))

    async def event_generator():
        try:
            for item in history:
                yield _sse_frame(item)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield _sse_frame(msg)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            try:
                _sse_clients[user_id].remove(queue)
            except ValueError:
                pass

    response = StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
    if new_anon_id:
        _set_anon_cookie(response, new_anon_id)
    return response


@app.get("/health")
async def health():
    return {"status": "ok", "service": "devloop"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
