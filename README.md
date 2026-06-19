# microservice-ai-repair-bot

## Uploaded-log + external GitHub orchestrator

The Python project is now the repair control plane. A human operator uploads an
error log and supplies an external GitHub repository URL through the FastAPI
backend. Each repository is prepared under the ignored path
`.workspace/runs/{run_id}/repo/`; the toy Java services in this repository are
fixtures only.

The API scaffold exposes:

- `POST /api/repair-runs` for multipart log uploads and repository metadata
- `GET /api/repair-runs` and `GET /api/repair-runs/{run_id}` for status
- `POST /api/repair-runs/{run_id}/approve-plan` and `/cancel`
- `GET /api/incidents` and `GET /api/incidents/{incident_id}`

Start it locally with `python -m uvicorn repairbot.api.app:app --reload`. The
default runtime uses in-memory persistence plus fake GitHub, Jira, patch, and
test clients. Set both `MONGODB_URI` and `MONGODB_DB` to select the Motor-backed
repository. Real GitHub/Jira/LLM adapters require explicit production wiring;
tests never call external services.

The uploaded-log workflow is orchestrated by LangGraph. LangChain provides the
structured planning and tool-adapter layer, while LangSmith configuration adds
trace metadata when explicitly enabled with credentials. LangSmith observes the
flow; it does not execute repairs.

## Project-aligned MVP boundaries

The MVP is deliberately capped at **four repositories total**: this Python/AI
orchestrator plus **at most three external microservice repositories**. The
product-facing example registry is `services.mvp-review.example.yaml`.

The product has exactly three conceptual agents:

1. **Log Analysis Agent** â€” turns the uploaded log into a structured incident.
2. **Repair Planning Agent** â€” produces the hypothesis, proposed solution, edits, and tests.
3. **Code Repair Agent** â€” patches the selected external repo and verifies the change.

LangGraph has additional internal workflow nodes, but those nodes are not extra
agents. The one-model MVP uses **GPT-5 mini** (`openai:gpt-5-mini`) and does not
perform multi-model voting. A future enterprise design could evaluate five model
candidates and select the best solution; that capability is not implemented.

Jira records contain a title, description, and proposed solution. After a PR is
created, its URL is written back into Jira. MongoDB/in-memory records retain the
error, solution, Jira URL, PR URL, target GitHub repo/branch, plan, status, and
LangSmith trace metadata.

The future UI is a human operator consoleâ€”not a pipeline diagram. The existing
API exposes the fields needed to upload a log, select a GitHub repo, review and
approve a plan, monitor status, and open Jira, PR, or LangSmith links.

## Safe YAML configuration

Repair behavior is configured without Python edits. The orchestrator ships with
the secret-free `repairbot.default.yaml`; a cloned target repo can override it
with the first matching file below:

- `repairbot.yaml`
- `.repairbot.yaml`
- `repairbot.yml`
- `.repairbot.yml`

The merge precedence is: explicit operator/API/CLI values, repo-local config,
orchestrator default YAML, non-secret environment defaults, then hardcoded
fallbacks. Environment variables always remain the only source for credentials.
Never put API keys, tokens, passwords, private keys, or credential-bearing URLs
in repository YAMLâ€”the loader rejects secret-looking data.

Safe YAML controls the selected allowlisted model, hypothesis/attempt limits,
Jira project and issue type, Jira title/description/solution templates, GitHub
base branch and PR behavior, test command, build/language/module settings,
approval policy, and LangSmith project/tags. Test commands are validated against
a reviewed Maven/Gradle/pytest allowlist and are never executed by unit tests.

The MVP default remains one active model, `openai:gpt-5-mini`. Configuration can
select another model only when that model is explicitly listed in
`model.allowed_models`; this does not implement multi-model voting.

`jira.mode` and `github.mode` are explicit `fake` or `real` settings. Fake mode
is the local/test default. Real adapters are production scaffolds, require
`JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, or `GITHUB_TOKEN` from the
environment, and fail closed when credentials are absent. Reviewed real network
contracts are intentionally not implemented in this scaffold.

Jira PR backlink updates are controlled by `jira.update_pr_link` and default to
`true`. The run-detail API returns `config_source` and the redacted
`effective_config` snapshot so the future UI can show exactly which model,
adapter modes, templates, approval rule, and test command were used.

The network-free CLI also accepts an optional safe defaults file:

```powershell
python -m repairbot.cli uploaded-log-repair `
  --log-file error.log `
  --repo-url https://github.com/example/order-service `
  --config repairbot.default.yaml
