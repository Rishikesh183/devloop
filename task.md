Build a production incident resolution agent called DevLoop.

## What it does
Monitors production errors via Sentry webhooks, uses OpenAI Codex to 
diagnose and fix the bug in the actual codebase, runs tests in a Docker 
sandbox, and opens a GitHub PR with the fix — automatically, without 
human intervention until review.

## Tech Stack
- Python + FastAPI (backend agent)
- OpenAI Responses API with codex-mini-latest model
- Sentry webhook (error intake)
- GitHub REST API via PyGithub (PR creation)
- Docker (sandbox test runner)
- Slack webhook (engineer notification)
- Next.js (simple dashboard, optional for now)

## Project Structure
devloop/
├── main.py                  # FastAPI app, webhook receiver
├── agent/
│   ├── orchestrator.py      # Main agent loop
│   ├── codex_client.py      # OpenAI Codex API calls
│   ├── github_client.py     # Repo fetch, branch, PR creation
│   ├── sandbox_runner.py    # Docker test execution
│   └── slack_notify.py      # Slack webhook notification
├── AGENTS.md                # Codex instructions for this repo
├── .env                     # API keys
├── requirements.txt
└── README.md

## Build this step by step

### Step 1 — FastAPI webhook receiver (main.py)
- POST /webhook/sentry endpoint
- Parses Sentry error payload: error message, stack trace, 
  filename, line number, environment
- Validates the webhook secret
- Triggers the orchestrator async so webhook returns 200 immediately

### Step 2 — GitHub client (github_client.py)
- Authenticate with GITHUB_TOKEN from env
- fetch_file(repo, filepath, branch) — returns file contents as string
- create_branch(repo, branch_name) — creates fix/devloop-{timestamp} branch
- commit_patch(repo, branch, filepath, new_content, message) — commits the fix
- open_pr(repo, branch, title, body) — opens PR with structured description
  PR body must include sections:
  ## Error Summary
  ## Root Cause  
  ## Fix Applied
  ## Test Results

### Step 3 — Codex client (codex_client.py)
- authenticate with OPENAI_API_KEY
- Function: generate_fix(error_message, stack_trace, file_content, filename)
- Sends this exact prompt structure to codex-mini-latest via 
  OpenAI Responses API:

  System: You are an expert software engineer. You will be given a 
  production error and the file where it occurred. Your job is to:
  1. Identify the root cause
  2. Write a minimal, safe fix
  3. Return ONLY the complete corrected file content
  4. Also return a brief root cause explanation (2-3 sentences)
  
  Respond in this exact JSON format:
  {
    "fixed_code": "...complete file content...",
    "root_cause": "...explanation...",
    "fix_summary": "...what you changed and why..."
  }

  User: 
  Error: {error_message}
  Stack trace: {stack_trace}
  File ({filename}): {file_content}

- Parse the JSON response and return structured result
- Handle API errors gracefully with retries (max 3)

### Step 4 — Sandbox runner (sandbox_runner.py)
- Spin up a Docker container using python:3.11-slim image
- Write the patched file into a temp directory
- Run the test command (pytest by default, configurable)
- Capture stdout, stderr, exit code
- Return: { passed: bool, output: string, duration: float }
- Cleanup container after run
- Timeout after 120 seconds

### Step 5 — Orchestrator (orchestrator.py)
Connect everything in this exact sequence:
1. Receive parsed Sentry error from webhook
2. Log: "DevLoop triggered for error: {error_message}"
3. Fetch the affected file from GitHub using github_client
4. Call codex_client.generate_fix() with error + file content
5. Create a new branch: fix/devloop-{timestamp}
6. Write patched file to temp location
7. Run sandbox_runner — execute tests against the patch
8. Commit the patch to the new branch
9. Open PR with full context including test results
10. Send Slack notification with PR link
11. Log entire run to devloop_runs.json for dashboard

### Step 6 — Slack notification (slack_notify.py)
- POST to SLACK_WEBHOOK_URL
- Message format:
  🔧 *DevLoop fixed a bug*
  *Error:* {error_message}
  *File:* {filename}
  *Tests:* ✅ Passing / ❌ Failing
  *PR:* {pr_url}
  _Review and merge when ready._

### Step 7 — AGENTS.md
Create this file so Codex understands the repo conventions:
- Language and framework being used
- Test command to run
- Code style rules (keep fixes minimal, don't refactor)
- Branch naming convention
- What NOT to change (config files, migrations, etc)

### Step 8 — Environment variables needed
SENTRY_WEBHOOK_SECRET=
OPENAI_API_KEY=
GITHUB_TOKEN=
GITHUB_REPO=owner/reponame
GITHUB_BASE_BRANCH=main
SLACK_WEBHOOK_URL=
TEST_COMMAND=pytest
DOCKER_IMAGE=python:3.11-slim

### Step 9 — Requirements.txt
fastapi
uvicorn
openai
PyGithub
docker
python-dotenv
requests
pydantic

### Step 10 — README.md
Write a clear README with:
- What DevLoop does (3 lines)
- Setup instructions
- How to connect Sentry webhook
- How to test locally by sending a mock Sentry payload
- Example PR output

## Important rules for the build
- Every function must have error handling with descriptive logs
- No hardcoded values — everything from .env
- The webhook must return 200 immediately, all processing is async
- Codex prompt must ask for JSON only — no markdown, no explanation outside JSON
- Docker sandbox must always cleanup even if tests fail
- PR must never be opened if Codex returns an error
- Keep the fix minimal — Codex should change as few lines as possible

## Test it works with this mock payload
Create a file mock_sentry_payload.json that simulates a real 
Sentry webhook for a Python TypeError, so we can test 
the full pipeline locally without a real Sentry account.

Build the complete working code for all files now.