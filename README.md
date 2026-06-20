# microservice-ai-repair-bot

## Uploaded-log + external GitHub orchestrator

The Python project is now the repair control plane. A human operator uploads an
error log and supplies an external GitHub repository URL through the FastAPI
backend. Each repository is prepared under the ignored path
`.workspace/runs/{run_id}/repo/`; the toy Java services in this repository are
fixtures only.

The API scaffold exposes:

- `POST /api/repair-runs` for multipart log uploads and repository metadata
- `POST /api/demo/runs` for a complete JSON, network-free demonstration run
- `GET /api/health` for storage health and active integration modes
- `GET /api/repair-runs` and `GET /api/repair-runs/{run_id}` for status
- `POST /api/repair-runs/{run_id}/approve-plan` and `/cancel`
- `GET /api/incidents` and `GET /api/incidents/{incident_id}`

Start it locally with `python -m uvicorn repairbot.api.app:app --reload`. The
default runtime uses in-memory persistence plus fake GitHub, Jira, patch, and
test clients. Set both `MONGODB_URI` and `MONGODB_DB` to select the Motor-backed
repository. Real GitHub, Jira, and OpenAI paths require environment credentials
plus explicit configuration/runtime authorization; default tests never call
external services.

## Backend-only MVP demo

Install the backend dependencies and start FastAPI:

```powershell
python -m pip install -e ".[dev]"
python -m uvicorn repairbot.api.app:app --reload
```

In another terminal, run a complete local CLI demonstration. The command reads
the checked-in sample log, executes all workflow stages with deterministic
adapters, and prints the run ID, incident, proposed solution, ticket/PR links,
effective model settings, and local trace placeholder:

```powershell
python -m repairbot.cli demo-run `
  --sample user-service `
  --repo-url https://github.com/Rkb301/user-service
```

The available samples and repository metadata URLs are:

- `user-service` — `https://github.com/Rkb301/user-service`
- `order-service` — `https://github.com/Rkb301/order-service`
- `payment-service` — `https://github.com/Rkb301/payment-service`

Run the same complete flow through the JSON API with PowerShell:

```powershell
$logText = Get-Content -Raw examples\demo-logs\user-service-missing-profile.log
$body = @{
  log_text = $logText
  github_repo_url = "https://github.com/Rkb301/user-service"
  service_name = "user-service"
  target_branch = "main"
} | ConvertTo-Json

$run = Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/api/demo/runs `
  -ContentType "application/json" `
  -Body $body
$run | ConvertTo-Json -Depth 10
```

Or use curl after replacing the escaped log text as needed:

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/demo/runs `
  -H "Content-Type: application/json" `
  -d '{"log_text":"ERROR user-service demo\njava.lang.NullPointerException: profile missing","github_repo_url":"https://github.com/Rkb301/user-service","service_name":"user-service","target_branch":"main"}'
