# Kanban work mode

Kanban mode is PMC's deliberately simple path for exploratory projects. It
coexists with programs and ordinary jobs; it does not alter their scheduling,
branches, or verification policy.

## Model

One project owns one persistent branch and worktree. Only one task can be
`WORKING` in that project at a time. Tasks are database cards rather than Git
integration branches:

`BACKLOG` → `READY` → `WORKING` → `CHECKPOINTED` → `DONE`

Useful work that cannot currently finish becomes `NEEDS_REPAIR`. Deterministic
infrastructure failures become `BLOCKED`. Neither state destroys a committed
checkpoint, and either leaves capacity for another READY card.

## Checkpoints and Git

`pmc kanban checkpoint` commits every meaningful slice on the project branch
and pushes it immediately. A non-fast-forward or other deterministic push
failure is recorded as `REMOTE_DIVERGED`/`GIT_PUSH_FAILED` and the card blocks
after that one push attempt. Kanban never cherry-picks dependencies and never
automatically retries an unchanged Git error.

## Verification

Projects choose `SMOKE`, `STANDARD`, or `STRICT`. `SMOKE` is intended for the
Football World Lab while it is being explored: run the relevant command, check
basic output, and check seeded determinism where applicable. Missing broad
test coverage is a warning, not a reason to erase a useful checkpoint.

## Operator commands

```bash
pmc kanban init football-world-lab --title 'Football World Lab' \
  --repo /home/t/football-game --branch work/football-world-lab
pmc kanban add football-world-lab people-generation \
  --title 'Generate deterministic synthetic players' \
  --request '...' --acceptance 'seeded output is stable'
pmc kanban start people-generation --executor jules
# executor works in the persistent project worktree
pmc kanban checkpoint people-generation --message 'add seeded player records'
pmc kanban smoke people-generation --command 'dotnet test --no-restore'
pmc kanban board football-world-lab
```

`pmc kanban board` reports the card state, executor, last checkpoint, smoke
evidence, latest activity and an exact blocked reason. A watchdog can call
`Kanban.diagnose_working(project, alive_task_ids)`; it only turns phantom work
into `NEEDS_REPAIR`, it never retries the executor itself.
