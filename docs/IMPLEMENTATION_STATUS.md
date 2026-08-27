# Implementation status

PMC already implements the schema/persistence contract, operator CLI, isolated Git
worktrees, deterministic verification, retries with failure evidence, scheduling,
audit reports, human acceptance, leases, and Bash/OpenHands/Jules adapters.

## Autonomous programs

- [x] A reviewed 2-50 node program DAG can run with an approved concurrency ceiling.
  Independent ready nodes use ordinary PMC job/resource/provider leases and isolated
  worktrees.
- [x] Nonterminal verifier-green jobs become `VERIFIED_FOR_CHAINING` with attributable
  internal commits while remaining `READY_FOR_REVIEW`; no fake human feedback or
  `ACCEPTED` state is created.
- [x] Program creation pins the exact repository SHA. Dependency commits are assembled
  topologically, and `pmc program-diff` shows the full result from the immutable base.
- [x] A restartable user-systemd runner supports status, pause/drain, resume after
  repair, cancel, expired-lease recovery, deadlock detection, and bounded parallelism.
- [x] A blocked branch halts new dispatch while active siblings drain. Git conflicts
  block with evidence instead of being guessed through.
- [x] The single terminal integration task is never internally promoted. Only
  `pmc program-accept` records human acceptance of the complete program result.

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
- [x] `pmc context-profile` aggregates version-specific request p50/p90 sizes,
  size-bucket 429 probability, condensation outcomes, progress, and verification.
  Configured thresholds remain explicitly labelled initial hypotheses until at
  least 50 requests and five attempts exist; PMC does not silently auto-tune them.
- [x] Failed remote OpenHands runs retrieve and apply their partial Git patch before
  classification. PMC writes a versioned, model-independent work-state packet with
  task, acceptance, baseline, changed files/diff, current plan, latest exact failure,
  and blockers; alternate candidates receive this instead of raw agent history.
- [x] Per-lane observed token remainder/reset state gates the next physical request.
  If no credential can currently fit it, PMC waits within policy or records
  `MODEL_REQUEST_DEFERRED_QUOTA` without burning a doomed provider call.
- [x] `pmc provider-capacity` reports configured credentials, explicit/shared scope
  IDs, unknown scopes, and counter-based independence evidence separately. Observed
  similarity/independence never silently becomes a confirmed quota-scope identity.
- [x] `pmc credentials probe [PROVIDER...]` performs cheap per-lane authentication
  warm-up independently of candidate qualification. It reserves and reconciles the
  exact lane, persists sanitized latency/quota/reset evidence and append-only events,
  and transitions success/401/403/429/transient failures to the appropriate health
  state without writing model-quality history. Numbered keys added to `secrets.env`
  are discovered as credential lanes automatically, and known-available lanes are
  preferred over unknown lanes during ordinary selection.
- [x] Tool schemas are included in reservations. Requests that exceed an explicitly
  configured sustainable lane size are rejected before provider dispatch, after
  OpenHands has had an opportunity to condense through its configured token trigger.
- [x] `pmc models conformance NAME` runs a cumulative L0 generation → L1 inspect
  → L2 edit → L3 failing-test repair → L4 Controller/verifier/audit gate.
  Nemotron Super + OpenHands passed all five levels and is the first enabled
  general-purpose OpenHands production candidate.
- [~] Live 2026-08-26 qualification: Mistral Medium rotated across seven strongly
  observed counter lanes with zero 429s, inspected the repo, and produced a useful
  two-file partial patch, but did not complete tests within the fixed 12-turn
  qualification budget. Groq OSS 120B used three strongly observed counter lanes;
  TPM-aware deferral reduced physical 429s from 13/26 to 0/13, it repaired the
  failing tests after two condensations, but omitted the inspection artifact and
  therefore failed L4. Both remain quarantined as general coders while retaining
  evidence as limited candidates. Local Qwen remains below the L2 gate.

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
