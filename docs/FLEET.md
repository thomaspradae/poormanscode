# Fleet setup

Each physical machine can be represented as a `resource_group`. If it can run only one expensive agent/model process at a time, use `resource_concurrency = 1` on every candidate assigned to that machine.

## OpenHands worker

OpenHands currently documents its Agent Server as the remote HTTP/WebSocket execution service and supports running it on on-prem machines. Install the Agent Server in a Python environment appropriate for the current OpenHands release, then run it only on an interface protected by Tailscale/firewall policy.

Typical development launch:

```bash
python -m openhands.agent_server --host 0.0.0.0 --port 8000
```

Then configure a PMC candidate:

```toml
[[candidates]]
name = "ofi2-qwen-openhands"
enabled = true
executor = "openhands"
role = "builder"
model = "openai/qwen2.5-coder:7b"
base_url = "http://127.0.0.1:11434/v1"
server_url = "http://100.x.y.z:8000"
resource_group = "ofi2"
resource_concurrency = 1
quota_group = "local-ofi2"
```

The `base_url` above is evaluated by the remote agent process, so `127.0.0.1` means Ollama on the worker itself.

## Verification-only machines

PMC's current verifier runs on the controller worktree. A future remote verifier can use the same executor/resource boundary, but do not move acceptance logic into the coding runtime. The important invariant is that verification remains independent of the authoring agent.

## Secrets

Do not put API keys in repository files or candidate config. Use environment variables named by `api_key_env`. The BashExecutor strips token/key/secret/password variables from child shell commands so repository code does not inherit provider credentials.
