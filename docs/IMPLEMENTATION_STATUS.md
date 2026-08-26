# Implementation status

PMC already implements the schema/persistence contract, operator CLI, isolated Git
worktrees, deterministic verification, retries with failure evidence, scheduling,
audit reports, human acceptance, leases, and Bash/OpenHands/Jules adapters.

## Executor policy

- [x] Treat BashExecutor as a narrow executor for small, deterministic work. By
  default it is ineligible for difficult jobs and FEATURE, ARCHITECTURAL,
  DEPENDENCY_API, INTEGRATION, and UI tasks. A candidate must opt in with
  `allow_complex_tasks = true` for a controlled experiment.
- [x] Use a mature coding-agent runtime for general software engineering. Jules is
  production-available for GitHub-backed jobs. OpenHands SDK 1.43 is installed and
  the adapter uses bounded iterations plus native stuck detection.
- [x] OpenHands Agent Server 1.43 runs as a dedicated identity in an ofi1 LXD
  boundary. The remote workspace root is aligned with PMC's uploaded repository;
  the previous default `workspace/project` mismatch is fixed.
- [x] Built-in OpenHands uses an ephemeral, authenticated, Tailscale-only PMC
  provider gateway. Provider credentials remain controller-side; every physical
  model request reserves/reconciles one ProviderPool lane and records tokens,
  request ID, rate headers, cost when known, failures, and bounded cooldown waits.
- [x] OpenHands context management is explicit rather than dependent on model-map
  defaults. SDK 1.43's `LLMSummarizingCondenser` is retained, with candidate-level
  event/token triggers and the same controller-side credential boundary.
- [x] Every OpenHands request now has a content-free token X-ray: message and tool
  counts, component estimates, request hash/growth, unchanged retries, condenser
  count, actual-vs-estimated usage, context occupancy, quota lane, headers, and
  wall time. `pmc context-xray JOB --composition` renders it.
- [x] Tool schemas are included in reservations. Requests that exceed an explicitly
  configured sustainable lane size are rejected before provider dispatch, after
  OpenHands has had an opportunity to condense through its configured token trigger.
- [x] `pmc models conformance NAME` runs a cumulative L0 generation → L1 inspect
  → L2 edit → L3 failing-test repair → L4 Controller/verifier/audit gate.
  Nemotron Super + OpenHands passed all five levels and is the first enabled
  general-purpose OpenHands production candidate.
- [~] Mistral Medium + OpenHands passes standalone L3 and rotates across all three
  credential lanes, but remains quarantined because its full Controller prompt
  exceeded the aggregate TPM pool before convergence. Groq OSS 120B reached real
  OpenHands turns but remains gated by its single lane's 8k TPM window. Local Qwen
  2.5 Coder 7B generated text but made no L2 tool action in 138 seconds.

## P0 substrate

- [x] Provision a dedicated `pmc-worker` identity for the restricted-user backend.
- [x] Harden and validate `sandbox = "bwrap"`: controller home invisibility,
  no-network namespace, CPU/memory/process limits, continuous workspace limits,
  timeout, and complete process-tree termination.
- [x] Keep provider credentials controller-side and prove child commands receive none.
- [x] Validate the complete lifecycle with an all-Bubblewrap disposable canary and
  reconstruct tokens, quota reconciliation, leases, human attribution, events, and artifacts.
- [x] Exercise SIGKILL/restart recovery across every durable transition.

## Operational activation before project #1

- [x] Expose the selected provider credential by environment-variable name and run
  `pmc doctor` successfully with the sandboxed Groq Bash candidate.
- [ ] Run one supervised real ticket through submit, inspect/diff, reject or accept,
  and confirm all attempt and human-decision rows exist.

## Data-gated work

- [ ] Run 20–50 real tickets across multiple repositories before changing the
  scheduler beyond its current deliberately simple exploration policy.
- [ ] Add task decomposition only after a logged single-attempt failure demonstrates
  the need.
- [ ] Formalize provider quota events when live 429 data shows which headers and reset
  semantics are reliable.
- [ ] Add GitHub issue/PR automation after local acceptance is operationally trusted.

Use `pmc efficiency` for the two headline measures: human-accepted success rate and
median wall-clock time to a human-accepted result.
