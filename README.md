# agent-shims

Route agent CLIs through the shared OpenCode Zen free-model pool.

```
                    ┌──────────────────────────┐
   Codex CLI ──────►│  codex_shim    :18893    │──┐
   (Responses)      │  Responses → chat        │  │
                    └──────────────────────────┘  │
                                                  │   ┌─────────────────┐
                    ┌──────────────────────────┐  ├──►│  zen-router     │
   OpenCode ───────►│  opencode_shim :18894    │──┘   │  :18892         │──► opencode.ai/zen
   (Responses/chat) │  passthrough + translate │      │  pool + failover│
                    └──────────────────────────┘      └─────────────────┘
                                                     one router, many shims
```

## Why this exists

Two facts, measured on this host, force the design:

1. **The Zen free tier is gated on request shape.** A request must carry the
   OpenCode client identity, `stream: true`, and the twelve harness tool
   names. A non-OpenCode client is refused with `403 FreeTierError`.
2. **Agent CLIs do not speak the pool's protocol.** Codex CLI v0.157 only
   accepts `wire_api = "responses"` (`chat` is rejected at config load), and
   OpenCode's Zen models are Responses-native — while the free tier that
   actually serves is reached over `chat/completions`.

A shim bridges one client to the pool. The **router is shared**: it owns
failover, cool-downs, quarantine and proof, so no shim duplicates that logic.

## Layout

```
agent-shims/
├── codex_shim.py        Codex CLI    -> :18893
├── opencode_shim.py     OpenCode     -> :18894
├── shared/
│   ├── gate.py          the free-tier gate: identity + harness tools
│   ├── responses.py     Responses <-> chat translation, both directions
│   └── shim_base.py     HTTP plumbing every shim inherits
└── tests/test_shims.py  34 unit tests, no network
```

**`gate.py` is the single source of the wire rules.** Every shim builds its
upstream request through it, so there is one copy and not one per protocol.

## Services

| Unit | Port | Role |
|---|---|---|
| `zen-router.service` | 18892 | shared pool (pre-existing) |
| `codex-shim.service` | 18893 | Codex CLI front end |
| `opencode-shim.service` | 18894 | OpenCode front end |

```bash
systemctl --user status zen-router codex-shim opencode-shim
curl -s localhost:18893/healthz
```

## Configuring Codex

`~/.codex/config.toml`:

```toml
model = "mimo-v2.6-flash-free"
model_provider = "zen"

[model_providers.zen]
name = "Zen"
base_url = "http://127.0.0.1:18893/v1"
wire_api = "responses"
env_key = "ZEN_SHIM_API_KEY"
```

Then `codex exec "your task"`. For non-interactive runs add
`--skip-git-repo-check` and, in a gateway/service context,
`--sandbox danger-full-access` (bubblewrap fails there).

## Configuring OpenCode

Point a custom OpenAI-compatible provider at `http://127.0.0.1:18894/v1`.
The shim accepts Responses *or* chat on the same port and answers in kind, so
the caller does not have to know which vocabulary it speaks.

## What was measured, not assumed

Every claim below came from a command run on this host:

- **The free models can drive an agent loop.** All five pool members returned
  a valid `exec_command` call with parseable arguments
  (`{"cmd": "echo HELLO"}`).
- **The gate needs the harness names, not the client's.** A request carrying
  only Codex's tools (`exec_command`, …) is refused; adding the twelve harness
  names makes the same request serve, and the model still calls the *caller's*
  tool.
- **A Responses-native request 403s unshaped.** Without the identity headers
  the upstream returns 403; with them it clears the WAF.
- **`wire_api = "chat"` is dead.** Codex 0.157.1: `` `wire_api = "chat"` is no
  longer supported`` — hence the Responses translation, not a passthrough.
- **Codex rejects an argument combination, not just unknown fields.**
  `justification` is legal only with `sandbox_permissions`; sent alone, the
  whole call is refused. `shared/responses.py` repairs this from the client's
  own schema.

## Known limitations

- **Model quality is the ceiling.** The free models complete short agent loops
  reliably; a longer multi-step task can stall without emitting a tool call.
  The shim translates correctly — it cannot make a small model plan.
- **The gate is a moving target.** It has been tightened deliberately before
  (opencode#544) and can be again. `assets/harness.json` is the thing to
  refresh when it is.
- **No auth on the shims.** Loopback-only, like the router. Keep it that way.
- **The harness tools are injected into every request**, costing prompt tokens
  (~12 small schemas). Their descriptions say plainly that they are not
  callable, and a harness call that leaks into a reply is dropped.
