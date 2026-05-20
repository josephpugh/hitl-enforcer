# Regulated Trade Agent — End-to-End Demo

A working demo of a regulated trading agent built on three local processes plus an embedded MCP server. Every consequential action (`place_trade`) is gated by a Prepare → Approve → Commit lifecycle with cryptographically linked, signed evidence on disk.

> See [DESIGN_SPEC.md](DESIGN_SPEC.md) for the full design rationale.

---

## Components

| Component                | Port  | Stack                                |
|--------------------------|-------|--------------------------------------|
| Chat UI                  | 5173  | Vite + React + TypeScript            |
| Agent backend (WS + MCP) | 8787  | Python, FastAPI, OpenAI SDK          |
| Confirmation surface     | 8788  | Python, FastAPI, Jinja2              |

The MCP server lives **in-process** with the agent backend. Per the spec it's "embedded with the agent for simplicity."

---

## Setup

```sh
# Python deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt

# Chat UI deps
cd chat-ui && npm install && cd ..

# Configure
cp .env.example .env
# Edit .env to set OPENAI_API_KEY
```

Keys (`./keys/server_ed25519.{priv,pub}`, `./keys/confirmer_ed25519.{priv,pub}`, `./keys/trust_bundle.json`) are generated automatically on first run.

---

## Run

Open three terminals (or use a process manager).

**Terminal 1 — agent backend:**
```sh
.venv/bin/python -m backend
```

**Terminal 2 — confirmation surface:**
```sh
.venv/bin/python -m confirmer
```

**Terminal 3 — chat UI:**
```sh
cd chat-ui && npm run dev
```

Then open <http://localhost:5173>.

Run all three commands from the project root. `.venv/bin/python` is used explicitly so you don't need to `source .venv/bin/activate` first.

If you see `[Errno 48] address already in use`, an old process is on the port. Find it and kill it:
```sh
lsof -nP -iTCP:8787 -sTCP:LISTEN    # or 8788
kill <PID>
```

---

## Demo script (5 minutes)

1. Open the chat UI, type **"Buy 100 shares of ORCL."**
2. The model calls `place_trade("ORCL", 100)`. Backend stages the intent, signs it with the server key, writes `evidence/intents/{id}.json`, and emits an `approval_required` frame.
3. Chat UI renders an **Approval Card** with the trade summary and — embedded inline — the deterministic confirmation form delivered as a sandboxed iframe from the confirmation surface (port 8788). Below the card three chips appear, the first already labelled *Intent signed (a1b2…)*. A small "Open in a new tab" fallback link sits below the iframe.
4. The iframe self-reports its content height to the chat UI via `postMessage`, and the chat UI resizes the iframe smoothly to fit. The confirmer's `Content-Security-Policy: frame-ancestors 'self' http://localhost:5173` header restricts who can embed it.
5. Click **Approve** inside the iframe. The surface re-renders the same HTML, hashes it, builds a receipt with the `display_manifest` (including `viewed_at` and `dwell_ms` — how long the page was visible before the click), signs it with the confirmer key, writes `evidence/receipts/{id}.json`, and POSTs to `/elicitation/resolve/{id}` on the backend.
6. The completed page inside the iframe `postMessage`s `{type:"hitl_decision", decision:"approve"}` to the chat UI (origin-checked). The card flips to **"Approved ✓"** instantly.
7. The MCP server runs all 8 verification checks. All pass. It atomically creates the `evidence/consumed/{id}` sentinel, runs the simulated broker, signs the execution record, writes `evidence/executions/{id}.json`.
8. The *Receipt signed* + *Execution signed* chips light up via the artifact poll. Clicking any chip pops a JSON modal showing the exact artifact on disk.
9. Drop to a terminal and run the auditor's view:
   ```sh
   .venv/bin/python tools/verify.py --approval-id 01J...
   ```
   It prints `[OK]` for every check, surfaces `Dwell time before decision: N ms`, and ends with `VERIFIED: …`.

---

## Failure scenarios you can demo

| Scenario | What to do | Expected |
|----------|-----------|----------|
| Decline | Click **Decline** | Receipt with `decision:"decline"`. No execution record. |
| Expiry | Wait 60 s on the approve page before clicking | Surface refuses, MCP returns "Approval expired." |
| Double-approve | Open the URL in two tabs, approve both | Second POST → 409. |
| Receipt tamper | Edit `evidence/receipts/{id}.json` to change quantity | `verify.py` flags signature mismatch. |
| Replay | Manually delete `evidence/consumed/{id}` and retrigger | Sentinel check refuses. |
| Quantity over limit | "Buy 11000 shares of ORCL" | MCP rejects pre-stage (`max_quantity` policy). |