```

Local mode records real-shaped data but performs no external Jira, GitHub,
OpenAI, MongoDB, or LangSmith calls. Real adapters remain credential-gated and
disabled by default. `GET /api/repair-runs/{run_id}` returns the same run,
incident, agent, ticket, PR, effective-config, trace, and metadata fields a
future operator interface can display. No frontend is included or claimed as
complete in this pass.

## Production storage and Jira setup

| Capability | Default | Production status |
| --- | --- | --- |
| In-memory storage | Enabled for local runs | Process-local and network-free |
| MongoDB storage | Disabled until both environment settings exist | Real Motor persistence with ping, indexes, redacted writes, and shutdown handling |
| Fake Jira | Enabled by `jira.mode: fake` | Deterministic local ticket metadata |
| Real Jira | Disabled until `jira.mode: real` and credentials are configured | Jira Cloud v3 issue creation, health check, and PR-link comments |
| Fake GitHub PR | Enabled locally | Deterministic PR metadata |
| Real GitHub PR | Disabled until explicitly selected and authorized | Real clone, repair branch, controlled commit, push, and REST PR creation |
| Patch/test execution | Fake for local demos | Reviewed three-service patch adapter and shell-free allowlisted test runner in real mode |
| OpenAI agents | Heuristic mode by default | Exactly three structured calls when `planner_mode: openai`, `--allow-api`, and `OPENAI_API_KEY` are present |
| LangSmith | Disabled by default | Configuration and local trace metadata are available; live verification remains optional |

Credentials belong only in environment variables or an ignored local `.env`.
Never place MongoDB credentials, Jira email/token values, or other secrets in
`repairbot.yaml`; the YAML loader rejects secret-looking keys and values.

### MongoDB

Configure a database account with read/write access to one database:

```powershell
$env:MONGODB_URI = "mongodb+srv://<user>:<password>@<cluster>/"
$env:MONGODB_DB = "repairbot"
python -m repairbot.cli storage-check
```

`storage-check` pings the selected database and initializes 19 named indexes:
nine unique stable-ID indexes and ten status/run/incident lookup indexes. Ticket
and pull-request records are indexed by both `incident_id` and `run_id`. It
prints the database name but never the connection URI. When both variables are
absent, the command reports the in-memory default. A partial configuration
fails closed.

Canonical Jira `title`, `description`, and `solution` values are stored in full
after secret redaction. They are not display previews and are not silently
shortened. To verify one stored ticket in `mongosh`, select the configured
database and run:

```javascript
const runId = "run_replace_me";
const ticket = db.jira_tickets.findOne(
  {run_id: runId},
  {_id: 0, run_id: 1, incident_id: 1, jira_key: 1, title: 1, description: 1, solution: 1}
);
printjson({
  ...ticket,
  solution_length: ticket?.solution?.length ?? 0,
  solution_end_excerpt: ticket?.solution?.slice(-200) ?? ""
});
```

The API uses MongoDB automatically only when both settings exist. Startup pings
the database and initializes indexes before serving requests. Check runtime
health without exposing credentials:

```powershell
python -m uvicorn repairbot.api.app:app
Invoke-RestMethod http://127.0.0.1:8000/api/health | ConvertTo-Json -Depth 5
```

The optional live test is disabled unless the flag and both database settings
are present. It creates a uniquely named temporary database and removes it at
the end when practical:

```powershell
$env:RUN_MONGODB_INTEGRATION_TESTS = "1"
python -m pytest -q tests/integration/test_mongodb_integration.py
```

### Jira Cloud

Set the Jira Cloud base URL and credentials only in the environment. The base
URL must be credential-free HTTPS:

```powershell
$env:JIRA_BASE_URL = "https://your-domain.atlassian.net"
$env:JIRA_EMAIL = "engineer@example.com"
$env:JIRA_API_TOKEN = "<jira-api-token>"
$env:JIRA_PROJECT_KEY = "DEMO"
python -m repairbot.cli jira-check
```

The default `jira-check` validates configuration without making a request. Use
the explicit network flag to call Jira Cloud `/rest/api/3/myself`:

```powershell
python -m repairbot.cli jira-check --allow-network
```

Set `jira.mode: real` in reviewed, secret-free YAML to select the real adapter.
Issue descriptions use Jira Cloud v3 document format and include the error,
proposed solution, affected service, repository URL, incident ID, and run ID.
When `jira.update_pr_link` is enabled, the workflow posts `Pull request: <url>`
as a Jira comment and avoids duplicate comments within the running process.

The optional Jira integration test can create a real issue and comment, so it
requires both the explicit flag and a dedicated test project:

```powershell
$env:JIRA_TEST_PROJECT_KEY = "DEMO"
$env:RUN_JIRA_INTEGRATION_TESTS = "1"
python -m pytest -q tests/integration/test_jira_integration.py
```

The test does not delete the issue because project policy may prohibit deletion.
Keep the flag unset for normal offline test runs.

### Real GitHub repair execution

Real GitHub mode requires a token in the environment and two explicit runtime
choices: real mode and a network allowance. Tokens are passed to Git and the
GitHub REST client without appearing in command arguments or output.

```powershell
$env:GITHUB_TOKEN = "<GitHub token from your secret manager>"
```

GitHub REST calls use Bearer authentication. Git clone, `ls-remote`, and push
use an in-memory HTTP Basic header with `x-access-token` as the username; the
credential helper is disabled for those subprocesses and the clean repository
URL remains in `origin`. Check configuration without network access, then opt
in to a read-only live REST and Git transport check:

```powershell
python -m repairbot.cli github-check
python -m repairbot.cli github-check --allow-network --repo-url https://github.com/Rkb301/user-service.git
```

Use dry-run first. It clones the repository, verifies a clean workspace, creates
a `bugfix/<JIRA_KEY>-<service>-<error>` branch, applies a reviewed deterministic
patch, and runs the allowlisted test command. It does not commit, push, or
create a PR:

```powershell
python -m repairbot.cli repair-run `
  --log-file examples/demo-logs/user-service-missing-profile.log `
  --repo-url https://github.com/Rkb301/user-service.git `
  --service-name user-service `
  --target-branch main `
  --real-git `
  --allow-network `
  --dry-run
```

Add real Jira to the same real-Git workflow only with explicit credentials,
network authorization, and a reviewed project key. Jira issue creation happens
before branch naming; a non-dry successful PR is linked back to that issue:

```powershell
python -m repairbot.cli repair-run `
  --log-file examples/demo-logs/user-service-missing-profile.log `
  --repo-url https://github.com/Rkb301/user-service.git `
  --service-name user-service `
  --target-branch demo-bug/user-service-missing-profile `
  --real-git `
  --real-jira `
  --jira-project BGSMFWRJ `
  --allow-network `
  --dry-run
