from __future__ import annotations

import os
import subprocess
import tempfile
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx

from .db import Database
from .domain import Candidate, ExecutionRequest, Job, Outcome
from .executors.bash import SHELL_TOOL, _extract_json
from .providers import OpenAICompatibleClient, ProviderError


def smoke_candidate(db: Database, candidate: Candidate) -> dict[str, Any]:
    """Verify generation and native tool protocol without exposing credentials."""
    key_env = (
        db.provider_credential_env(candidate.provider) if candidate.provider else None
    )
    c = replace(candidate, api_key_env=key_env or candidate.api_key_env)
    details: dict[str, Any] = {"provider": c.provider, "model": c.model}
    generation_ok = tool_ok = False
    transient = False
    try:
        if c.executor == "jules":
            # Jules is an external task executor, so validate its authenticated
            # task/source API rather than pretending it has a native shell tool.
            key = os.getenv(c.api_key_env or "")
            response = httpx.get(
                "https://jules.googleapis.com/v1alpha/sources",
                headers={"x-goog-api-key": key or ""},
                timeout=30,
            )
            response.raise_for_status()
            generation_ok = isinstance(response.json().get("sources", []), list)
            tool_ok = generation_ok
            details["kind"] = "external-executor-api"
        elif c.executor == "openhands" and c.extra.get("agent_kind") == "acp":
            # ACP providers own both inference and tools. Validate the actual
            # agent protocol by making it write a sentinel in a disposable Git
            # workspace; a direct provider completion cannot prove this path.
            from .executors.openhands import OpenHandsExecutor

            with tempfile.TemporaryDirectory(prefix="pmc-acp-smoke-") as td:
                worktree = Path(td)
                subprocess.run(["git", "init", "-q", str(worktree)], check=True)
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(worktree),
                        "config",
                        "user.email",
                        "pmc@localhost",
                    ],
                    check=True,
                )
                subprocess.run(
                    ["git", "-C", str(worktree), "config", "user.name", "PMC"],
                    check=True,
                )
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(worktree),
                        "commit",
                        "--allow-empty",
                        "-qm",
                        "baseline",
                    ],
                    check=True,
                )
                job = Job(f"PMC-SMOKE-{uuid.uuid4().hex[:8]}", worktree, "ACP smoke")
                result = OpenHandsExecutor().run(
                    ExecutionRequest(
                        job,
                        c,
                        worktree,
                        "Create a file named PMC_ACP_SMOKE.txt containing exactly PMC_ACP_TOOL_OK and finish.",
                        1,
                    )
                )
                generation_ok = result.ok
                sentinel = worktree / "PMC_ACP_SMOKE.txt"
                tool_ok = (
                    sentinel.exists()
                    and sentinel.read_text().strip() == "PMC_ACP_TOOL_OK"
                )
                if not result.ok:
                    details["error"] = result.error
                    transient = result.outcome in {
                        Outcome.RATE_LIMIT,
                        Outcome.PROVIDER_FAILURE,
                        Outcome.RESOURCE_FAILURE,
                        Outcome.TIMEOUT,
                    }
                details["kind"] = "acp-agent-file-write"
                details["accounting"] = result.accounting_level
        elif c.executor == "openhands" and c.base_url and c.model:
            # OpenHands keeps model inference in the PMC controller and uses the
            # remote Agent Server only for workspace/tool execution.  Smoke both
            # halves independently so a provider can be generation-ready while
            # a worker remains quarantined if its workspace endpoint is broken.
            client = OpenAICompatibleClient(c.base_url, c.api_key_env, timeout=90)
            reply = client.chat(
                model=c.model,
                messages=[
                    {"role": "user", "content": "Reply with exactly PMC_SMOKE_OK"}
                ],
                temperature=0,
                max_tokens=256,
            )
            generation_ok = "PMC_SMOKE_OK" in (reply.content or "")
            if c.server_url:
                from openhands.sdk import Workspace

                server_key = (
                    os.getenv(c.server_api_key_env) if c.server_api_key_env else None
                )
                workspace = Workspace(host=c.server_url, api_key=server_key)
                result = workspace.execute_command("printf PMC_OPENHANDS_TOOL_OK")
                tool_ok = "PMC_OPENHANDS_TOOL_OK" in (result.stdout or "")
            details["kind"] = "controller-llm-remote-workspace"
            details["request_ids_present"] = bool(reply.request_id)
        elif c.executor == "bash" and c.base_url and c.model:
            client = OpenAICompatibleClient(c.base_url, c.api_key_env, timeout=90)
            reply = client.chat(
                model=c.model,
                messages=[
                    {"role": "user", "content": "Reply with exactly PMC_SMOKE_OK"}
                ],
                # Reasoning models may consume a small hidden reasoning budget
                # before emitting the visible sentinel.
                temperature=0,
                max_tokens=256,
            )
            generation_ok = "PMC_SMOKE_OK" in (reply.content or "")
            tool_reply = client.chat(
                model=c.model,
                messages=[
                    {
                        "role": "user",
                        "content": "Use the shell tool exactly once with command: printf PMC_TOOL_OK",
                    }
                ],
                temperature=0,
                max_tokens=256,
                tools=[SHELL_TOOL],
            )
            tool_ok = any(
                call.get("function", {}).get("name") == "shell"
                and "PMC_TOOL_OK" in str(call.get("function", {}).get("arguments", ""))
                for call in tool_reply.tool_calls
            )
            if not tool_ok and tool_reply.content:
                try:
                    action = _extract_json(tool_reply.content)
                    arguments = action.get("arguments", {})
                    if isinstance(arguments, str):
                        import json

                        arguments = json.loads(arguments)
                    command = arguments.get("command") or arguments.get("cmd")
                    if action.get("action") == "bash":
                        command = action.get("command")
                    tool_ok = action.get("name") == "shell" and "PMC_TOOL_OK" in str(
                        command
                    )
                    tool_ok |= action.get("action") == "bash" and "PMC_TOOL_OK" in str(
                        command
                    )
                except (ValueError, TypeError, AttributeError):
                    pass
            details["request_ids_present"] = bool(
                reply.request_id or tool_reply.request_id
            )
        else:
            details["error"] = "unsupported executor for conformance smoke"
    except Exception as exc:  # smoke must quarantine any adapter failure
        details["error"] = f"{type(exc).__name__}: {exc}"
        transient = isinstance(exc, ProviderError) and (
            exc.status_code == 429 or exc.status_code >= 500
        )
    db.set_model_conformance(
        candidate,
        generation_ok=generation_ok,
        tool_ok=tool_ok,
        details=details,
        status=("DEGRADED" if transient else None),
    )
    row = db.model_conformance(candidate)
    return {
        "candidate": candidate.name,
        "generation": generation_ok,
        "tool": tool_ok,
        "status": row["status"],
    }