---

## File layout

```
hitl-enforcer/
├── DESIGN_SPEC.md
├── README.md
├── .env.example
├── backend/
│   ├── __main__.py          # entrypoint: python -m backend
│   ├── app.py               # FastAPI + WebSocket + elicitation-resolve callback
│   ├── config.py
│   ├── agent/agent_loop.py  # OpenAI tool-call loop
│   ├── crypto/              # keys, JCS, sign/verify envelope
│   ├── evidence/            # store + hash-chained journal
│   └── mcp_server/
│       ├── server.py
│       ├── policy.py
│       ├── verify.py        # 8 verification checks
│       ├── broker.py        # simulated fill
│       ├── elicitation.py   # in-process MCP elicit-URL bridge
│       ├── intent_builder.py
│       └── tools/place_trade.py
├── confirmer/
│   ├── __main__.py
│   ├── app.py               # FastAPI + frame-ancestors CSP + dwell capture
│   ├── templates/
│   │   ├── trade-confirm-v1.html
│   │   ├── completed.html       # postMessages decision to parent
│   │   └── error.html
│   └── static/
│       ├── style.css
│       └── resize-bridge.js     # postMessages content height to parent
├── chat-ui/                 # Vite + React + TS
│   └── src/components/ApprovalCard.tsx   # iframe + origin-checked listener
├── tools/verify.py          # standalone audit CLI
├── tests/                   # pytest + live E2E script
├── keys/                    # generated on first boot (gitignored)
└── evidence/                # written at runtime (gitignored)
```

---

## Audit fields captured in the approval receipt

Each receipt is signed by the confirmer key and includes, in addition to the spec's baseline fields:

| Field | Purpose |
|-------|---------|
| `approved_at` | When the user POSTed the decision. |
| `viewed_at` | When the approval page was most recently rendered to the user. |
| `dwell_ms` | Milliseconds between most-recent render and the decision. Anti-clickjacking / informed-consent signal. |
| `display_manifest.template_id` + `template_version` | Which deterministic template rendered the page. |
| `display_manifest.rendered_digest` | SHA-256 of the exact HTML the user saw. |
| `display_manifest.material_fields_shown` | Every policy-required field with its value as rendered. |

`viewed_at` and `dwell_ms` are `null` if the decider POSTed without ever GETting the page (e.g. script, replay, bookmark). The verifier prints a `[WARN]` in that case.

---

## Embedded confirmation surface (security model)

The confirmation surface is delivered to the chat UI as a **sandboxed iframe** so the trade form appears inline in chat. Importantly, this preserves the audit story:

- Same trusted origin renders the deterministic template
- Same confirmer key signs the receipt
- Same `rendered_digest` reproducible from disk

The chat UI is the picture frame, not the picture. Cross-origin isolation prevents the chat UI from reading or scripting the iframe's contents. Two messages flow back from the iframe via `window.postMessage`, both **origin-checked** by the chat UI:

- `{type: "hitl_decision", approval_id, decision}` — emitted by `completed.html` on load.
- `{type: "hitl_resize", height}` — emitted by `resize-bridge.js` whenever the iframe content changes size.

The confirmer sets `Content-Security-Policy: frame-ancestors 'self' http://localhost:5173` so only the chat UI (and the confirmer itself) can embed it.

---

## Notes

- The agent loop uses `chat.completions` with tool-calls (not the Responses API) because the tool-call shape is simpler and the demo semantics — tool description, HITL block, structured tool result — are identical.
- The "MCP elicitation-URL" primitive is implemented in-process via an asyncio broker (`backend/mcp_server/elicitation.py`). The confirmation surface notifies the backend over HTTP after writing the receipt; the broker then completes the awaiting future, exactly as the protocol-level elicitation-URL flow would.
- No HSM. Keys live on disk at `./keys/` (gitignored). The confirmer key is distinct from the server key so an auditor can attribute who signed what.
- Conversational state lives only within a single WebSocket session (in-memory). Disconnect/refresh creates a new agent session; the evidence chain on disk is the durable record.

---

## Tests

```sh
.venv/bin/python -m pytest tests/ -v          # 9 failure-scenario tests
.venv/bin/python tests/check_dwell_live.py    # live dwell-time round-trip
.venv/bin/python tools/verify.py --all        # walk every artifact on disk
```
