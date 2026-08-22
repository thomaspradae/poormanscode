from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import time
from pathlib import Path

from .classifier import classify
from .config import DEFAULT_CONFIG, load_config
from .controller import Controller, LeaseBusy
from .db import Database
from .domain import Job, JobState
from .gitops import ensure_repo


def _controller(args) -> Controller:
    cfg = load_config(Path(args.config).expanduser() if getattr(args, "config", None) else None)
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
    for src_name, dest_name in (("AGENTS.md", "AGENTS.md"), ("poorman.yaml", "poorman.yaml")):
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
    if args.max_files is not None:
        constraints["max_files_changed"] = args.max_files
    if args.max_lines is not None:
        constraints["max_patch_lines"] = args.max_lines
    if args.no_new_dependencies:
        constraints["no_new_dependencies"] = True
    if args.allowed_path:
        constraints["allowed_paths"] = args.allowed_path
    job = Job(
        id=job_id,
        repo=repo,
        request=args.request,
        base_branch=args.base_branch,
        priority=args.priority,
        task_type=args.task_type or classify(args.request),
        acceptance=args.acceptance or [],
        constraints=constraints,
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
            print(f"{job_id}: BLOCKED: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
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
        print(f"{r['id']:<12} {r['state']:<18} {r['task_type']:<18} {r['priority']:<2} {request}")
    return 0


def cmd_inspect(args) -> int:
    ctl = _controller(args)
    d = ctl.db.job_detail(args.job_id)
    job = d["job"]
    print(json.dumps({
        "id": job["id"], "state": job["state"], "repo": job["repo"],
        "worktree": job["worktree"], "request": job["request"],
        "accepted_commit": job["accepted_commit"],
    }, indent=2))
    print("\nATTEMPTS")
    for a in d["attempts"]:
        print(
            f"#{a['attempt_no']} {a['candidate']} [{a['executor']}] {a['status']} "
            f"{(a['duration_seconds'] or 0):.1f}s tokens="
            f"{(a['input_tokens'] or 0)+(a['output_tokens'] or 0)} cost=${(a['cost_usd'] or 0):.4f}"
        )
        if a["error"]:
            print("  error:", a["error"][:1000])
    if d["verifications"]:
        print("\nVERIFICATION")
        for v in d["verifications"]:
            findings = json.loads(v["findings_json"])
            print(f"#{v['attempt_no']} {v['candidate']}: {'PASS' if v['ok'] else 'FAIL'} patch_lines={v['patch_lines']}")
            for finding in findings:
                print("  -", finding)
    if d["reviews"]:
        print("\nREVIEWS")
        for r in d["reviews"]:
            print(f"#{r['attempt_no']} {r['reviewer_candidate']}: {r['verdict']} {r['summary'] or ''}")
    if d["feedback"]:
        print("\nHUMAN FEEDBACK")
        for f in d["feedback"]:
            print(f"{f['verdict']}: {f['feedback'] or ''}")
    return 0


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
    commit = ctl.accept(args.job_id, args.message)
    print(commit)
    return 0


def cmd_reject(args) -> int:
    ctl = _controller(args)
    ctl.reject(args.job_id, args.feedback)
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
        print(f"{r['tool']:<24} {n:>4} {ar:>7.1f}% {(r['avg_seconds'] or 0):>10.1f} {(r['avg_cost'] or 0):>10.4f}")
    return 0


def cmd_stats(args) -> int:
    ctl = _controller(args)
    rows = ctl.db.candidate_stats(
        role=args.role, task_type=args.task_type, phase=args.phase, selection_mode=args.mode
    )
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("no observations yet")
        return 0
    print(f"{'CANDIDATE':<30} {'N':>4} {'VERIFY':>8} {'HUMAN+':>8} {'HUM N':>6} {'AVG S':>9} {'AVG TOK':>9} {'AVG $':>9}")
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
        print(f"{c.name:<30} {c.role:<10} {c.executor:<12} {(c.model or '-'): <35} {'READY' if av.ok else av.reason}")
    return 0


def cmd_doctor(args) -> int:
    ok = True
    print(f"config: {Path(args.config).expanduser() if args.config else DEFAULT_CONFIG}")
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
    elif needs_bwrap:
        print("bubblewrap: MISSING (required by an enabled sandbox=bwrap candidate)")
        ok = False
    else:
        print("bubblewrap: not installed (no enabled candidate requires it)")

    needs_openhands = any(c.enabled and c.executor == "openhands" for c in ctl.cfg.candidates)
    try:
        import openhands.sdk  # type: ignore
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
        print(f"candidate {c.name}: {'MISSING ' + c.api_key_env if missing else 'configured'}")
        if missing:
            ok = False
    return 0 if ok else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pmc")
    p.add_argument("--config", help="path to config.toml")
    sub = p.add_subparsers(dest="command", required=True)

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
    s.add_argument("--priority", type=int, choices=range(0, 5), default=2)
    s.add_argument("--task-type")
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
    s.set_defaults(func=cmd_accept)

    s = sub.add_parser("reject")
    s.add_argument("job_id")
    s.add_argument("feedback")
    s.set_defaults(func=cmd_reject)

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

    s = sub.add_parser("export")
    s.add_argument("output")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("candidates")
    s.set_defaults(func=cmd_candidates)

    s = sub.add_parser("doctor")
    s.set_defaults(func=cmd_doctor)
    return p


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
