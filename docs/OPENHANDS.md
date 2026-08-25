# OpenHands deployment

PMC's OpenHands workspace server is deployed on the `pmc-node-worker` LXD
container on `uace-ofi-01`.

- OpenHands Agent Server/SDK: 1.43.1
- systemd unit: `openhands-agent-server.service`
- service identity: `openhands` (unprivileged)
- listener: container loopback, published only on the worker's Tailscale address
- authentication: a dedicated `OPENHANDS_SESSION_KEY`, separate from model keys
- workspace canary: authenticated upload, command execution, and cleanup

The PMC adapter passes a separate `server_api_key_env` to the remote workspace.
For the built-in OpenHands agent, provider credentials are serialized as model
configuration rather than exported into tool subprocesses. ACP agents have a
different contract: the selected credential is injected into the ACP subprocess
through OpenHands' secret registry and masked in events. The Gemini CLI system
policy additionally enables environment-variable redaction and blocks
`GEMINI_API_KEY` from shell-tool environments. Every provider/agent pair must
still pass a real coding canary before production enablement.

Gemini has three separately versioned paths:

- Native OpenHands is disabled: OpenHands/LiteLLM loses Gemini 3's required
  `thought_signature` on multi-turn function calls.
- Non-native OpenHands is disabled: it avoids the signature error but consumed
  the full 20-request daily allowance without completing a tiny repair.
- Gemini CLI over ACP is the preferred path. Google owns the function/tool
  history, OpenHands transports the ACP conversation, and PMC retains worktree,
  patch, verifier, accounting, and acceptance authority.

The ACP file-write conformance gate passes both with the CLI auto-router and
when pinned to `gemini-3.7-flash`. The pinned candidate remains disabled until
an L3/L4 coding canary can run after provider daily quota resets. The auto-router
candidate is also disabled after exhausting its current daily quota; neither is
eligible for production merely because its adapter passed an earlier smoke.

OpenHands remote patch capture stages the remote workspace before exporting a
binary diff. This is required to include newly-created files. Remote Git
initialization uses an allow-empty baseline so skeletal repositories work.

Mistral/OpenHands passes basic generation but remains disabled after multi-turn
provider rate limits. It is independent of the Gemini ACP path.
