# DevLoop

AI-powered production incident resolution agent. Sentry detects a crash → DevLoop finds the root cause, writes a regression test that **reproduces** the bug, patches it, **proves** the fix (red → green) on GitHub Actions or Docker, retries with test feedback if needed, opens a GitHub PR, and notifies Slack — automatically.

**Live demo:** https://devloop-frontend.vercel.app
**Backend API:** https://devloop-qtn8.onrender.com

---

## How it works

```
Sentry webhook  ──►  POST /webhook/sentry
Manual trigger  ──►  POST /trigger
Demo trigger    ──►  POST /trigger/demo
Break prod      ──►  POST /demo/break     (bad deploy → simulated Sentry alert → pipeline)
                        │
                   orchestrator.py
                   ├── github_client.py   fetch file + tree → branch → one commit (fix + test) → PR
                   ├── codex_client.py    analyzer → fixer (edits + regression test) → refiner
                   ├── sandbox_runner.py  red/green test phases on GitHub Actions or Docker
                   ├── events.py          structured timeline → dashboard (SSE) + run record
                   └── slack_notify.py    incoming webhook notification
```

### Pipeline

1. **Analyzer** (small/fast) — reads error + stack trace only → `{root_cause, buggy_line, fix_strategy, scope}`
2. **Fixer** (code-focused) — reads the file (or an 80-line window around the crash for big files) + analysis → **search/replace edits** (never the whole file) + one regression test
3. **Reproduce (red)** — the regression test runs on the original code and must fail
4. **Verify (green)** — full test suite with the fix. Red and green share one sandbox run
5. **Self-heal** — if green fails, the test output goes back to the fixer for follow-up edits, up to `MAX_FIX_ATTEMPTS` (default 3). Still failing → the PR opens as a **draft**
6. **PR** — fix + regression test in one commit; the PR body shows the red/green proof and every attempt

### Rate limits and tokens

- Analyzer and fixer pools each have 4 free OpenRouter models × every configured key.
- A 429 cools that model+key down (honours `Retry-After` / `X-RateLimit-Reset`) and the next one is tried. When the **whole batch** is cooling down, DevLoop waits for the earliest reset and retries the batch (`LLM_MAX_BATCHES`, `LLM_MAX_WAIT_SECONDS`). Resets far away (daily free quota) fail fast with a clear message.
- Each step retries on its own — a successful analysis is never re-run because the fixer was rate-limited.
- Output is edits, not files, and big files are sent as an excerpt, so tokens scale with the fix, not the file. Token use is shown per run.

### Test runners (`TEST_RUNNER`)

| Value | How |
|---|---|
| `auto` (default) | Docker if the daemon is reachable, else GitHub Actions if a token is set, else skip |
| `actions` | Dispatches `.github/workflows/devloop-sandbox.yml` on `ACTIONS_RUNNER_REPO`, which checks out the target repo, overlays the patch and runs `sandbox/phase_runner.py`. **Use this on Render** (no Docker there) |
| `docker` | Downloads the repo tarball, runs the same phase runner in `DOCKER_IMAGE` |
| `none` | Skip tests (PR says so) |

Actions setup: push this repo (the workflow must be on the default branch of `ACTIONS_RUNNER_REPO`), and give `ACTIONS_RUNNER_TOKEN` (or `GITHUB_TOKEN`) **Actions: read & write** on it. Private target repos also need a `TARGET_REPO_TOKEN` secret in the runner repo with read access to them.

---

## Stack

| Layer | Tech |
|---|---|
| Backend | FastAPI, Python 3.11+ |
| LLM | OpenRouter (free tier, multi-model fallback) |
| GitHub | PyGithub — fetch, branch, commit, PR |
| Auth | GitHub OAuth + Slack OAuth, JWT cookies |
| Database | Supabase (PostgreSQL) — users, runs, logs, repos |
| Live logs | Server-Sent Events (SSE) per user |
| Frontend | Next.js 15, Tailwind CSS, TypeScript |
| Sandbox | Docker |

---

## Local setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

Requires Python 3.11+ and Docker running locally.

### 2. Configure environment

```bash
cp .env.example .env
```

Key variables:

| Variable | Description |
|---|---|
| `OPENROUTER_API_KEY` | Primary LLM key — free at openrouter.ai |
| `OPENROUTER_API_KEY_2` | Backup LLM key (different account) for rate limit failover |
| `GITHUB_TOKEN` | Fallback PAT if user has no OAuth token |
| `GITHUB_CLIENT_ID / SECRET` | GitHub OAuth app credentials |
| `SLACK_CLIENT_ID / SECRET` | Slack OAuth app (scope: `incoming-webhook`) |
| `SUPABASE_URL / SERVICE_KEY` | Supabase project credentials |
| `JWT_SECRET` | Cookie signing secret |
| `FRONTEND_URL` | CORS origin (default: `http://localhost:3000`) |
| `BACKEND_URL` | Self-reference for OAuth callbacks (default: `http://localhost:8000`) |
| `SENTRY_WEBHOOK_SECRET` | Global Sentry webhook secret. Per-repo secrets (set when adding a repo) also work and route the run to that user + repo |
| `ALLOW_UNSIGNED_WEBHOOKS` | Local dev only — `true` accepts unsigned Sentry webhooks |
| `DEMO_REPO` | Repo the "Try Demo Repo" button targets (`owner/name`) |
| `DEMO_COOLDOWN_SECONDS` | Min seconds between demo runs (default 120) |
| `DEMO_BASE_BRANCH` | Branch "Break prod" forks incident branches from (default `main`) |
| `DEMO_APP_ROOT` | Folder in the demo repo containing `store/` (default empty = repo root) |
| `TEST_RUNNER` | `auto` / `actions` / `docker` / `none` (see above) |
| `ACTIONS_RUNNER_REPO` | Repo holding the sandbox workflow (default `Rishikesh183/devloop`) |
| `ACTIONS_RUNNER_TOKEN` | Token with Actions write on that repo (falls back to `GITHUB_TOKEN`) |
| `MAX_FIX_ATTEMPTS` | Fix → test → feedback rounds before giving up (default 3) |
| `LLM_MAX_BATCHES` / `LLM_MAX_WAIT_SECONDS` | Rate-limit batch retries (default 4) / longest single wait (default 60s) |

