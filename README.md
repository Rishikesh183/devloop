# DevLoop

AI-powered production incident resolution agent. Sentry detects a crash → DevLoop fetches the broken file, runs a two-LLM diagnosis + fix pipeline, tests the patch in a Docker sandbox, opens a GitHub PR, and notifies Slack — automatically.

**Live demo:** https://devloop-frontend.vercel.app
**Backend API:** https://devloop-qtn8.onrender.com

---

## How it works

```
Sentry webhook  ──►  POST /webhook/sentry
Manual trigger  ──►  POST /trigger
Demo trigger    ──►  POST /trigger/demo
                        │
                   orchestrator.py
                   ├── github_client.py   fetch file → create branch → commit → open PR
                   ├── codex_client.py    two-LLM pipeline: analyzer → fixer
                   ├── sandbox_runner.py  Docker test runner
                   └── slack_notify.py    incoming webhook notification
```

### Two-LLM pipeline

1. **Analyzer** (small/fast) — reads error + stack trace only, outputs `{root_cause, buggy_line, fix_strategy, scope}`
2. **Fixer** (code-focused) — reads file + analyzer output only, outputs `{fixed_code, fix_summary}`

Both pools have 4 free OpenRouter models + a backup API key as fallback. `max_retries=0` on the SDK — our loop handles 429s by moving to the next model.

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
| `SENTRY_WEBHOOK_SECRET` | Optional — verifies Sentry webhook HMAC signature |

### 3. Create Supabase tables

Run this once in your Supabase SQL editor:

```sql
create table if not exists users (
  id uuid primary key default gen_random_uuid(),
  github_id text unique not null,
  github_login text,
  github_token text,
  slack_webhook_url text,
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
```

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

1. Sentry project → **Settings → Integrations → Webhooks**
2. Add URL: `https://devloop-qtn8.onrender.com/webhook/sentry`
3. Enable **issue** events
4. Copy signing secret → set `SENTRY_WEBHOOK_SECRET` in `.env`

No prod bugs? Use the **"Try Demo Repo"** button on the dashboard — fires against `rishikesh183/devloop-demo-app` which has a live bug maintained for demos.

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

## File map

```
main.py                    FastAPI app — all endpoints, SSE, OAuth
agent/
  orchestrator.py          Pipeline coordinator
  codex_client.py          Two-LLM pipeline with model fallback
  github_client.py         PyGithub wrapper
  sandbox_runner.py        Docker test runner
  slack_notify.py          Slack webhook sender
db/
  client.py                Supabase singleton
  users.py                 User CRUD
  runs.py                  Run + log_lines CRUD
  repos.py                 user_repos CRUD
dashboard/
  app/page.tsx             Landing page
  app/dashboard/page.tsx   Main dashboard UI
mock_sentry_payload.json   Demo payload — TypeError in store/pricing.py
```
