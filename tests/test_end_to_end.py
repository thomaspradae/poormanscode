from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pmc.config import PMCConfig
from pmc.controller import Controller
from pmc.domain import Candidate, Job, JobState


class Handler(BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self):
        length = int(self.headers.get("content-length", "0"))
        json.loads(self.rfile.read(length) or b"{}")
        Handler.calls += 1
        if Handler.calls == 1:
            content = json.dumps({
                "action": "bash",
                "command": "sed -i 's/a - b/a + b/' app.py"
            })
        elif Handler.calls == 2:
            content = json.dumps({"action": "bash", "command": "pytest -q"})
        else:
            content = json.dumps({"action": "done", "summary": "fixed subtraction bug and tests pass"})
        body = json.dumps({
            "id": f"mock-{Handler.calls}",
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def sh(cwd: Path, command: str):
    return subprocess.run(["bash", "-lc", command], cwd=cwd, check=True, text=True, capture_output=True)


def test_controller_full_cycle(tmp_path: Path):
    Handler.calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        repo = tmp_path / "repo"
        repo.mkdir()
        sh(repo, "git init -q -b main && git config user.email test@example.com && git config user.name Test")
        (repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
        (repo / "test_app.py").write_text("from app import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
        (repo / "poorman.yaml").write_text("test: pytest -q\nmax_patch_lines: 20\nmax_files_changed: 2\n")
        sh(repo, "git add -A && git commit -qm init")

        c = Candidate(
            name="mock-bash",
            executor="bash",
            model="mock-model",
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            max_turns=5,
            sandbox="guarded",
        )
        cfg = PMCConfig(
            db_path=tmp_path / "pmc.db",
            runs_dir=tmp_path / "runs",
            worktrees_dir=tmp_path / "worktrees",
            exploration_rate=0.2,
            min_samples_per_candidate=2,
            max_attempts=2,
            candidates=[c],
        )
        ctl = Controller(cfg)
        job = Job("PMC-000001", repo, "Fix add so it adds", task_type="BUG_FIX", acceptance=["tests pass"])
        ctl.db.create_job(job)
        assert ctl.run_job(job.id) == JobState.READY
        commit = ctl.accept(job.id)
        assert len(commit) >= 7
        detail = ctl.db.job_detail(job.id)
        assert detail["job"]["state"] == "ACCEPTED"
        stats = ctl.db.candidate_stats(phase="first")
        assert stats[0]["verified"] == 1
        assert stats[0]["accepted_attempts"] == 1
        with ctl.db.connect() as conn:
            attempt = conn.execute("SELECT * FROM attempts").fetchone()
            assert attempt["outcome"] == "SUCCESS"
            assert attempt["context_hash"]
            versions = json.loads(attempt["version_snapshot_json"])
            assert versions["base_repository_sha"]
            decision = conn.execute("SELECT * FROM scheduler_decisions").fetchone()
            assert decision["selection_probability"] == 1.0
            event_types = [r[0] for r in conn.execute("SELECT event_type FROM events ORDER BY seq")]
            assert event_types == sorted(event_types, key=lambda x: event_types.index(x))
            assert {"JOB_CREATED", "SCHEDULER_DECISION", "ATTEMPT_STARTED",
                    "RESOURCE_RESERVED", "VERIFICATION_STARTED", "HUMAN_ACCEPTED"} <= set(event_types)
    finally:
        server.shutdown()
        server.server_close()
