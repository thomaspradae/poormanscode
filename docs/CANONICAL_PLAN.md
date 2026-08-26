# Canonical PMC implementation plan

Rule: anything affecting learning must be observable and versioned. Anything
affecting acceptance must remain outside worker authority.

## Unified provider capacity and job budgets

- [x] Candidates own quality history; credentials are interchangeable quota lanes
- [x] Provider credential registry stores secret references only, with health,
  cooldown, concurrency, quota-scope identity and conservative confidence
- [x] Atomic per-model-request credential selection, reservation, reconciliation,
  authentication quarantine, and least-utilized lane selection
- [x] Provider capacity gates candidates without adding credentials to candidate identity
- [x] Automatic numbered-key discovery plus per-credential `pmc credentials probe`
  warm-up ledger; lane health is independent of candidate qualification and quota-scope
  confidence
- [x] `pmc models list`, `pmc models smoke`, and cumulative L0-L4
  `pmc models conformance` persist generation, repository inspection, editing,
  repair, verifier, accounting, and audit evidence and gate production scheduling
- [x] Complexity and semantic risk are classified separately with human overrides
- [x] Job-owned budget envelopes cap attempts, requests, tokens, wall time, cost,
  reviews, repairs, parallel candidates and challengers
- [x] Human review/repair time, human edit burden, no-edit acceptance, and stable
  post-acceptance outcomes with `pmc attention` / `pmc outcome`
- [x] Auditable risk-, verification-, and uncertainty-driven planner, independent
  reviewer, and challenger-opinion allocation bounded by each job envelope
- [x] Contextual Thompson routing after a configurable evidence threshold, using
  stable human outcomes by task type, complexity, risk, and attempt phase; every
  decision records seed, contextual observations, uncertainty, and propensity

Invariants: candidates own performance history; credentials own quota state;
providers aggregate capacity; jobs own spending limits; verification owns truth;
humans own acceptance.

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
- [x] Explicit OpenHands summarizing condenser configuration plus content-free
  per-request token/context X-rays, including tool schemas, growth, retries,
  condensation, lane quota state, and actual-vs-estimated usage
- [x] Versioned, model-independent WorkState recovery packet and partial remote-diff
  preservation across executor failure/candidate handoff; raw model reasoning is not
  transferred between candidates
- [x] Evidence profiles for request-size percentiles, size-bucket 429 probability,
  post-condensation progress/repetition, and verifier outcome. Initial thresholds
  remain hypotheses and automatic tuning stays disabled until evidence is mature
- [x] Conservative quota-scope report separating configured scopes from strongly
  observed counter-lane independence; observation never fabricates confirmed scope IDs
- [x] Pre-dispatch observed-TPM fit, reset-aware waiting, and quota deferral events
  prevent known-impossible physical requests
- [x] Separate model context capacity, provider sustainable request throughput,
  and cumulative job budgets; reject known-incompatible requests before inference
- [x] Independent deterministic verification after successful executor output
- [x] Human decision attached to exact READY attempt
- [x] Complete reconstructable audit bundle
- [x] Process-tree termination, aggregate workspace/file/artifact limits, and accounting tests
- [x] Enforced worker network denial on this host through a minimal-mount Bubblewrap backend;
  guarded/restricted-user explicitly advertise `full` only
- [x] Continuous during-execution workspace byte/file monitoring and termination
- [x] Disposable canary lifecycle reconstructed from DB, events, and artifacts
- [x] Generic skeletal-repository detection, inferred hard capabilities, probed
  capability registry, pre-token scheduler rejection, and BLOCKED_CAPABILITY audit events
- [x] Authoritative bootstrap guidance for Unity, Node, Rust, .NET, and Python
- [x] Controller-side Gemini Google Search grounding with query budgets and persisted
  research events/results/sources; worker credentials and network remain isolated
- [x] Repeated-command and token-without-diff convergence controls

## P1 — early production

- [~] Generic project/toolchain profiles (bootstrap foundation complete); Unity/C#
  execution and verification profile still requires a registered Unity Editor
- [~] Unity profile now provides native skeletal-repo bootstrap, versioned Editor
  registration, batch compilation, and optional EditMode/PlayMode gates. Host Editor
  installation/licensing and the first live canary remain outstanding.
- [ ] First-class artifacts and machine/visual/performance/playtest gates
- [ ] Unity EditMode, PlayMode, build, scene, serialization, runtime, and performance checks
- [ ] Independent read-only reviewer and reviewer scoring
- [x] Controller-mediated grounded research tool for Bash workers
- [ ] Task characterization, risk, context size, and verification strength

## P2 — multi-agent/distributed capability

- [x] Foreman proposal format, human approval, dependency DAG, and Kanban CLI
- [ ] Automatic cross-worktree integration/rebase/conflict handling
- [ ] Shadow/challenger execution
- [ ] Post-acceptance regression, revert, reopen, and hotfix outcomes

## P3 — empirical router evolution

- [x] Contextual candidate estimates and contextual Thompson routing with a
  sparse-data epsilon/cold-start fallback
- [x] Information-value-aware allocation driven by contextual uncertainty,
  semantic risk, and verification strength
- [x] Human-attention economics and stable-outcome metrics
- [~] Context/request policy evidence is now queryable with `pmc context-profile`;
  automated threshold selection remains data-gated (minimum 50 requests/five attempts
  per immutable candidate version plus verified post-condensation outcomes)
- [ ] Manual-agent baselines and role-specific benchmarks

## P4 — complete software factory

- [ ] GitHub issue/PR/CI integration and operator dashboard
- [ ] Background jobs and idle-resource scheduling
- [ ] Slurm/experiment executor and experiment artifacts
- [ ] Backups, restores, integrity checks, and exports
- [x] OpenHands L0-L4 coding/lifecycle conformance suite and controller-side
  credential-isolating provider gateway; broader Jules/Bash plugin conformance
  remains incremental
- [ ] Automated performance and experiment reporting
