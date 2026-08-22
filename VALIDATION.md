# Validation status

Validated in the build environment on 2026-08-21:

- Python bytecode compilation: pass
- 11 automated tests: pass
- Git worktree creation and controller-owned commit lifecycle: pass
- deterministic test failure detection: pass
- protected-path and secret scanning including newly-created/untracked files: pass
- scheduler cold-start behavior: pass
- human ACCEPT attribution to the exact READY attempt: pass
- shared physical-resource concurrency blocking: pass
- expired lease / crash recovery: pass
- complete mocked model cycle (HTTP model -> shell edits -> tests -> verifier -> READY -> human accept -> commit): pass
- mocked Jules REST ChangeSet -> controller worktree patch application: pass
- source distribution wheel build: pass (`poormans_code-0.1.0-py3-none-any.whl`)

Not live-tested here:

- a real OpenHands SDK/Agent Server session, because the OpenHands packages are not installed in this offline build environment;
- a real Jules session, because no user credentials/quota are exposed to this environment;
- real Groq/Gemini/Ollama inference, for the same credential/network reason;
- Tailscale connectivity between the user's physical workers.

Those integrations remain behind executor/provider boundaries. Failures there do not alter the SQLite/Git/verifier architecture, and the CLI's `doctor` command is intended to expose missing local dependencies/configuration before jobs are dispatched.