```

`--dry-run` suppresses Git commit, push, and PR creation, but `--real-jira`
still creates the explicitly requested Jira issue so its key can name the local
repair branch.

To use the actual three-agent OpenAI path in an otherwise dry repair run, add
both the planner selection and the separate API allowance. `--allow-network`
does not authorize model calls:

```powershell
$env:OPENAI_API_KEY = "<OpenAI key from your secret manager>"
python -m repairbot.cli repair-run `
  --log-file examples/demo-logs/user-service-missing-profile.log `
  --repo-url https://github.com/Rkb301/user-service.git `
  --service-name user-service `
  --target-branch demo-bug/user-service-missing-profile `
  --real-git `
  --real-jira `
  --jira-project KAN `
  --planner-mode openai `
  --allow-network `
  --allow-api `
  --dry-run
```

This path calls `LogAnalysisAgent`, `RepairPlanningAgent`, and `ReviewPrAgent`
once each. The selected model analyzes and reviews; the source edit still comes
only from the reviewed deterministic patch adapter. Unsupported repository
shapes fail closed or return an already-fixed/no-change result.

### Preparing local demo-bug branches

Main branches may already contain the reviewed fixes. The following commands
create intentional local bug branches from clean service checkouts. They do not
push anything. Set `$repairbotRoot` to this repository first:

```powershell
$repairbotRoot = "C:\path\to\microservice-ai-repair-bot"
```

Run inside a clean `user-service` checkout:

```powershell
git switch main
git pull --ff-only
git switch -c demo-bug/user-service-missing-profile
git apply "$repairbotRoot\examples\demo-bug-patches\user-service-missing-profile.patch"
git add src/main/java/com/example/user/UserServiceApp.java
git commit -m "demo: restore missing-profile failure"
```

Run inside a clean `order-service` checkout:

```powershell
git switch main
git pull --ff-only
git switch -c demo-bug/order-service-null-customer
git apply "$repairbotRoot\examples\demo-bug-patches\order-service-null-customer.patch"
git add src/main/java/com/example/order/OrderServiceApp.java
git commit -m "demo: restore null-customer failure"
```

Run inside a clean `payment-service` checkout:

```powershell
git switch main
git pull --ff-only
git switch -c demo-bug/payment-service-invalid-charge
git apply "$repairbotRoot\examples\demo-bug-patches\payment-service-invalid-charge.patch"
git add src/main/java/com/example/payment/PaymentServiceApp.java
git commit -m "demo: restore invalid-charge failure"
```

Review each branch and run its failing test before any optional manual push.
Repair demos should pass the matching `demo-bug/...` value to `--target-branch`.
The repair bot never creates or pushes these demo branches automatically.

Remove `--dry-run` only when a real branch push and PR are intended:

```powershell
python -m repairbot.cli repair-run `
  --log-file examples/demo-logs/user-service-missing-profile.log `
  --repo-url https://github.com/Rkb301/user-service.git `
  --service-name user-service `
  --target-branch main `
  --real-git `
  --allow-network
```

The same runtime controls are available to multipart API requests as
`github_mode=real`, `execution_mode=real`, `allow_network=true`, and
`dry_run=true|false`. Repo-local YAML may select real modes, but it must never
contain credentials. A runtime allowance is still required.

Production safety rules:

- Only credential-free `https://github.com/<owner>/<repo>` URLs are accepted.
- Only generated `ai-fix/...` or `bugfix/...` branches may be pushed.
- Protected branches are never push targets.
- Real patching begins only from a clean clone.
- The controlled patch adapter currently supports the three MVP Java services.
- Only reviewed Maven, Gradle, or pytest commands run, always with `shell=False`.
- Tests must pass before commit, push, or PR creation.
- Failed tests retain run evidence but do not create a PR.
- Patch paths must stay inside the clone and cannot target secrets or deployment files.

The optional live GitHub test is disabled unless every setting and the explicit
flag are present. It creates a unique repair branch and PR in the configured
test repository:

```powershell
$env:GITHUB_TEST_REPO_URL = "https://github.com/<owner>/<dedicated-test-repo>"
$env:GITHUB_TEST_BASE_BRANCH = "main"
$env:RUN_GITHUB_INTEGRATION_TESTS = "1"
python -m pytest -q tests/integration/test_github_integration.py
```

The live test does not close or delete the PR automatically. Keep the flag unset
for normal validation.

### OpenAI planner verification

