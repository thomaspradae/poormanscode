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
Provider credentials are not placed in the worker environment. A remote
conversation can still serialize the model configuration to the Agent Server;
this is why every provider/model pair must pass a real multi-turn coding canary
before production enablement.

At present the Gemini/OpenHands pair is quarantined because Gemini's tool-call
protocol requires `thought_signature` data that the current OpenHands/LiteLLM
path does not preserve. Mistral/OpenHands passes the basic smoke but hit provider
rate limits during a multi-turn canary. Both candidates remain disabled until a
provider-specific adapter and request-level accounting can prove a complete
coding lifecycle.
