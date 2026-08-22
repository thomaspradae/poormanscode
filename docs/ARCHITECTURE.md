# PMC Architecture

## Boundary

PMC knows about an `Executor`, not about one permanent agent framework.

```text
                    human ticket / feedback
                              |
                              v
                    +-------------------+
                    | PMC CONTROL PLANE |
                    +-------------------+
                      |   |    |     |
                jobs/db  git  policy  evidence
                      \    |    |    /
                       \   |    |   /
                        SCHEDULER
                            |
             +--------------+---------------+
             |              |               |
             v              v               v
         BashExecutor  OpenHandsExecutor  JulesExecutor
             |              |               |
             +--------------+---------------+
                            |
                       task worktree
                            |
                   deterministic verifier
                            |
                    optional independent
                          reviewer
                            |
                     READY_FOR_REVIEW
                            |
                      human decision
                       /          \
                    reject       accept
                      |             |
                    repair      PMC commit
```

## Data is part of the product

Every executor/model pairing is a named `Candidate`. Every attempt records candidate, executor, model, selection mode, task type, attempt number, latency, token counts, cost when available, verifier outcome, reviewer outcome, and human outcome.

Attempt 1 and repair attempts are analyzed separately. This matters because repairing an existing patch is a different problem from solving a ticket from a clean base.

`selection_mode=explore` is randomized among exploration-eligible candidates. That slice is the closest thing PMC has to a continuing production experiment. Policy-selected (`exploit`) rows are useful operationally but should not be interpreted as an unbiased head-to-head comparison.

## Controller-owned boundaries

Executors never own acceptance. PMC owns:

- Git baseline, worktree and final commit
- hidden verification commands
- protected-path and patch-size policy
- secret scanning
- scheduler/quota/resource accounting
- human feedback
- the historical outcome database

An executor may run tests while working. That is advisory. The controller runs fresh verification after the executor stops.

## Resource model

`quota_group` models a shared scarce allowance, such as several candidates consuming the same provider pool.

`resource_group` models a physical bottleneck, such as multiple model candidates sharing one CPU-only machine. `resource_concurrency=1` prevents them from being scheduled simultaneously.

Candidate-level `max_concurrency` handles a resource like Jules that permits a fixed number of concurrent sessions.

## Why three executors

`BashExecutor` is the deliberately small control harness. It uses an OpenAI-compatible model endpoint and a JSON shell-action protocol.

`OpenHandsExecutor` is the default rich agent runtime when its extra tools/isolation are worth it. Local execution is possible, but production should prefer a sandboxed/remote Agent Server.

`JulesExecutor` is different because it contributes an external autonomous environment and separate quota. It returns a ChangeSet patch, which PMC applies to its own worktree and verifies itself.

## Learning policy

The current scheduler intentionally stays simple:

1. collect a minimum number of production observations per exploration-eligible candidate;
2. exploit the best observed candidate most of the time;
3. randomly allocate the configured exploration fraction;
4. prefer human acceptance as the quality signal once enough labels exist; otherwise use deterministic verifier success provisionally;
5. stratify by task type when enough task-specific observations exist.

Do not replace this with a complicated contextual bandit until there is enough data to validate that the extra machinery improves accepted-commit economics.
