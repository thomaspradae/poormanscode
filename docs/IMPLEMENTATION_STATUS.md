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
- [ ] Do not enable an OpenHands production candidate until a remote Agent Server
  is deployed behind an enforceable workspace/network boundary and the candidate
  passes a real file-edit, test, timeout, and cleanup conformance canary.
- [ ] Add per-model-request accounting callbacks for OpenHands. Its current SDK
  metrics are explicitly recorded as aggregate rather than fake request-level
  precision.

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