### 3. Create Supabase tables

Run this once in your Supabase SQL editor:

```sql
create table if not exists users (
  id uuid primary key default gen_random_uuid(),
  github_id text unique not null,
  github_login text,
  github_token text,
  slack_webhook_url text,
  preferred_model text,
  user_openrouter_key text,
  created_at timestamptz default now()
);

create table if not exists runs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references users(id) on delete cascade,
  error_message text,
  filename text,
  environment text,
  status text default 'running',
  pr_url text,
  branch text,
  test_passed boolean,
  started_at timestamptz default now(),
  completed_at timestamptz,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create table if not exists log_lines (
  id uuid primary key default gen_random_uuid(),
  run_id uuid references runs(id) on delete cascade,
  user_id uuid references users(id) on delete cascade,
  level text,
  message text,
  created_at timestamptz default now()
);

create table if not exists user_repos (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references users(id) on delete cascade,
  repo text not null,
  base_branch text default 'main',
  sentry_secret text,
  created_at timestamptz default now(),
  unique(user_id, repo)
);
create index if not exists idx_user_repos_user_id on user_repos(user_id);
create index if not exists idx_log_lines_run_id on log_lines(run_id, created_at);
```

Already have the tables? Add the newer columns:

```sql
alter table users add column if not exists preferred_model text;
alter table users add column if not exists user_openrouter_key text;
alter table runs add column if not exists started_at timestamptz default now();
alter table runs add column if not exists completed_at timestamptz;
alter table runs add column if not exists root_cause text;
alter table runs add column if not exists fix_summary text;
alter table runs add column if not exists diff text;
alter table runs add column if not exists attempts int;
alter table runs add column if not exists reproduced boolean;
alter table runs add column if not exists duration_seconds real;
alter table runs add column if not exists tokens_used int;
alter table runs add column if not exists events jsonb;
```

Without these, runs still save their basic fields; the dashboard just can't replay old runs.

### 4. Run locally

```bash
# Backend
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# Frontend
cd dashboard && npm run dev

# Optional: expose backend for Sentry webhooks
ngrok http 8000
```

---

## Connecting Sentry

Use an **Internal Integration** (signed webhooks). The old "Integrations → WebHooks" plugin sends unsigned requests, which DevLoop rejects.

1. Sentry → **Settings → Developer Settings → Custom Integrations → Create New Integration → Internal Integration**
2. Webhook URL: `https://devloop-qtn8.onrender.com/webhook/sentry`
3. Turn on **Alert Rule Action**. Permissions: Issue & Event → Read. (Optional: subscribe to `issue` webhooks)
4. Save, copy the **Client Secret**. Either paste it as the repo's "Sentry secret" in the dashboard (routes to that user + repo), or set it as `SENTRY_WEBHOOK_SECRET` (global, runs against `GITHUB_REPO`)
5. **Alerts → Create Alert → Issues**: when "a new issue is created" → action "Send a notification via DevLoop" (your integration)
6. Your app must report to Sentry (`sentry_sdk.init(dsn=...)`), and frame paths must match repo paths

No prod bugs? Hit **💥 Break Prod** on the dashboard: DevLoop pushes one of four realistic bugs (`agent/scenarios.py`) to a fresh `incident/*` branch of the demo repo, fires a Sentry-shaped alert, and you watch the timer from bad deploy to PR. Or use the **"Try Demo Repo"** button on the dashboard — fires against the repo set in `DEMO_REPO` (default `rishikesh183/devloop-demo-app`) which has a live bug maintained for demos.

---

## Deploy

**Backend (Render)**
- Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Set all env vars in Render dashboard

**Frontend (Vercel)**
- Root dir: `dashboard`
- Env var: `NEXT_PUBLIC_BACKEND_URL=https://devloop-qtn8.onrender.com`

**OAuth callback URLs to register:**
- GitHub: `https://devloop-qtn8.onrender.com/auth/github/callback`
- Slack: `https://devloop-qtn8.onrender.com/auth/slack/callback`

---

## Tests

```bash
pytest
```

Covers edit application, rate-limit batching, the real phase runner (red → green on a tiny project), break-prod scenarios and the orchestrator loop (with GitHub/LLM/sandbox faked).

## File map

```
main.py                    FastAPI app — all endpoints, SSE, OAuth
agent/
  orchestrator.py          Pipeline coordinator
  codex_client.py          Two-LLM pipeline with model fallback
  github_client.py         PyGithub wrapper
  sandbox_runner.py        Test phases on GitHub Actions or Docker
  edits.py                 Search/replace edit application + diffs
  events.py                Structured run timeline for the dashboard
  scenarios.py             "Break prod" bug scenarios
  slack_notify.py          Slack webhook sender
db/
  client.py                Supabase singleton
  users.py                 User CRUD
  runs.py                  Run + log_lines CRUD
  repos.py                 user_repos CRUD
dashboard/
  app/page.tsx             Landing page
  app/dashboard/page.tsx   Main dashboard UI
sandbox/phase_runner.py    Runs red/green phases inside the sandbox
.github/workflows/devloop-sandbox.yml   Actions sandbox (dispatched by DevLoop)
tests/                     Backend tests
mock_sentry_payload.json   Demo payload — TypeError in store/pricing.py
```
