# Poor Man's Code (PMC)

PMC is a control plane for turning a repository + ticket into a verified candidate commit while spending the cheapest useful intelligence available.

It deliberately does **not** implement one giant coding agent. Executors are replaceable. The controller owns jobs, Git worktrees, quota/resource policy, verification, retry/escalation, human feedback, and the long-run dataset used to learn what actually works.

## What is implemented

- SQLite job/attempt/verification/history database in WAL mode
- Git worktree isolation and controller-owned commits
- `Executor` boundary
- `BashExecutor`: a small mini-SWE-style shell loop using any OpenAI-compatible model endpoint
- `OpenHandsExecutor`: local OpenHands SDK execution, plus a remote Agent Server path
  with controller-side per-request credential pooling
- `JulesExecutor`: Jules REST session creation, polling, ChangeSet retrieval, and patch application
- deterministic verifier: test/lint/typecheck/build/hidden-test hooks, patch/file budgets, protected paths, secret scan
- epsilon-greedy production exploration so alternatives continue receiving real jobs
- empirical per-candidate statistics from real attempts
- retry/escalation with verifier failures fed into the next attempt
- optional independent reviewer candidate
- CLI for submit/run/status/inspect/accept/reject/stats/export/doctor
- reviewed Foreman plans, dependency-aware feature DAGs, and a Kanban-style board
- repo contract: `AGENTS.md` + `poorman.yaml`

## Install

```bash
cd poormans-code
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

For OpenHands:

```bash
pip install -e '.[openhands]'
```

## First setup

```bash
pmc init-config
$EDITOR ~/.config/poormans-code/config.toml

cd /path/to/your/repo
pmc init-repo
```

Then submit a real ticket:

```bash
pmc submit . "Make injuries persist between matches" \
  --acceptance "injury survives save/load" \
  --acceptance "old saves still load" \
  --run
```

For a larger feature, propose and approve a dependency plan before dispatch:

```bash
pmc feature-create . "Playable prototype" "Build the first playable prototype"
pmc feature-plan PMCF-000001 --candidate groq-oss20-bash
pmc feature-approve PMCF-000001
pmc board PMCF-000001
pmc daemon
```

See `docs/FOREMAN.md` for the reviewed JSON plan format and dependency semantics.

For a long-running, low-touch build, use an autonomous program. Intermediate
verifier-green jobs can feed dependent work without being mislabeled as human
accepted; the terminal integration result remains human-gated:

```bash
pmc program-create . "V0 laboratory" --spec docs/v0-build-spec.md --workers 3
pmc program-plan PMCF-000001 --file program-plan.json
pmc program-approve PMCF-000001
pmc program-run PMCF-000001 --detach
pmc program-status PMCF-000001
```

See `docs/PROGRAMS.md` for state semantics, recovery behavior, and intervention
commands.

Inspect and accept only after PMC's verifier is green:

```bash
pmc inspect PMC-000001
pmc accept PMC-000001
```

Reject with actionable feedback and rerun the same worktree:

```bash
pmc reject PMC-000001 "Duration must use calendar days, not matches"
pmc run PMC-000001
```

## Learning instead of benchmark-gating

Every attempt is logged from job #1. The scheduler uses a configurable exploration rate (default 20%). Most work goes to the current best candidate; a minority is assigned randomly across exploration-eligible candidates so PMC keeps collecting counterfactual evidence instead of permanently starving alternatives. Because the assignment mode is stored, `pmc stats --mode explore` gives you the cleanest comparison slice later instead of mixing randomized exploration with policy-selected work.

Early on the scheduler is intentionally simple. `pmc stats` is the truth. More sophisticated bandits should only be added after enough production rows exist to justify them.

## Security boundary

The controller strips secrets from child-process environments. `BashExecutor` should run with `sandbox = "bwrap"`; that backend exposes only system runtimes and the task worktree, denies network when configured, and applies process/memory plus continuously monitored workspace limits. It does not mount the controller's home. OpenHands should run in its own sandbox/Agent Server for untrusted jobs. No executor is allowed to decide that its own work passed verification.

Provider credentials may be stored as literal `KEY=VALUE` entries in
`~/.config/poormans-code/secrets.env`. PMC loads that file controller-side only and
refuses it unless it is owned by the current user with mode `600`. Values are never
copied into worktrees or child-shell environments.

## State lifecycle

`QUEUED -> RUNNING -> VERIFYING -> REVIEWING -> READY_FOR_REVIEW -> ACCEPTED`

Failure paths include `RETRY`, `BLOCKED`, `FAILED`, `CANCELLED`.

## Important design rule

No new framework enters PMC because it is fashionable. It enters only if it adds a capability the system lacks or beats an existing executor/model combination on real accepted work enough to justify the complexity.

## Evidence and audit files

For every job, PMC writes an append-style audit directory under `runs_dir/PMC-xxxxxx/` containing the job contract, exact worker prompts, attempt metadata, verification/review results, final diff, human feedback, and summary. SQLite remains the queryable source of truth; the files make individual jobs easy to inspect without database archaeology.

Useful analysis commands:

```bash
pmc stats --phase first
pmc stats --phase repair
pmc stats --mode explore
pmc export ~/pmc-attempts.csv
pmc context-xray PMC-000001 --composition
```

Before enabling a new built-in OpenHands model candidate, run the production
coding ladder:

```bash
pmc models conformance nvidia-nemotron-super-openhands
```

This proves generation, repository inspection, editing, failing-test repair, and
the complete PMC worktree/verifier/audit lifecycle. Remote OpenHands receives an
ephemeral gateway token, never a provider API key; PMC selects and reconciles a
legitimate credential lane for each physical model request.

OpenHands context behavior is explicit per candidate. PMC records a content-free
token X-ray for every physical request: estimated and provider-reported tokens,
message/tool counts, tool-schema and observation composition, context growth,
unchanged retries, condenser calls, credential lane, rate headers, and context
occupancy when the model window is configured. The X-ray stores hashes and sizes,
never prompts or credentials. Candidate settings `condenser_max_tokens`,
`condenser_max_events`, and `request_token_soft_limit` distinguish working-memory
condensation from provider throughput and cumulative job budgets.

The randomized `explore` slice is the preferred long-run comparison dataset. Raw aggregate success rates can be confounded because the scheduler deliberately sends different kinds of work to different candidates.

## OpenHands version note

The PMC core requires Python 3.11+. Current OpenHands installation documentation uses Python 3.12+ for its CLI/runtime, so use a Python 3.12+ environment on machines that will run the OpenHands executor. The adapter is intentionally isolated because the OpenHands and Jules APIs are external moving parts.


## Measure whether PMC itself is worth it

Log occasional manual control jobs instead of only comparing PMC candidates:

```bash
pmc record-manual codex "Fix the save migration bug" --seconds 240 --accepted --cost 0
pmc baseline-stats
```

This is the project-level anti-bullshit check: over time compare accepted-commit rate, wall time, and cost against the boring alternative of invoking a strong coding agent manually.
