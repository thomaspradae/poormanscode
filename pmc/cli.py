from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from .classifier import classify
from .config import DEFAULT_CONFIG, load_config
from .controller import Controller, LeaseBusy
from .domain import Job, JobState
from .foreman import propose_plan, validate_plan


def _attempt_duration(attempt) -> float:
    if attempt["duration_seconds"] is not None:
        return float(attempt["duration_seconds"])
    if attempt["status"] == "RUNNING" and attempt["started_at"]:
        return max(
            0.0,
            (
                datetime.now(UTC) - datetime.fromisoformat(attempt["started_at"])
            ).total_seconds(),
        )
    return 0.0


from .gitops import ensure_repo


def _controller(args) -> Controller:
    cfg = load_config(
        Path(args.config).expanduser() if getattr(args, "config", None) else None
    )
    return Controller(cfg)


def _templates_dir() -> Path:
    return Path(__file__).resolve().parent / "templates"


def cmd_init_config(args) -> int:
    dest = Path(args.path).expanduser() if args.path else DEFAULT_CONFIG
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not args.force:
        print(f"exists: {dest}")
        return 0
    shutil.copyfile(_templates_dir() / "config.toml", dest)
    print(f"created: {dest}")
    return 0


def cmd_init_repo(args) -> int:
    repo = Path(args.repo).resolve()
    ensure_repo(repo)
    templates = _templates_dir()
    for src_name, dest_name in (
        ("AGENTS.md", "AGENTS.md"),
        ("poorman.yaml", "poorman.yaml"),
    ):
        dest = repo / dest_name
        if dest.exists() and not args.force:
            print(f"exists: {dest}")
            continue
        shutil.copyfile(templates / src_name, dest)
        print(f"created: {dest}")
    return 0


def cmd_submit(args) -> int:
    ctl = _controller(args)
    repo = Path(args.repo).resolve()
    ensure_repo(repo)
    job_id = ctl.db.next_job_id()
    constraints = {}
    from .capabilities import infer_required_capabilities, repository_is_skeletal

    inferred = infer_required_capabilities(
        args.request, skeletal=repository_is_skeletal(repo)
    )
    if inferred:
        constraints["required_capabilities"] = inferred
    if args.max_files is not None:
        constraints["max_files_changed"] = args.max_files
    if args.max_lines is not None:
        constraints["max_patch_lines"] = args.max_lines
    if args.no_new_dependencies:
        constraints["no_new_dependencies"] = True
    if args.allowed_path:
        constraints["allowed_paths"] = args.allowed_path
    from .budget import characterize, envelope_for

    task_type = args.task_type or classify(args.request)
    inferred_complexity, inferred_risk = characterize(
        args.request, task_type, args.priority
    )
    complexity = args.complexity or inferred_complexity
    risk = args.risk or inferred_risk
    job = Job(
        id=job_id,
        repo=repo,
        request=args.request,
        base_branch=args.base_branch,
        priority=args.priority,
        task_type=task_type,
        acceptance=args.acceptance or [],
        constraints=constraints,
        complexity=complexity,
        risk=risk,
        budget=envelope_for(complexity, risk, args.budget),
    )
    ctl.db.create_job(job)
    print(job_id)
    if args.run:
        state = ctl.run_job(job_id, forced_candidate=args.candidate)
        print(state.value)
    return 0


def cmd_run(args) -> int:
    ctl = _controller(args)
    state = ctl.run_job(args.job_id, forced_candidate=args.candidate)
    print(state.value)
    return 0 if state == JobState.READY else 2