```

Python repair-orchestration control plane with a retained CLI prototype for
Jira-driven Java microservice repair. This is a backend scaffold, not a finished
operator product.

The system ingests realistic mixed logs, streams/parses multiline Java stack traces, clusters errors deterministically, stores runtime artifacts in SQLite/JSON, creates Jira bugs, and processes Jira tickets one at a time toward `bugfix/{JIRA_KEY}` branches and human-reviewed GitHub PRs.

No UI is included.

`jira-fix` uses an explicit repair loop:

```text
Observe -> Plan -> Hypothesize -> Select -> Patch -> Test -> Reflect -> Retry
```

The planning and reflection files are structured, user-readable summaries. They are not raw private chain-of-thought.

## What It Does

- Accepts redacted uploaded logs plus external GitHub repository URLs through FastAPI.
- Persists platform runs through an async in-memory/MongoDB repository boundary.
- Orchestrates fake-by-default incident, plan, approval, patch, test, and PR stages with LangGraph.
- Preserves the original `diagnose` and `repair` commands for local log-to-fix flows.
- Adds Java/Jira workflow commands: `log-summary`, `jira-analyze`, `jira-create`, `jira-fix`, `jira-run`.
- Uses SQLite at `.repairbot/repairbot.db` for local storage.
- Uses JSON artifacts under `runs/` or a path passed with `--out`.
- Implements test-time compute at runtime: streaming, parsing, clustering, hypothesis generation/ranking, code inspection budgets, test runs, patch rejection, retries, and PR creation only after passing tests.
- Includes a three-service MVP registry and four legacy local fixture directories.
- Includes a public-GitHub safety scanner and optional pre-commit hook.

## Setup

```powershell
cd C:\Users\ASUS\Documents\LangStackPrac\microservice-ai-repair-bot
python -m pip install -e ".[dev]"
```

Create a local `.env` only when you need API-backed commands:

```powershell
python -m repairbot.cli init-env
```

This copies `.env.example` to `.env` only if `.env` does not already exist. Paste keys manually into `.env`; never ask Codex or any assistant to store, print, or reveal real keys. `.env` is ignored by Git.

## Safe Test Order

Start with zero-cost local commands:

```powershell
python -m compileall -q repairbot
python -m pytest -q --basetemp=pytest_temp/run
python -m repairbot.cli safety-check --path .
python examples\traffic_generator\generate_traffic.py --services-config services.mvp-review.example.yaml --mode small --out-dir logs
python -m repairbot.cli log-summary --logs-dir logs --services-config services.mvp-review.example.yaml
python -m repairbot.cli jira-analyze --logs-dir logs --services-config services.mvp-review.example.yaml --out runs\run-001\analysis.json
python -m repairbot.cli jira-create --analysis runs\run-001\analysis.json --out runs\run-001\jira_tickets.json --dry-run
```

API-cost commands require real credentials and explicit intent:

```powershell
python -m repairbot.cli ai-smoke-test --allow-api
python -m repairbot.cli repair ... --allow-api
python -m repairbot.cli jira-fix ... --allow-api
```

`--dry-run --allow-api` can still spend API credits because the repair agent may call the configured provider. Dry-run mode does not commit, push, or open a PR. Failed dry-runs restore worktree edits by default and write debugging artifacts; use `--keep-dry-run-changes` only when you intentionally want to inspect edits in a dirty worktree. `--dry-run --allow-dirty` requires `--keep-dry-run-changes` so pre-existing edits are not restored or removed.

Planning modes:

- `jira-fix --plan-only --allow-api` may spend API credits in future provider-backed planners, but it does not edit files, run repair, commit, push, or open a PR.
- `jira-fix --plan-first --dry-run --allow-api` may spend API credits and may temporarily edit files, then restores dry-run edits by default.
- Real `jira-fix --allow-api` may edit files, run tests, and commit locally on `bugfix/{JIRA_KEY}` after tests pass.

`log-summary`, `safety-check`, `jira-analyze`, and `jira-create --dry-run` do not call OpenAI.

## Windows Pytest Temp Setup

If Windows blocks the default pytest temp directory, run:

```powershell
mkdir .tmp -ErrorAction SilentlyContinue
mkdir pytest_temp -ErrorAction SilentlyContinue
$env:TEMP=(Resolve-Path .\.tmp).Path
$env:TMP=(Resolve-Path .\.tmp).Path
python -m pytest -q --basetemp=pytest_temp/run
```

## Configure Jira

```powershell
$env:JIRA_BASE_URL="https://your-domain.atlassian.net"
$env:JIRA_EMAIL="you@example.com"
$env:JIRA_API_TOKEN="..."
$env:JIRA_PROJECT_KEY="BUG"
```

`jira-create` uses Jira Cloud REST API v3 and Basic auth with email/API token.

## Configure GitHub

```powershell
$env:GITHUB_TOKEN="..."
```

The bot opens PRs through GitHub REST only when `--open-pr` is passed. It never merges and never requests auto-merge.

## MVP model: GPT-5 mini

```powershell
$env:REPAIRBOT_PROVIDER="openai"
$env:OPENAI_API_KEY="..."
$env:REPAIRBOT_MODEL="openai:gpt-5-mini"
```

Commands that do not need AI, Jira, or GitHub credentials do not require those keys.

Validate OpenAI configuration with one explicit tiny API call:

```powershell
python -m repairbot.cli ai-smoke-test --allow-api
```

This may spend a tiny amount of API credit and never prints your API key.

## Runtime Budgets

```powershell
$env:REPAIRBOT_MAX_HYPOTHESES=5
$env:REPAIRBOT_TOP_HYPOTHESES=3
$env:REPAIRBOT_MAX_FILES_TO_READ=30
$env:REPAIRBOT_MAX_DEPENDENCY_DEPTH=3
$env:REPAIRBOT_MAX_FIX_ATTEMPTS=2
$env:REPAIRBOT_MAX_TEST_RUNS=3
$env:REPAIRBOT_MAX_LOG_EXAMPLES_PER_CLUSTER=5
```

This is test-time compute only. The prototype does not train, fine-tune, update model weights, or learn from private data.

## Java/Maven fixtures

The MVP uses these three fixture slots to represent three external repos:

- `user-service`: `GET /users/{id}`, `POST /users`, `GET /users/{id}/profile`
- `order-service`: `POST /orders`, `GET /orders/{id}`, `GET /orders/user/{userId}`
- `payment-service`: `POST /payments/charge`, `GET /payments/{id}`, `POST /payments/refund`

`inventory-service` remains only as a fourth legacy/local fixture so older CLI
tests and examples continue to work. It is not a fourth MVP microservice repo.

Each service uses in-memory toy data and includes an intentional bug.

Run tests for one service:

```powershell
cd examples\toy-java-microservices\order-service
mvn test
```

Run one service:

```powershell
mvn -q -DskipTests package
java -cp target\classes com.example.order.OrderServiceApp
```

## Generate Mixed Logs

```powershell
python examples\traffic_generator\generate_traffic.py `
  --services-config services.mvp-review.example.yaml `
  --requests 5000 `
  --error-rate 0.15 `
  --mode medium `
  --out-dir logs
```

