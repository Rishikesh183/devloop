# DevLoop

DevLoop is a production incident resolution agent. When Sentry detects an error, DevLoop automatically fetches the affected file, uses OpenAI Codex to diagnose and fix the bug, tests the patch in a Docker sandbox, and opens a GitHub PR — no human needed until review.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

Requires Python 3.11+ and Docker running locally.

### 2. Configure environment

```bash
cp .env.example .env
```

Fill in `.env`:

| Variable | Description |
|---|---|
| `SENTRY_WEBHOOK_SECRET` | From Sentry project settings → Client Keys |
| `OPENAI_API_KEY` | OpenAI key with access to `codex-mini-latest` |
| `GITHUB_TOKEN` | Personal access token (`repo` + `pull_request` scopes) |
| `GITHUB_REPO` | Target repo as `owner/reponame` |
| `GITHUB_BASE_BRANCH` | Branch to diff against and PR into (default: `main`) |
| `SLACK_WEBHOOK_URL` | Slack incoming webhook URL (optional) |
| `TEST_COMMAND` | Command to run in sandbox (default: `pytest`) |
| `DOCKER_IMAGE` | Docker image for tests (default: `python:3.11-slim`) |

### 3. Start the server

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Connect Sentry Webhook

1. Go to your Sentry project → **Settings → Integrations → Webhooks**
2. Add webhook URL: `https://your-server.com/webhook/sentry`
3. Enable the **Error** event type
4. Copy the webhook secret and set `SENTRY_WEBHOOK_SECRET` in `.env`

## Test Locally

Send a mock Sentry payload without a real Sentry account:

```bash
curl -X POST http://localhost:8000/webhook/sentry \
  -H "Content-Type: application/json" \
  -d @mock_sentry_payload.json
```

The server returns `200` immediately. Watch logs for the full pipeline:

```
DevLoop triggered for error: TypeError: unsupported operand type(s)...
Fetching app/utils/calculator.py from owner/repo@main
Codex generated fix successfully for app/utils/calculator.py
Branch fix/devloop-20240315143022 created
Running sandbox tests...
Tests PASSED in 8.43s
PR opened: https://github.com/owner/repo/pull/42
Slack notification sent
```

## Example PR Output

```markdown
## Error Summary
**File:** `app/utils/calculator.py`
**Error:** TypeError: unsupported operand type(s) for +: 'NoneType' and 'int'

## Root Cause
The `tax_amount` parameter can be `None` when no tax applies to an item,
but the function assumes it is always an integer. The addition `base_price + tax_amount`
raises `TypeError` when `tax_amount` is `None`.

## Fix Applied
Added a `None` guard: `tax_amount = tax_amount or 0` before the addition.
This treats missing tax as zero without changing the return type or logic.

## Test Results
**Status:** ✅ Passing
**Duration:** 8.43s
```

## Run Log

Every DevLoop run is appended to `devloop_runs.json` for audit and dashboard use.
