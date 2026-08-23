# Canonical PMC implementation plan

Rule: anything affecting learning must be observable and versioned. Anything
affecting acceptance must remain outside worker authority.

## P0 — required for production project #1

- [x] Reproducible install, canonical paths, migrations, production-path doctor
- [x] Version all experimental inputs and policies per attempt
- [x] Immutable, versioned Candidate as the empirical unit
- [x] Append-only event ledger
- [x] Fully reconstructable scheduler decisions including propensity
- [x] Per-model-request quota events, reservations, reconciliation, and live routing feedback
- [x] Quantitative CPU/RAM/disk/GPU/named-slot resource reservations and leases
- [x] Sandbox abstraction with secret-free environment, process-tree/cgroup limits,
  worktree-only filesystem, continuous disk limits, and enforced Bubblewrap no-network mode
- [x] Baseline-owned Git and immutable verification policy
- [x] Canonical terminal outcome taxonomy and explicit retry/quality-scoring policy
- [x] SIGKILL/restart recovery across dispatch, model request, execution, verification,
  review, READY, and commit; acceptance state/feedback/events are atomic and idempotent
- [x] Versioned ContextBundle with persisted manifest/hash
- [x] Independent deterministic verification after successful executor output
- [x] Human decision attached to exact READY attempt
- [x] Complete reconstructable audit bundle
- [x] Process-tree termination, aggregate workspace/file/artifact limits, and accounting tests
- [x] Enforced worker network denial on this host through a minimal-mount Bubblewrap backend;
  guarded/restricted-user explicitly advertise `full` only
- [x] Continuous during-execution workspace byte/file monitoring and termination
- [x] Disposable canary lifecycle reconstructed from DB, events, and artifacts

## P1 — early production

- [ ] Generic project/toolchain profiles and Unity/C# profile
- [ ] First-class artifacts and machine/visual/performance/playtest gates
- [ ] Unity EditMode, PlayMode, build, scene, serialization, runtime, and performance checks
- [ ] Independent read-only reviewer and reviewer scoring
- [ ] Research role/profile
- [ ] Task characterization, risk, context size, and verification strength

## P2 — multi-agent/distributed capability

- [ ] Foreman, dependency DAG, parallel worktrees, integration/rebase/conflicts
- [ ] Shadow/challenger execution
- [ ] Post-acceptance regression, revert, reopen, and hotfix outcomes

## P3 — empirical router evolution

- [ ] Contextual candidate estimates and contextual bandit routing
- [ ] Information-value-aware exploration
- [ ] Human-attention economics and stable-outcome metrics
- [ ] Manual-agent baselines and role-specific benchmarks

## P4 — complete software factory

- [ ] GitHub issue/PR/CI integration and operator dashboard
- [ ] Background jobs and idle-resource scheduling
- [ ] Slurm/experiment executor and experiment artifacts
- [ ] Backups, restores, integrity checks, and exports
- [ ] Executor conformance suite and richer security policy
- [ ] Automated performance and experiment reporting