The real structured planner check requires `langchain-openai`, an environment
key, and an explicit API allowance. The default command is explanatory and
performs no request:

```powershell
python -m repairbot.cli ai-planner-check
```

Run one small structured-output request only when API use is intended:

```powershell
$env:OPENAI_API_KEY = "<OpenAI key from your secret manager>"
python -m repairbot.cli ai-planner-check --allow-api
```

The command reports the provider/model, confirms one API invocation, confirms
all three templates rendered, and prints only the bounded parsed check result.
It does not print the API key or raw provider payload.

The uploaded-log workflow is orchestrated by LangGraph. LangChain provides the
structured planning and tool-adapter layer, while LangSmith configuration adds
trace metadata when explicitly enabled with credentials. LangSmith observes the
flow; it does not execute repairs.

## Project-aligned MVP boundaries

The MVP is deliberately capped at **four repositories total**: this Python/AI
orchestrator plus **at most three external microservice repositories**. The
product-facing example registry is `services.mvp-review.example.yaml`.

The product has exactly three agents. In `planner_mode: openai`, each is backed
by one structured call to the configured OpenAI model:

1. **LogAnalysisAgent** — turns a redacted log excerpt into structured evidence.
2. **RepairPlanningAgent** — produces hypotheses, a selected plan, validation strategy, risks, and Jira-ready solution text.
3. **ReviewPrAgent** — produces a bounded PR title/summary/checklist and Jira comment from the actual patch and test result.

LangGraph still owns clone/config, deterministic patch selection/execution,
tests, persistence, and finalization; those nodes are not extra agents. The
one-model MVP uses **GPT-5 mini** (`openai:gpt-5-mini`) and does not perform
multi-model voting. A future enterprise design could evaluate five model
candidates and select the best solution; that capability is not implemented.

Jira records contain a title, description, and proposed solution. After a PR is
created, its URL is written back into Jira. MongoDB/in-memory records retain the
error, solution, Jira URL, PR URL, target GitHub repo/branch, plan, status, and
LangSmith trace metadata.

The future UI is a human operator console—not a pipeline diagram. The existing
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
in repository YAML—the loader rejects secret-looking data.

Safe YAML controls the selected allowlisted model, agent mode and prompts,
hypothesis/attempt limits,
Jira project and issue type, Jira title/description/solution templates, GitHub
base branch and PR behavior, execution mode, test command,
build/language/module settings, approval policy, and LangSmith project/tags.
Test commands are validated against a reviewed Maven/Gradle/pytest allowlist and
run without a shell; unit coverage uses only isolated local pytest fixtures.

The MVP default remains one active model, `openai:gpt-5-mini`. Configuration can
select another model only when that model is explicitly listed in
`model.allowed_models`; this does not implement multi-model voting.

The `prompts:` pack has a bounded, non-empty system and human template for each
agent plus a version and `planner_mode`. Old YAML without this section remains
valid. Repo-local values override orchestrator defaults. Secret-looking prompt
content is rejected and rendered values are redacted and bounded before model
invocation or persistence.

Inspect the effective pack without making any API call:

```powershell
python -m repairbot.cli prompt-config
python -m repairbot.cli prompt-config --format json
python -m repairbot.cli prompt-config --repo-path C:\path\to\service
python -m repairbot.cli prompt-config --show-rendered-sample
```

Run records expose the planner mode, provider/model, exact agent names/count,
whether an OpenAI request was invoked, LangChain/LangGraph/LangSmith flags,
prompt version/names, bounded redacted rendered inputs, and the three concise
structured outputs. Local mode reports OpenAI and LangChain as unused rather
than inferring use from the configured model name. LangSmith is active only
when YAML enables it, a key exists, and LangSmith tracing is enabled in the
environment; otherwise the run exposes its local trace reference.

`jira.mode` and `github.mode` are explicit `fake` or `real` settings. Fake mode
is the local/test default. Real adapters require
`JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, or `GITHUB_TOKEN` from the
environment, fail closed when credentials are absent, and remain blocked until
the matching runtime allowance is supplied.

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
- Includes a three-service MVP registry; local compatibility fixtures are not product repositories.
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

- OpenAI agent mode requires `--allow-api`, `OPENAI_API_KEY`, and an allowlisted `openai:` model; heuristic mode remains network-free.
- Source patch generation is intentionally not delegated to a model; only the three reviewed Java scenarios are supported.
- `jira-run` intentionally processes fixes one-by-one and prints fix commands instead of silently mutating multiple repos.
- Root-cause inference is deterministic and intentionally conservative.
- Toy services are minimal Java HTTP services, not full Spring Boot apps.
- Maven tests require Maven and network access for first dependency resolution.
- The safety scanner is a guardrail, not a substitute for a real secret-scanning product.