Use `--mode small`, `--mode medium`, or `--mode large`. Add `--send-http` after starting the toy services if you want the generator to also call the HTTP APIs.

## Log Summary

```powershell
python -m repairbot.cli log-summary `
  --logs-dir logs `
  --services-config services.mvp-review.example.yaml
```

No LLM/API keys are used.

## Analyze Only

```powershell
python -m repairbot.cli jira-analyze `
  --logs-dir logs `
  --services-config services.mvp-review.example.yaml `
  --out runs\run-001\analysis.json
```

Explicit mappings are also supported:

```powershell
python -m repairbot.cli jira-analyze `
  --logs user-service=logs\user-service.log `
  --logs order-service=logs\order-service.log `
  --logs payment-service=logs\payment-service.log `
  --services-config services.mvp-review.example.yaml `
  --out runs\run-001\analysis.json
```

## Create Jira Tickets

Real Jira:

```powershell
python -m repairbot.cli jira-create `
  --analysis runs\run-001\analysis.json `
  --out runs\run-001\jira_tickets.json
```

Local dry-run records:

```powershell
python -m repairbot.cli jira-create `
  --analysis runs\run-001\analysis.json `
  --out runs\run-001\jira_tickets.json `
  --dry-run
```

Duplicate clusters are not recreated if they already exist in SQLite.

## Fix One Jira Ticket

```powershell
python -m repairbot.cli jira-fix `
  --ticket BUG-123 `
  --repo . `
  --service order-service `
  --services-config services.mvp-review.example.yaml `
  --base-branch main `
  --test-cmd "mvn test" `
  --open-pr `
  --github-repo owner/order-service `
  --allow-api
```

