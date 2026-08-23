# Implementation status

PMC already implements the schema/persistence contract, operator CLI, isolated Git
worktrees, deterministic verification, retries with failure evidence, scheduling,
audit reports, human acceptance, leases, and Bash/OpenHands/Jules adapters.

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
