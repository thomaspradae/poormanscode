# Autonomous programs

Programs are approved dependency graphs for long-running work. They reuse ordinary
PMC jobs, worktrees, candidates, quotas, verification, leases, and audit events.
They do not give workers authority to accept or merge their own output.

## Safety model

An intermediate program task may reach `VERIFIED_FOR_CHAINING` after its normal
PMC verifier passes. PMC then creates an attributable internal commit and may use
that commit as the baseline of dependent tasks.

`VERIFIED_FOR_CHAINING` deliberately does **not** mean `ACCEPTED`:

- the job remains `READY_FOR_REVIEW`;
- no human-feedback acceptance row is written;
- the event ledger labels the commit as internal and verifier-derived;
- the single terminal integration task is never promoted automatically;
- only `pmc program-accept` records human acceptance of the final result.

All work remains on PMC-owned program worktrees and branches until final review.

## Typical workflow

```bash
pmc program-create /path/to/repo "Football World Lab V0" \
  --spec /path/to/football-world-v0.md --workers 3

pmc program-plan PMCF-000001 --candidate <qualified-foreman-candidate>
# Or load a reviewed plan:
pmc program-plan PMCF-000001 --file program-plan.json

pmc program-approve PMCF-000001
pmc program-run PMCF-000001 --detach
pmc program-status PMCF-000001
```

Follow a detached run with:

```bash
journalctl --user -fu pmc-program-pmcf-000001
```

When the terminal integration job is green:

```bash
pmc program-status PMCF-000001
pmc inspect <terminal-job-id>
pmc program-diff PMCF-000001
pmc program-accept PMCF-000001
```

## Plan contract

Program plans contain 2-50 tasks, explicit acceptance criteria for every task,
an acyclic dependency graph, and exactly one terminal task. Because there is one
sink, every task must transitively feed the final integration result.

```json
{
  "tasks": [
    {
      "id": "foundation",
      "request": "Create deterministic simulation primitives",
      "acceptance": ["seeded runs have stable canonical hashes"],
      "depends_on": [],
      "task_type": "ARCHITECTURAL",
      "complexity": "DIFFICULT",
      "risk": "HIGH",
      "budget": "high-risk",
      "priority": 1,
      "candidate_order": ["jules"]
    },
    {
      "id": "integration",
      "request": "Integrate and run the final experiment",
      "acceptance": ["the documented clean-checkout workflow passes"],
      "depends_on": ["foundation"],
      "task_type": "INTEGRATION",
      "priority": 1
    }
  ]
}
```

Plans become immutable when approved. Candidate capability checks happen before
jobs are created. Program creation records the exact base repository commit; every
task branches from that immutable baseline rather than whatever the named branch
happens to contain days later. `program-diff` shows the aggregate result from that
original commit, including all internally promoted milestones.

## Operational behavior

- `--workers N` is a ceiling. PMC dispatches only dependency-ready work.
- Each job has its own worktree and job/resource/provider leases.
- Dependency commits are applied in deterministic topological order.
- Independent siblings may run concurrently; a dependent never sees an
  unverified sibling.
- A controller restart promotes already-verified work idempotently and waits for
  still-valid job leases before recovering them.
- A blocked branch stops new dispatch. Already-running siblings drain so useful
  verified work is preserved.
- A dependency integration conflict blocks the affected job with exact Git
  evidence. PMC never guesses through a conflict or rewrites accepted history.
- A graph with no runnable/in-flight task is marked blocked instead of polling
  forever.
- Detached runs use a user systemd service with restart-on-crash. Deliberate
  `BLOCKED`, `PAUSED`, and `READY_FOR_REVIEW` outcomes exit successfully and do
  not create restart loops.

## Intervention

Pause dispatch while allowing current jobs to drain:

```bash
pmc program-pause PMCF-000001
```

Resume a paused program:

```bash
pmc program-resume PMCF-000001
pmc program-run PMCF-000001 --detach
```

For a blocked implementation, preserve the worktree and provide repair evidence:

```bash
pmc reject PMC-000123 "Exact corrective feedback"
pmc program-resume PMCF-000001
pmc program-run PMCF-000001 --detach
```

Cancel the program and its queued/running tasks:

```bash
pmc program-cancel PMCF-000001
```

Cancellation, final acceptance, destructive conflict resolution, and merging to
the repository's main branch remain human-controlled.

## Known boundary

Program mode performs deterministic commit assembly through Git cherry-picks. It
does not autonomously resolve semantic or textual merge conflicts. A conflict is
treated as evidence that the approved decomposition did not establish a clean
enough ownership boundary; the program blocks for a reviewed repair or revised
plan rather than silently choosing code.
