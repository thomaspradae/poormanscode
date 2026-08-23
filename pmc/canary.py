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
        body = json.dumps(
            {
                "id": f"canary-{self.calls}",
                "choices": [{"message": {"content": json.dumps(content)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def run_canary(
    verifier_sandbox: str = "guarded", builder_sandbox: str = "guarded"
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="pmc-canary-") as td:
        root = Path(td)
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "canary@localhost"], cwd=repo, check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "PMC Canary"], cwd=repo, check=True
        )
        (repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
        (repo / "poorman.yaml").write_text(
            "test: python -m py_compile app.py\nmax_patch_lines: 20\n"
        )
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            candidate = Candidate(
                "canary-bash-v1",
                "bash",
                model="canary-model",
                provider="local-canary",
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                sandbox=builder_sandbox,
                network=builder_sandbox == "guarded",
                max_turns=3,
            )
            cfg = PMCConfig(
                root / "pmc.db",
                root / "runs",
                root / "worktrees",
                verifier_sandbox=verifier_sandbox,
                candidates=[candidate],
            )
            ctl = Controller(cfg)
            job = Job(
                "PMC-CANARY", repo, "Fix add", acceptance=["syntax verification passes"]
            )
            ctl.db.create_job(job)
            state = ctl.run_job(job.id)
            if state != JobState.READY:
                raise RuntimeError(f"canary ended in {state}")
            commit = ctl.accept(job.id)
            with ctl.db.connect() as conn:
                event_rows = conn.execute(
                    "SELECT * FROM events ORDER BY seq"
                ).fetchall()
                attempt = conn.execute(
                    "SELECT * FROM attempts WHERE outcome='SUCCESS'"
                ).fetchone()
                requests = conn.execute(
                    "SELECT * FROM model_requests WHERE attempt_id=?", (attempt["id"],)
                ).fetchall()
                feedback = conn.execute(
                    "SELECT * FROM human_feedback WHERE job_id=?", (job.id,)
                ).fetchall()
                reserved = conn.execute(
                    "SELECT COUNT(*) FROM quota_reservations WHERE state='RESERVED'"
                ).fetchone()[0]
                leases = conn.execute(
                    "SELECT (SELECT COUNT(*) FROM leases) + "
                    "(SELECT COUNT(*) FROM resource_leases)"
                ).fetchone()[0]
            reconstructed = (
                bool(requests)
                and sum((row["actual_input_tokens"] or 0) for row in requests)
                == (attempt["input_tokens"] or 0)
                and sum((row["actual_output_tokens"] or 0) for row in requests)
                == (attempt["output_tokens"] or 0)
                and len(feedback) == 1
                and feedback[0]["attempt_id"] == attempt["id"]
                and reserved == 0
                and leases == 0
                and {"HUMAN_ACCEPTED", "COMMIT_CREATED"}
                <= {row["event_type"] for row in event_rows}
            )
            if not reconstructed:
                raise RuntimeError(
                    "canary ledger could not reconstruct accepted lifecycle"
                )
            artifacts = len(list((root / "runs" / job.id).iterdir()))
            return {
                "state": "ACCEPTED",
                "commit": commit,
                "events": len(event_rows),
                "successful_attempts": 1,
                "model_requests": len(requests),
                "unreconciled_quota_reservations": reserved,
                "active_leases": leases,
                "reconstructable": reconstructed,
                "audit_artifacts": artifacts,
                "verifier_sandbox": verifier_sandbox,
                "builder_sandbox": builder_sandbox,
            }
        finally:
            server.shutdown()
            server.server_close()