The branch name is `bugfix/{JIRA_KEY}`. Tests must pass before commit/push/PR. The PR body states that the change is AI-generated and requires human review.

For safe debugging of the legacy `inventory-service` fixture:

```powershell
python -m repairbot.cli jira-fix `
  --ticket BUG-123 `
  --repo . `
  --service inventory-service `
  --services-config services.yaml.example `
  --base-branch master `
  --dry-run `
  --allow-api
```

This legacy compatibility flow can call the AI provider and spend API credits,
but it does not commit, push, or open a PR. It uses the selected fixture path
from `services.yaml.example`, captures test/diff evidence, and restores dry-run
edits unless `--keep-dry-run-changes` is passed.

Plan only:

```powershell
python -m repairbot.cli jira-fix `
  --ticket BUG-123 `
  --repo . `
  --service inventory-service `
  --services-config services.yaml.example `
  --base-branch master `
  --test-cmd "mvn test" `
  --plan-only `
  --max-hypotheses 5 `
  --allow-api
```

Plan first plus dry run:

```powershell
python -m repairbot.cli jira-fix `
  --ticket BUG-123 `
  --repo . `
  --service inventory-service `
  --services-config services.yaml.example `
  --base-branch master `
  --test-cmd "mvn test" `
  --plan-first `
  --dry-run `
  --max-hypotheses 5 `
  --allow-api
```

Use `--require-plan-approval` to stop after planning unless a matching `--approve-plan <PLAN_ID>` is supplied on a later run.

## Full Sequential Pipeline

```powershell
python -m repairbot.cli jira-run `
  --logs-dir logs `
  --services-config services.mvp-review.example.yaml `
  --base-branch main `
  --mode analyze-jira-fix-pr
```

Modes:

- `analyze-only`
- `analyze-jira`
- `analyze-jira-fix-local`
- `analyze-jira-fix-pr`

The prototype prints one-by-one `jira-fix` commands for the fix stage so a human can review each ticket/repo before repair begins.

## Original Commands

```powershell
python -m repairbot.cli diagnose --repo C:\path\to\repo --service service-a --logs logs\service-a-error.log
python -m repairbot.cli repair --repo C:\path\to\repo --service service-a --logs logs\service-a-error.log --test-cmd "pytest -q" --dry-run --allow-api
```

## Safety Checks

```powershell
python -m repairbot.cli safety-check --path .
python -m repairbot.cli install-safety-hook --path .
```

The scanner checks for common OpenAI, Google, GitHub, Jira/AWS-style token leaks, private key blocks, password-like assignments, `.env` files, and large logs.

## Before Pushing To GitHub

- Run `python -m repairbot.cli safety-check --path .`
- Confirm `.env` is not tracked.
- Confirm generated logs are not tracked unless synthetic and small.
- Confirm no real API keys are in code/config.
- Confirm `.env.example` contains placeholders only.
- Confirm Java toy services use fake/local data only.
- Confirm GitHub branch protection is enabled for real repos.
- Never commit real production logs.

The root `.gitignore` excludes `.env*`, logs, `.repairbot/`, `.workspace/`,
`runs/`, Java build output, IDE files, key/cert files, `secrets.*`, and
`credentials.*`.

## Known Limitations

- Agent-backed Java patching still requires LangChain-compatible provider credentials.
- `jira-run` intentionally processes fixes one-by-one and prints fix commands instead of silently mutating multiple repos.
- Root-cause inference is deterministic and intentionally conservative.
- Toy services are minimal Java HTTP services, not full Spring Boot apps.
- Maven tests require Maven and network access for first dependency resolution.
- The safety scanner is a guardrail, not a substitute for a real secret-scanning product.