def coding_conformance(controller: Any, candidate: Candidate) -> dict[str, Any]:
    """Run the production OpenHands L0-L4 ladder in one reconstructable job.

    The cumulative fixture proves generation, repository inspection, editing,
    failure observation/repair, deterministic verification, request accounting,
    and the complete Controller audit lifecycle without spending four separate
    agent runs on redundant lower-level tasks.
    """
    if candidate.executor != "openhands" or candidate.extra.get("agent_kind") == "acp":
        raise ValueError("coding conformance currently requires built-in OpenHands")
    started = time.monotonic()
    token = f"PMC_INSPECT_{uuid.uuid4().hex[:10]}"
    details: dict[str, Any] = {
        "candidate": candidate.name,
        "provider": candidate.provider,
        "model": candidate.model,
        "levels": {},
    }
    with tempfile.TemporaryDirectory(prefix="pmc-openhands-conformance-") as td_text:
        root = Path(td_text)
        repo = root / "repo"
        repo.mkdir()
        (repo / "SPEC.txt").write_text(
            f"Inspection token: {token}\nThe slug separator is a hyphen.\n"
        )
        (repo / "slug.py").write_text(
            "def slugify(value: str) -> str:\n    return value\n"
        )
        (repo / "test_slug.py").write_text(
            "import unittest\n"
            "from slug import slugify\n\n"
            "class SlugTests(unittest.TestCase):\n"
            "    def test_normalizes_words(self):\n"
            "        self.assertEqual(slugify('  Brake Repair Bogotá!  '), "
            "'brake-repair-bogota')\n"
            "    def test_collapses_separators(self):\n"
            "        self.assertEqual(slugify('Oil___Change'), 'oil-change')\n\n"
            "if __name__ == '__main__':\n    unittest.main()\n"
        )
        (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n")
        (repo / "poorman.yaml").write_text(
            "test: python -m unittest -v\n"
            "lint: null\nbuild: null\n"
            "protected: [poorman.yaml]\nmax_patch_lines: 200\n"
        )
        for command in (
            ["git", "init", "-q", "-b", "main", str(repo)],
            ["git", "-C", str(repo), "config", "user.email", "pmc@localhost"],
            ["git", "-C", str(repo), "config", "user.name", "PMC"],
            ["git", "-C", str(repo), "add", "."],
            ["git", "-C", str(repo), "commit", "-qm", "conformance baseline"],
        ):
            subprocess.run(command, check=True)
        before = subprocess.run(
            ["python", "-m", "unittest", "-v"],
            cwd=repo,
            text=True,
            capture_output=True,
        )
        details["initial_test_failed"] = before.returncode != 0

        from .controller import Controller
        from .domain import BudgetEnvelope, JobState

        # Conformance is deliberately bounded. A candidate that cannot finish
        # this tiny repair in twelve agent turns is not production-ready.
        runnable = replace(candidate, enabled=True, max_turns=12)
        cfg = replace(
            controller.cfg,
            db_path=root / "pmc.db",
            runs_dir=root / "runs",
            worktrees_dir=root / "worktrees",
            candidates=[runnable],
            max_attempts=1,
            same_candidate_retries=1,
            review_enabled=False,
            require_model_conformance=False,
            research_enabled=False,
        )
        ctl = Controller(cfg)
        job_id = f"PMC-CONFORMANCE-{uuid.uuid4().hex[:10].upper()}"
        job = Job(
            job_id,
            repo,
            (
                "Read SPEC.txt and create inspection.txt containing exactly its "
                "inspection token. Run python -m unittest -v and observe the "
                "failures. Repair slugify in slug.py without changing tests, "
                "rerun the tests until they pass, verify the inspection file, "
                "then finish."
            ),
            task_type="BUG_FIX",
            complexity="DIFFICULT",
            risk="LOW",
            budget=BudgetEnvelope(
                name="conformance",
                max_attempts=1,
                max_model_requests=30,
                max_wall_seconds=900,
                max_repairs=0,
            ),
        )
        ctl.db.create_job(job)
        try:
            state = ctl.run_job(job_id, forced_candidate=runnable.name)
        except Exception as exc:
            state = JobState.FAILED
            details["controller_error"] = f"{type(exc).__name__}: {exc}"
        persisted = ctl.db.get_job(job_id)
        worktree = persisted.worktree
        attempts = ctl.db.job_detail(job_id)["attempts"]
        attempt = attempts[-1] if attempts else None
        totals = (
            ctl.db.model_request_totals(int(attempt["id"]), runnable.name)
            if attempt
            else {
                "requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": None,
            }
        )
        with ctl.db.connect() as conn:
            lanes = [
                row[0]
                for row in conn.execute(
                    "SELECT credential_id FROM model_requests ORDER BY id"
                ).fetchall()
            ]
            events = {
                row[0]: int(row[1])
                for row in conn.execute(
                    "SELECT event_type,COUNT(*) FROM events GROUP BY event_type"
                ).fetchall()
            }
        request_xray = []
        for row in ctl.db.model_request_xray(job_id):
            metrics = row["context_metrics"]
            request_xray.append(
                {
                    "turn": row["turn_number"],
                    "kind": row.get("request_kind"),
                    "state": row["state"],
                    "estimated_input_tokens": row["estimated_input_tokens"],
                    "actual_input_tokens": row["actual_input_tokens"],
                    "actual_output_tokens": row["actual_output_tokens"],
                    "credential_id": row.get("credential_id"),
                    "context_occupancy": metrics.get("context_occupancy"),
                    "condensation_count": metrics.get(
                        "condensation_count_before", 0
                    ),
                    "message_count": metrics.get("message_count"),
                    "tool_count": metrics.get("tool_count"),
                    "growth_tokens": metrics.get("growth_tokens"),
                    "unchanged_retry": metrics.get("unchanged_from_previous"),
                    "duplicate_messages": metrics.get(
                        "duplicate_messages_within_request"
                    ),
                    "composition": metrics.get("composition", {}),
                    "rate_headers": row.get("rate_headers", {}),
                }
            )
        inspection_ok = bool(
            worktree
            and (worktree / "inspection.txt").exists()
            and (worktree / "inspection.txt").read_text().strip() == token
        )
        changed = bool(
            worktree
            and subprocess.run(
                ["git", "-C", str(worktree), "diff", "--quiet", "HEAD", "--", "slug.py"]
            ).returncode
        )
        post = (
            subprocess.run(
                ["python", "-m", "unittest", "-v"],
                cwd=worktree,
                text=True,
                capture_output=True,
            )
            if worktree
            else None
        )
        tests_ok = bool(post and post.returncode == 0)
        audit_ok = (root / "runs" / job_id / "events.json").exists()
        details["levels"] = {
            "L0_generation": totals["requests"] > 0 and totals["output_tokens"] > 0,
            "L1_inspection": inspection_ok,
            "L2_edit": changed,
            "L3_failure_repair": details["initial_test_failed"] and tests_ok,
            "L4_pmc_lifecycle": state == JobState.READY and audit_ok,
        }
        details.update(
            {
                "state": state.value,
                "job_id": job_id,
                "model_requests": totals,
                "credential_lanes": lanes,
                "event_counts": events,
                "request_xray": request_xray,
                "attempt": dict(attempt) if attempt else None,
                "wall_seconds": round(time.monotonic() - started, 3),
            }
        )
    passed = all(details["levels"].values())
    controller.db.set_model_conformance(
        candidate,
        generation_ok=bool(details["levels"].get("L0_generation")),
        tool_ok=bool(details["levels"].get("L2_edit")),
        details=details,
        status="AVAILABLE" if passed else "QUARANTINED",
    )
    return details