def cmd_daemon(args) -> int:
    ctl = _controller(args)
    print(f"PMC daemon using {ctl.cfg.db_path}")
    while True:
        jobs = ctl.db.queued_jobs(limit=1)
        if not jobs:
            if args.once:
                return 0
            time.sleep(args.poll_seconds)
            continue
        job_id = jobs[0]
        print(f"running {job_id}", flush=True)
        try:
            state = ctl.run_job(job_id)
            print(f"{job_id}: {state.value}", flush=True)
        except LeaseBusy as exc:
            print(f"{job_id}: leased elsewhere ({exc})", file=sys.stderr, flush=True)
            if not args.once:
                time.sleep(args.poll_seconds)
        except Exception as exc:
            ctl.db.set_state(job_id, JobState.BLOCKED)
            print(
                f"{job_id}: BLOCKED: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
        if args.once:
            return 0


def cmd_status(args) -> int:
    ctl = _controller(args)
    rows = ctl.db.list_jobs(args.limit)
    if not rows:
        print("no jobs")
        return 0
    print(f"{'JOB':<12} {'STATE':<18} {'TYPE':<18} {'P':<2} REQUEST")
    for r in rows:
        request = r["request"].replace("\n", " ")[:80]
        print(
            f"{r['id']:<12} {r['state']:<18} {r['task_type']:<18} {r['priority']:<2} {request}"
        )
    return 0


def cmd_feature_create(args) -> int:
    ctl = _controller(args)
    repo = Path(args.repo).resolve()
    ensure_repo(repo)
    feature_id = ctl.db.next_feature_id()
    ctl.db.create_feature(feature_id, repo, args.title, args.request, args.base_branch)
    print(feature_id)
    return 0


def cmd_feature_plan(args) -> int:
    ctl = _controller(args)
    feature = ctl.db.get_feature(args.feature_id)
    if args.file:
        plan = validate_plan(json.loads(Path(args.file).read_text()))
        candidate_name = None
    else:
        matches = [
            c for c in ctl.cfg.candidates if c.name == args.candidate and c.enabled
        ]
        if not matches:
            raise RuntimeError(
                f"unknown or disabled Foreman candidate: {args.candidate}"
            )
        plan = propose_plan(
            Path(feature["repo"]), feature["title"], feature["request"], matches[0]
        )
        candidate_name = matches[0].name
    ctl.db.save_feature_plan(args.feature_id, plan, candidate_name)
    print(json.dumps(plan, indent=2))
    print(f"\nReview, then run: pmc feature-approve {args.feature_id}")
    return 0


def cmd_feature_approve(args) -> int:
    ctl = _controller(args)
    feature = ctl.db.get_feature(args.feature_id)
    if not feature["plan_json"]:
        raise RuntimeError("feature has no proposed plan")
    plan = validate_plan(json.loads(feature["plan_json"]))
    next_number = int(ctl.db.next_job_id().split("-")[-1])
    jobs = []
    from .capabilities import infer_required_capabilities, repository_is_skeletal

    skeletal = repository_is_skeletal(Path(feature["repo"]))
    for position, task in enumerate(plan["tasks"]):
        job = Job(
            id=f"PMC-{next_number + position:06d}",
            repo=Path(feature["repo"]),
            request=task["request"],
            base_branch=feature["base_branch"],
            priority=int(task.get("priority", 2)),
            task_type=str(task.get("task_type") or classify(task["request"])),
            acceptance=list(task.get("acceptance", [])),
            constraints={
                "_feature_dependencies": list(task.get("depends_on", [])),
                "_candidate_order": list(task.get("candidate_order", [])),
                "required_capabilities": infer_required_capabilities(
                    task["request"], skeletal=skeletal and not task.get("depends_on")
                ),
            },
        )
        jobs.append((job, task["id"], position, list(task.get("depends_on", []))))
    by_name = {c.name: c for c in ctl.cfg.candidates if c.enabled}
    for job, key, _position, _depends in jobs:
        ordered = job.constraints.get("_candidate_order") or []
        unknown = [name for name in ordered if name not in by_name]
        if unknown:
            raise RuntimeError(f"task {key} names unknown candidates: {unknown}")
        considered = (
            [by_name[name] for name in ordered] if ordered else list(by_name.values())
        )
        if considered and all(ctl.capabilities.missing(job, c) for c in considered):
            details = {c.name: ctl.capabilities.missing(job, c) for c in considered}
            raise RuntimeError(
                f"task {key} has no capability-compatible candidate: {details}"
            )
    ctl.db.approve_feature_plan(args.feature_id, jobs)
    for job, key, _position, depends in jobs:
        suffix = f" depends={','.join(depends)}" if depends else " ready"
        print(f"{job.id} {key}{suffix}")
    return 0


def cmd_board(args) -> int:
    ctl = _controller(args)
    features = (
        [ctl.db.get_feature(args.feature_id)]
        if args.feature_id
        else ctl.db.list_features()
    )
    if not features:
        print("no features")
        return 0
    for feature in features:
        state = ctl.db.refresh_feature_state(feature["id"])
        print(f"{feature['id']} [{state}] {feature['title']}")
        tasks = ctl.db.feature_tasks(feature["id"])
        if not tasks:
            print("  (no approved tasks)")
            continue
        accepted = {row["task_key"] for row in tasks if row["state"] == "ACCEPTED"}
        for row in tasks:
            deps = json.loads(row["depends_json"])
            waiting = [dep for dep in deps if dep not in accepted]
            display_state = (
                f"WAITING({','.join(waiting)})"
                if waiting and row["state"] == "QUEUED"
                else row["state"]
            )
            print(f"  {row['job_id']:<12} {row['task_key']:<24} {display_state}")
    return 0


def cmd_inspect(args) -> int:
    ctl = _controller(args)
    d = ctl.db.job_detail(args.job_id)
    job = d["job"]
    print(
        json.dumps(
            {
                "id": job["id"],
                "state": job["state"],
                "repo": job["repo"],
                "worktree": job["worktree"],
                "request": job["request"],
                "accepted_commit": job["accepted_commit"],
            },
            indent=2,
        )
    )
    print("\nATTEMPTS")
    for a in d["attempts"]:
        duration = _attempt_duration(a)
        input_tokens = a["input_tokens"] or 0
        output_tokens = a["output_tokens"] or 0
        cost_usd = a["cost_usd"]
        if a["status"] == "RUNNING":
            live = ctl.db.model_request_totals(a["id"])
            input_tokens = live["input_tokens"]
            output_tokens = live["output_tokens"]
            cost_usd = live["cost_usd"]
        print(
            f"#{a['attempt_no']} {a['candidate']} [{a['executor']}] {a['status']} "
            f"{duration:.1f}s tokens="
            f"{input_tokens + output_tokens} cost=${(cost_usd or 0):.4f}"
        )
        if a["error"]:
            print("  error:", a["error"][:1000])
    if d["verifications"]:
        print("\nVERIFICATION")
        for v in d["verifications"]:
            findings = json.loads(v["findings_json"])
            print(
                f"#{v['attempt_no']} {v['candidate']}: {'PASS' if v['ok'] else 'FAIL'} patch_lines={v['patch_lines']}"
            )
            for finding in findings:
                print("  -", finding)
    if d["reviews"]:
        print("\nREVIEWS")
        for r in d["reviews"]:
            print(
                f"#{r['attempt_no']} {r['reviewer_candidate']}: {r['verdict']} {r['summary'] or ''}"
            )
    if d["feedback"]:
        print("\nHUMAN FEEDBACK")
        for f in d["feedback"]:
            print(f"{f['verdict']}: {f['feedback'] or ''}")
    if d["intelligence_allocations"]:
        print("\nEXTRA INTELLIGENCE")
        for item in d["intelligence_allocations"]:
            print(
                f"{item['role']} {item['candidate'] or '-'}: {item['state']} — {item['reason']}"
            )
    if d["post_acceptance_outcomes"]:
        print("\nPOST-ACCEPTANCE OUTCOMES")
        for item in d["post_acceptance_outcomes"]:
            print(f"{item['outcome']}: {item['details'] or ''}")
    return 0


def cmd_reverify(args) -> int:
    state = _controller(args).reverify_job(args.job_id)
    print(state.value)
    return 0 if state == JobState.READY else 2


def cmd_cancel(args) -> int:
    ctl = _controller(args)
    ctl.cancel(args.job_id)
    print("CANCELLED")
    return 0


def cmd_cleanup(args) -> int:
    ctl = _controller(args)
    ctl.cleanup(args.job_id, force=args.force)
    print("CLEAN")
    return 0


def cmd_diff(args) -> int:
    ctl = _controller(args)
    job = ctl.db.get_job(args.job_id)
    if not job.worktree or not job.baseline_commit:
        raise RuntimeError("job has no worktree")
    sys.stdout.write(ctl.worktrees.diff(job.worktree, job.baseline_commit))
    return 0


def cmd_accept(args) -> int:
    ctl = _controller(args)
    commit = ctl.accept(
        args.job_id,
        args.message,
        review_seconds=args.review_seconds,
        human_changed_lines=args.human_changed_lines,
    )
    print(commit)
    return 0


def cmd_reject(args) -> int:
    ctl = _controller(args)
    ctl.reject(
        args.job_id,
        args.feedback,
        review_seconds=args.review_seconds,
        repair_seconds=args.repair_seconds,
        human_changed_lines=args.human_changed_lines,
    )
    print("RETRY")
    return 0


def cmd_record_manual(args) -> int:
    ctl = _controller(args)
    task_type = args.task_type or classify(args.request)
    row_id = ctl.db.record_manual_baseline(
        request=args.request,
        task_type=task_type,
        tool=args.tool,
        duration_seconds=args.seconds,
        accepted=args.accepted,
        cost_usd=args.cost,
        repo=str(Path(args.repo).resolve()) if args.repo else None,
        job_id=args.job_id,
        notes=args.notes,
    )
    print(row_id)
    return 0


def cmd_baseline_stats(args) -> int:
    ctl = _controller(args)
    rows = ctl.db.manual_stats(args.task_type)
    if not rows:
        print("no manual baseline observations yet")
        return 0
    print(f"{'TOOL':<24} {'N':>4} {'ACCEPT':>8} {'AVG S':>10} {'AVG $':>10}")
    for r in rows:
        n = r["n"] or 0
        ar = 100 * (r["accepted"] or 0) / n if n else 0
        print(
            f"{r['tool']:<24} {n:>4} {ar:>7.1f}% {(r['avg_seconds'] or 0):>10.1f} {(r['avg_cost'] or 0):>10.4f}"
        )
    return 0


def cmd_stats(args) -> int:
    ctl = _controller(args)
    rows = ctl.db.candidate_stats(
        role=args.role,
        task_type=args.task_type,
        phase=args.phase,
        selection_mode=args.mode,
    )
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("no observations yet")
        return 0
    print(
        f"{'CANDIDATE':<30} {'N':>4} {'VERIFY':>8} {'HUMAN+':>8} {'HUM N':>6} {'AVG S':>9} {'AVG TOK':>9} {'AVG $':>9}"
    )
    for r in rows:
        n = r["attempts"] or 0
        vr = 100 * (r["verified"] or 0) / n if n else 0
        ha = 100 * (r["accepted_attempts"] or 0) / n if n else 0
        print(
            f"{r['candidate']:<30} {n:>4} {vr:>7.1f}% {ha:>7.1f}% "
            f"{(r['human_labeled'] or 0):>6} {(r['avg_seconds'] or 0):>9.1f} "
            f"{(r['avg_tokens'] or 0):>9.0f} {(r['avg_cost'] or 0):>9.4f}"
        )
    return 0


def cmd_efficiency(args) -> int:
    rows = _controller(args).db.efficiency_stats()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print(
        f"{'CANDIDATE':<30} {'ATTEMPTS':>8} {'ACCEPTED':>8} {'SUCCESS':>9} {'MEDIAN S':>10}"
    )
    for row in rows:
        median = (
            "-"
            if row["median_wall_clock_to_accepted_seconds"] is None
            else f"{row['median_wall_clock_to_accepted_seconds']:.1f}"
        )
        print(
            f"{row['candidate']:<30} {row['attempts']:>8} {row['accepted']:>8} {row['success_rate']:>8.1%} {median:>10}"
        )
    return 0


def cmd_attention(args) -> int:
    rows = _controller(args).db.attention_stats()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print(
        f"{'CANDIDATE':<32} {'JOBS':>5} {'STABLE':>7} {'REVIEW S':>10} {'REPAIR S':>10} {'NO EDIT':>8}"
    )
    for row in rows:
        no_edit = row["no_edit_rate"]
        print(
            f"{row['candidate']:<32} {row['jobs']:>5} {row['stable_accepted'] or 0:>7} "
            f"{row['avg_review_seconds'] or 0:>10.1f} {row['avg_repair_seconds'] or 0:>10.1f} "
            f"{(100 * no_edit if no_edit is not None else 0):>7.1f}%"
        )
    return 0


def cmd_outcome(args) -> int:
    ctl = _controller(args)
    ctl.db.record_post_acceptance_outcome(
        args.job_id,
        args.outcome,
        details=args.details,
        repair_seconds=args.repair_seconds,
        changed_lines=args.human_changed_lines,
    )
    print(args.outcome)
    return 0


def cmd_export(args) -> int:
    ctl = _controller(args)
    rows = ctl.db.export_attempts()
    out = Path(args.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        out.write_text("")
        print(out)
        return 0
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(out)
    return 0


def cmd_candidates(args) -> int:
    ctl = _controller(args)
    print(f"{'NAME':<30} {'ROLE':<10} {'EXECUTOR':<12} {'MODEL':<35} STATUS")
    for c in ctl.cfg.candidates:
        av = ctl.scheduler.available(c, ctl.cfg.candidates)
        print(
            f"{c.name:<30} {c.role:<10} {c.executor:<12} {(c.model or '-'): <35} {'READY' if av.ok else av.reason}"
        )
    return 0


def cmd_models_list(args) -> int:
    ctl = _controller(args)
    for candidate in ctl.cfg.candidates:
        row = ctl.db.model_conformance(candidate)
        status = row["status"] if row else "UNKNOWN"
        provider = candidate.provider or "local"
        print(
            f"{candidate.name:32} {status:12} {provider:12} {candidate.model or candidate.executor}"
        )
    return 0


def cmd_models_smoke(args) -> int:
    from .conformance import smoke_candidate

    ctl = _controller(args)
    selected = set(args.candidates or [])
    candidates = [
        c
        for c in ctl.cfg.candidates
        if c.enabled and (not selected or c.name in selected)
    ]
    unknown = selected - {c.name for c in candidates}
    if unknown:
        raise SystemExit("unknown/disabled candidates: " + ", ".join(sorted(unknown)))
    failed = False
    for candidate in candidates:
        result = smoke_candidate(ctl.db, candidate)
        print(
            f"{result['candidate']}: {result['status']} generation={result['generation']} tool={result['tool']}"
        )
        failed |= result["status"] != "AVAILABLE"
    return 2 if failed else 0


def cmd_models_conformance(args) -> int:
    from .conformance import coding_conformance

    ctl = _controller(args)
    selected = set(args.candidates)
    candidates = [c for c in ctl.cfg.candidates if c.name in selected]
    unknown = selected - {c.name for c in candidates}
    if unknown:
        raise SystemExit("unknown candidates: " + ", ".join(sorted(unknown)))
    failed = False
    for candidate in candidates:
        result = coding_conformance(ctl, candidate)
        print(json.dumps(result, indent=2, default=str))
        failed |= not all(result["levels"].values())
    return 2 if failed else 0


def cmd_context_xray(args) -> int:
    """Display content-free per-request context and quota diagnostics."""
    from datetime import datetime

    ctl = _controller(args)
    rows = ctl.db.model_request_xray(args.job_id)
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    if not rows:
        print(f"No model requests recorded for {args.job_id}")
        return 1
    print(
        f"{'TURN':>4} {'KIND':<9} {'CANDIDATE':<32} {'EST IN':>7} "
        f"{'ACT IN':>7} {'OUT':>6} {'OCC':>7} {'COND':>5} {'LANE':<12} "
        f"{'MSG':>4} {'TOOLS':>5} {'GROW':>7} {'SEC':>6} STATE"
    )
    for row in rows:
        metrics = row["context_metrics"]
        occupancy = metrics.get("context_occupancy")
        occupancy_text = f"{100 * occupancy:.1f}%" if occupancy is not None else "?"
        started, finished = row.get("started_at"), row.get("finished_at")
        seconds = None
        if started and finished:
            seconds = (
                datetime.fromisoformat(finished) - datetime.fromisoformat(started)
            ).total_seconds()
        print(
            f"{row['turn_number']:>4} {row.get('request_kind') or 'agent':<9} "
            f"{row['candidate']:<32} {row.get('estimated_input_tokens') or 0:>7} "
            f"{row.get('actual_input_tokens') or 0:>7} "
            f"{row.get('actual_output_tokens') or 0:>6} {occupancy_text:>7} "
            f"{metrics.get('condensation_count_before', 0):>5} "
            f"{(row.get('credential_id') or '-'): <12} "
            f"{metrics.get('message_count', 0):>4} {metrics.get('tool_count', 0):>5} "
            f"{metrics.get('growth_tokens') if metrics.get('growth_tokens') is not None else 0:>7} "
            f"{seconds if seconds is not None else 0:>6.1f} {row['state']}"
        )
        if args.composition:
            composition = metrics.get("composition", {})
            print(
                "     composition: "
                + ", ".join(f"{key}={value}" for key, value in composition.items())
            )
            headers = row.get("rate_headers", {})
            safe = {
                key: value
                for key, value in headers.items()
                if key.startswith("x-ratelimit-") or key == "retry-after"
            }
            if safe:
                print("     quota: " + ", ".join(f"{k}={v}" for k, v in safe.items()))
    return 0


def cmd_capabilities(args) -> int:
    ctl = _controller(args)
    snapshot = ctl.capabilities.local
    print(f"resource: {snapshot.resource}")
    for name in sorted(snapshot.capabilities):
        print(name)
    return 0


def cmd_bootstrap_unity(args) -> int:
    ctl = _controller(args)
    repo = Path(args.repo).resolve()
    ensure_repo(repo)
    from .toolchains import UnityToolchain

    toolchain = UnityToolchain.from_config(ctl.cfg.toolchains.get("unity", {}))
    toolchain.bootstrap(repo, timeout=args.timeout)
    print(f"Unity project created through {toolchain.editor}")
    print(
        "Review generated files, update immutable poorman.yaml policy, then commit the baseline."
    )
    return 0


def cmd_doctor(args) -> int:
    ok = True
    print(
        f"config: {Path(args.config).expanduser() if args.config else DEFAULT_CONFIG}"
    )
    try:
        ctl = _controller(args)
        print(f"database: OK ({ctl.cfg.db_path})")
    except Exception as exc:
        print(f"database/config: FAIL ({exc})")
        return 2
    for binary in ("git", "bash"):
        found = shutil.which(binary)
        print(f"{binary}: {'OK ' + found if found else 'MISSING'}")
        ok &= bool(found)
    bwrap = shutil.which("bwrap")
    needs_bwrap = any(c.enabled and c.sandbox == "bwrap" for c in ctl.cfg.candidates)
    if bwrap:
        print(f"bubblewrap: OK {bwrap}")
        if needs_bwrap:
            probe = subprocess.run(
                [
                    bwrap,
                    "--die-with-parent",
                    "--new-session",
                    "--ro-bind",
                    "/",
                    "/",
                    "--dev",
                    "/dev",
                    "--proc",
                    "/proc",
                    "--tmpfs",
                    "/tmp",
                    "/usr/bin/true",
                ],
                text=True,
                capture_output=True,
            )
            if probe.returncode:
                print(f"bubblewrap production probe: FAIL ({probe.stderr.strip()})")
                ok = False
            else:
                print("bubblewrap production probe: OK")
    elif needs_bwrap:
        print("bubblewrap: MISSING (required by an enabled sandbox=bwrap candidate)")
        ok = False
    else:
        print("bubblewrap: not installed (no enabled candidate requires it)")

    needs_openhands = any(
        c.enabled and c.executor == "openhands" for c in ctl.cfg.candidates
    )
    try:
        print("OpenHands SDK: OK")
    except Exception:
        if needs_openhands:
            print("OpenHands SDK: MISSING (required by an enabled openhands candidate)")
            ok = False
        else:
            print("OpenHands SDK: not installed (no enabled candidate requires it)")

    for c in ctl.cfg.candidates:
        if not c.enabled:
            continue
        missing = bool(c.api_key_env and not os.getenv(c.api_key_env))
        print(
            f"candidate {c.name}: {'MISSING ' + c.api_key_env if missing else 'configured'}"
        )
        if missing:
            ok = False
        if c.provider:
            pool_ok, pool_reason = ctl.db.provider_availability(c.provider)
            print(
                f"candidate {c.name} provider pool: "
                f"{'OK' if pool_ok else 'UNAVAILABLE ' + pool_reason}"
            )
            ok &= pool_ok
        if ctl.cfg.require_model_conformance:
            conformance = ctl.db.model_conformance(c)
            status = conformance["status"] if conformance else "UNKNOWN"
            print(f"candidate {c.name} conformance: {status}")
            if c.enabled and status != "AVAILABLE":
                # Degraded/unknown candidates are safely excluded from routing;
                # report them without making the whole control plane unhealthy.
                print(f"candidate {c.name}: excluded until a smoke test passes")
        if c.executor == "bash" and c.sandbox in {"none", "guarded"}:
            print(f"candidate {c.name}: UNSAFE sandbox={c.sandbox}")
            ok = False
        if c.executor == "bash":
            from .sandbox import build_sandbox

            sandbox = build_sandbox(c.sandbox, c.extra)
            policy = c.effective_network_policy
            if not sandbox.supports_network_policy(policy):
                print(
                    f"candidate {c.name}: FAIL network_policy={policy} "
                    f"cannot be enforced by {sandbox.name}"
                )
                ok = False
            else:
                print(f"candidate {c.name}: network_policy={policy} enforceable")
        if c.executor == "openhands" and c.extra.get("agent_kind") != "acp":
            max_events = c.extra.get("condenser_max_events", 80)
            max_tokens = c.extra.get("condenser_max_tokens")
            request_limit = c.extra.get("request_token_soft_limit")
            print(
                f"candidate {c.name}: condenser=LLMSummarizingCondenser "
                f"max_events={max_events} max_tokens={max_tokens or 'model-default'} "
                f"request_soft_limit={request_limit or 'none'}"
            )
    if ctl.cfg.research_enabled:
        present = bool(os.getenv(ctl.cfg.research_api_key_env))
        print(
            f"research: {'configured' if present else 'MISSING ' + ctl.cfg.research_api_key_env} "
            f"(model={ctl.cfg.research_model}, controller-side)"
        )
        ok &= present
    else:
        print("research: disabled")
    for name, toolchain_cfg in ctl.cfg.toolchains.items():
        if name == "unity":
            from .toolchains import UnityToolchain, UnityToolchainError

            try:
                unity = UnityToolchain.from_config(toolchain_cfg)
                print(f"toolchain unity: configured ({unity.editor})")
            except UnityToolchainError as exc:
                print(f"toolchain unity: unavailable ({exc})")
    if ctl.cfg.verifier_sandbox in {"none", "guarded"}:
        print(f"verifier sandbox: UNSAFE ({ctl.cfg.verifier_sandbox})")
        ok = False
    elif ctl.cfg.verifier_sandbox == "restricted-user":
        probe = subprocess.run(
            ["sudo", "-n", "-u", "pmc-worker", "/usr/bin/true"],
            text=True,
            capture_output=True,
        )
        if probe.returncode:
            print(f"verifier sandbox: FAIL restricted-user ({probe.stderr.strip()})")
            ok = False
        else:
            print("verifier sandbox: OK restricted-user (network_policy=full only)")
    elif ctl.cfg.verifier_sandbox == "bwrap":
        verifier_probe = (
            subprocess.run(
                [
                    bwrap,
                    "--die-with-parent",
                    "--new-session",
                    "--ro-bind",
                    "/",
                    "/",
                    "--dev",
                    "/dev",
                    "--proc",
                    "/proc",
                    "--tmpfs",
                    "/tmp",
                    "/usr/bin/true",
                ],
                text=True,
                capture_output=True,
            )
            if bwrap
            else None
        )
        if verifier_probe is None or verifier_probe.returncode:
            detail = (
                verifier_probe.stderr.strip()
                if verifier_probe
                else "bubblewrap missing"
            )
            print(f"verifier sandbox: FAIL bwrap ({detail})")
            ok = False
        else:
            print("verifier sandbox: OK bwrap")
    else:
        print(f"verifier sandbox: OK {ctl.cfg.verifier_sandbox}")
    return 0 if ok else 2


def cmd_version(args) -> int:
    from .versioning import SCHEMA_VERSION, pmc_git_sha

    print(f"poormans-code 0.1.0 schema={SCHEMA_VERSION} git={pmc_git_sha()}")
    return 0


def cmd_canary(args) -> int:
    from .canary import run_canary

    print(json.dumps(run_canary(args.verifier_sandbox, args.builder_sandbox), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pmc")
    p.add_argument("--config", help="path to config.toml")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("version")
    s.set_defaults(func=cmd_version)

    s = sub.add_parser("canary")
    s.add_argument(
        "--verifier-sandbox",
        default="guarded",
        choices=["guarded", "restricted-user", "bwrap"],
    )
    s.add_argument(
        "--builder-sandbox",
        default="guarded",
        choices=["guarded", "restricted-user", "bwrap"],
    )
    s.set_defaults(func=cmd_canary)

    s = sub.add_parser("init-config")
    s.add_argument("--path")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init_config)

    s = sub.add_parser("init-repo")
    s.add_argument("repo", nargs="?", default=".")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init_repo)

    s = sub.add_parser("submit")
    s.add_argument("repo")
    s.add_argument("request")
    s.add_argument("--base-branch", default="main")
    s.add_argument("--priority", type=int, choices=range(5), default=2)
    s.add_argument("--task-type")
    s.add_argument("--complexity", choices=["TRIVIAL", "STANDARD", "DIFFICULT"])
    s.add_argument("--risk", choices=["LOW", "MEDIUM", "HIGH", "CRITICAL"])
    s.add_argument(
        "--budget", choices=["trivial", "standard", "difficult", "high-risk"]
    )
    s.add_argument("--acceptance", action="append")
    s.add_argument("--max-files", type=int)
    s.add_argument("--max-lines", type=int)
    s.add_argument("--allowed-path", action="append")
    s.add_argument("--no-new-dependencies", action="store_true")
    s.add_argument("--run", action="store_true")
    s.add_argument("--candidate", help="force a specific candidate for this run")
    s.set_defaults(func=cmd_submit)

    s = sub.add_parser("run")
    s.add_argument("job_id")
    s.add_argument("--candidate", help="force a specific candidate")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("daemon")
    s.add_argument("--poll-seconds", type=float, default=5.0)
    s.add_argument("--once", action="store_true")
    s.set_defaults(func=cmd_daemon)

    s = sub.add_parser("status")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("feature-create")
    s.add_argument("repo")
    s.add_argument("title")
    s.add_argument("request")
    s.add_argument("--base-branch", default="main")
    s.set_defaults(func=cmd_feature_create)

    s = sub.add_parser("feature-plan")
    s.add_argument("feature_id")
    source = s.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="reviewed JSON plan file")
    source.add_argument("--candidate", help="OpenAI-compatible Foreman candidate")
    s.set_defaults(func=cmd_feature_plan)

    s = sub.add_parser("feature-approve")
    s.add_argument("feature_id")
    s.set_defaults(func=cmd_feature_approve)

    s = sub.add_parser("board")
    s.add_argument("feature_id", nargs="?")
    s.set_defaults(func=cmd_board)

    s = sub.add_parser("inspect")
    s.add_argument("job_id")
    s.set_defaults(func=cmd_inspect)

    s = sub.add_parser("diff")
    s.add_argument("job_id")
    s.set_defaults(func=cmd_diff)

    s = sub.add_parser("cancel")
    s.add_argument("job_id")
    s.set_defaults(func=cmd_cancel)

    s = sub.add_parser("cleanup")
    s.add_argument("job_id")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_cleanup)

    s = sub.add_parser("accept")
    s.add_argument("job_id")
    s.add_argument("--message")
    s.add_argument("--review-seconds", type=float)
    s.add_argument("--human-changed-lines", type=int)
    s.set_defaults(func=cmd_accept)

    s = sub.add_parser("reject")
    s.add_argument("job_id")
    s.add_argument("feedback")
    s.add_argument("--review-seconds", type=float)
    s.add_argument("--repair-seconds", type=float)
    s.add_argument("--human-changed-lines", type=int)
    s.set_defaults(func=cmd_reject)

    s = sub.add_parser(
        "reverify", help="re-run verification for the latest preserved attempt"
    )
    s.add_argument("job_id")
    s.set_defaults(func=cmd_reverify)

    s = sub.add_parser("record-manual")
    s.add_argument("tool", help="e.g. codex, claude-code, hand-written")
    s.add_argument("request")
    s.add_argument("--seconds", type=float, required=True)
    s.add_argument("--cost", type=float, default=0.0)
    result = s.add_mutually_exclusive_group(required=True)
    result.add_argument("--accepted", action="store_true")
    result.add_argument("--rejected", action="store_true")
    s.add_argument("--task-type")
    s.add_argument("--repo")
    s.add_argument("--job-id")
    s.add_argument("--notes")
    s.set_defaults(func=cmd_record_manual)

    s = sub.add_parser("baseline-stats")
    s.add_argument("--task-type")
    s.set_defaults(func=cmd_baseline_stats)

    s = sub.add_parser("stats")
    s.add_argument("--role", default="builder")
    s.add_argument("--task-type")
    s.add_argument("--phase", choices=["all", "first", "repair"], default="all")
    s.add_argument("--mode", choices=["cold_start", "explore", "exploit", "forced"])
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("efficiency")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_efficiency)

    s = sub.add_parser("attention")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_attention)

    s = sub.add_parser("outcome")
    s.add_argument("job_id")
    s.add_argument(
        "outcome",
        choices=[
            "STABLE",
            "REOPENED",
            "REVERTED",
            "REGRESSION",
            "HOTFIX",
            "HUMAN_CORRECTION",
        ],
    )
    s.add_argument("--details")
    s.add_argument("--repair-seconds", type=float)
    s.add_argument("--human-changed-lines", type=int)
    s.set_defaults(func=cmd_outcome)

    s = sub.add_parser("export")
    s.add_argument("output")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("candidates")
    s.set_defaults(func=cmd_candidates)

    s = sub.add_parser("models")
    model_sub = s.add_subparsers(dest="models_command", required=True)
    ml = model_sub.add_parser("list")
    ml.set_defaults(func=cmd_models_list)
    ms = model_sub.add_parser("smoke")
    ms.add_argument("candidates", nargs="*")
    ms.set_defaults(func=cmd_models_smoke)
    mc = model_sub.add_parser("conformance")
    mc.add_argument("candidates", nargs="+")
    mc.set_defaults(func=cmd_models_conformance)

    s = sub.add_parser("context-xray")
    s.add_argument("job_id")
    s.add_argument("--composition", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_context_xray)

    s = sub.add_parser("capabilities")
    s.set_defaults(func=cmd_capabilities)

    s = sub.add_parser("bootstrap-unity")
    s.add_argument("repo")
    s.add_argument("--timeout", type=int, default=1800)
    s.set_defaults(func=cmd_bootstrap_unity)

    s = sub.add_parser("doctor")
    s.set_defaults(func=cmd_doctor)
    return p


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
