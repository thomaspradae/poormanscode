# Implementation status

PMC already implements the schema/persistence contract, operator CLI, isolated Git
worktrees, deterministic verification, retries with failure evidence, scheduling,
audit reports, human acceptance, leases, and Bash/OpenHands/Jules adapters.

## Required before the first untrusted real job

- [ ] Provision a dedicated `pmc-worker` account on each worker host.
- [ ] Require `sandbox = "bwrap"` for Bash candidates and validate UID, filesystem,
  network, CPU, memory, timeout, and process-group behavior with an adversarial job.
- [ ] Configure provider credentials outside repositories and verify child commands
  receive none of them. The HTTP client may read only the selected provider key.
- [ ] Run `pmc doctor` successfully on the selected worker.
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
