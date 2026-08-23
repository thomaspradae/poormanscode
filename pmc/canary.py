from __future__ import annotations

import json
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import PMCConfig
from .controller import Controller
from .domain import Candidate, Job, JobState


class _Handler(BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length", "0")))
        type(self).calls += 1
        actions = [
            {"action": "bash", "command": "sed -i 's/a - b/a + b/' app.py"},
            {"action": "done", "summary": "canary patch complete"},
        ]
        content = actions[min(self.calls - 1, len(actions) - 1)]
        body = json.dumps({"id": f"canary-{self.calls}", "choices": [{"message": {"content": json.dumps(content)}}],
                           "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def run_canary(verifier_sandbox: str = "guarded") -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="pmc-canary-") as td:
        root = Path(td)
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "canary@localhost"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "PMC Canary"], cwd=repo, check=True)
        (repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
        (repo / "poorman.yaml").write_text("test: python -m py_compile app.py\nmax_patch_lines: 20\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            candidate = Candidate("canary-bash-v1", "bash", model="canary-model",
                                  provider="local-canary", base_url=f"http://127.0.0.1:{server.server_port}/v1",
                                  sandbox="guarded", max_turns=3)
            cfg = PMCConfig(root / "pmc.db", root / "runs", root / "worktrees",
                            verifier_sandbox=verifier_sandbox, candidates=[candidate])
            ctl = Controller(cfg)
            job = Job("PMC-CANARY", repo, "Fix add", acceptance=["syntax verification passes"])
            ctl.db.create_job(job)
            state = ctl.run_job(job.id)
            if state != JobState.READY:
                raise RuntimeError(f"canary ended in {state}")
            commit = ctl.accept(job.id)
            with ctl.db.connect() as conn:
                events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                attempts = conn.execute("SELECT COUNT(*) FROM attempts WHERE outcome='SUCCESS'").fetchone()[0]
            artifacts = len(list((root / "runs" / job.id).iterdir()))
            return {"state": "ACCEPTED", "commit": commit, "events": events,
                    "successful_attempts": attempts, "audit_artifacts": artifacts,
                    "verifier_sandbox": verifier_sandbox}
        finally:
            server.shutdown()
            server.server_close()
