# Regulated Trade Agent — End-to-End Demo

A working reference implementation of an MCP-resident **human-in-the-loop enforcement** pattern for regulated agentic actions. Every consequential tool call (`place_trade`) is gated by a Prepare → Approve → Commit lifecycle with cryptographically linked, signed evidence on disk. The model can't bypass it; the chat UI can't bypass it; the gate lives in server code, not prompt instructions.

Built on the real **MCP Streamable HTTP transport** (spec [2025-03-26](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports#streamable-http)) and the SDK's first-class **URL-mode elicitation** primitive (`ctx.elicit_url`) — not a hand-rolled broker.

Trading is the example domain — the pattern carries unchanged to `send_wire`, `submit_compliance_filing`, `delete_customer_record`, or any other action that crosses a regulatory bright line.

> See [DESIGN_SPEC.md](DESIGN_SPEC.md) for the full design rationale.

---

## Why this exists

US financial services firms are entering a near-term regulatory environment that will require demonstrable human-in-the-loop control over agentic actions — not "agent makes a recommendation that a human reads," but **"agent proposes a regulated action that a verified human approves before it executes, with a tamper-evident artifact of every link in the chain."**

The clearest current signal is **FINRA's 2026 Annual Regulatory Oversight Report** (Dec 2025), which dedicates a standalone subsection to AI agents and states that agents capable of acting or transacting require *"explicit human checkpoints before execution"* together with narrow scope, permissions, and operation-level audit trails. FINRA examiners are walking into firms with that document in hand now; firms that cannot demonstrate compliance with its expectations face findings, corrective action, and disciplinary exposure.

Other regulatory vectors stacking on top:

- **NYDFS Part 500** (Oct 2024 industry letter from Superintendent Harris) — covered entities' cybersecurity programs must include AI. Audit trails for AI access to NPI must be at an operation level; *"standard API call logs and LLM inference logs do not satisfy this requirement."*
- **OCC Bulletin 2026-13 / SR 26-02** (Apr 2026) replaced SR 11-7. Generative and agentic AI are explicitly out of scope of the new MRM framework, but supervisors are applying MRM principles by analogy today and an interagency RFI on agentic AI has been signaled.
- **SEC Division of Examinations** named AI governance an examination priority for 2025.
- **GLBA Safeguards Rule** (2023 amendments) — AI agents accessing NPI are subject to the same access control, audit log, and minimum-necessary standards as human employees.
- **CFPB / UDAAP** — incorrect output from an AI chatbot has been found to constitute a UDAAP violation. An enforced approval gate is a direct defense for any consumer-facing action.
- **EU AI Act Article 14** — relevant for firms with EU nexus; effective for most high-risk Annex III systems Aug 2, 2026. Codifies meaningful human oversight, including explicit attention to **automation bias**.
- **Colorado AI Act** — covers high-risk AI in financial services with developer/deployer obligations; effective Jun 30, 2026.

The architectural shape regulators are converging on, across all of the above:

1. **Tool-action risk classification** — documented, versioned policy that classifies tools by risk tier and oversight model.
2. **Approval gate at the protocol layer, not the model layer** — the agent can't decide whether something needs approval and can't fabricate the approval.
3. **Tamper-evident audit chain** tying each action to the proposing agent, the prompt context, the approver identity, the policy that flagged it, and the downstream effect.
4. **Review-quality signal** — time-per-review, override rate, reviewer authority. "Click-OK" with 0 % override rate fails the meaningful-oversight test.
5. **Single-use, action-bound approvals** — one approval, one specific resolved action, time-bounded.

This repository implements all five inside a single MCP server plus a deterministic confirmation surface — no shared service, no platform team, no governance product. The only durable dependency is a write-once storage tier (filesystem here; S3 Object Lock / Azure Immutable Blob / GCS Bucket Lock in production).

### How the implementation maps to those expectations

| Expectation | Where it lives in this repo |
|---|---|
| Explicit human checkpoint before execution (FINRA) | `place_trade` blocks on `BROKER.elicit_url` until a signed receipt is written and verified ([place_trade.py:108](backend/mcp_server/tools/place_trade.py#L108)). |
| Operation-level audit trail (NYDFS Part 500) | Three signed artifacts per regulated action — `order_intent`, `approval_receipt`, `execution_record` — linked by `intent_digest` and `receipt_digest`, plus an append-only `journal.ndjson` with `prev_hash` chaining ([store.py](backend/evidence/store.py), [journal.py](backend/evidence/journal.py)). |
| Tamper-evident artifacts | RFC 8785 JCS canonicalization + Ed25519 signatures, distinct keys per signer (server vs. confirmer), independent verifier ([tools/verify.py](tools/verify.py)) that runs against disk + public keys alone. |
| Single-use, action-bound approval | Receipt carries `intent_digest` over canonicalized intent. Atomic `O_CREAT\|O_EXCL` sentinel at `evidence/consumed/{id}` prevents replay. Receipt expires in `max_approval_age_seconds`. Verification checks 5–7 enforce all of this. |
| Meaningful oversight, not rubber-stamping (Article 14(4)(b), automation bias) | Confirmation surface renders the *resolved* action — real ticker, real quantity, real account ID, not symbolic IDs. Receipt captures `display_manifest.rendered_digest` (SHA-256 of the exact HTML the user saw), `material_fields_shown`, `viewed_at`, and `dwell_ms` — so review-quality metrics are derivable directly from the artifact. |
| Approver identity binding | Receipt records `approver.subject`, `auth_method`, `auth_assertion_digest`. Verification check 8 enforces `approver.subject == intent.caller.principal`. |
| Approval surface outside the agent | Confirmation surface (port 8788) runs as a separate process. The chat UI embeds it as a sandboxed iframe with `frame-ancestors` CSP. The agent never holds the receipt-signing key; a compromised chat UI cannot fabricate a receipt that passes verification. |
| Policy as code, external to agent reasoning | `backend/mcp_server/policy.py` registers per-tool policies (rule_id, rule_version, required material fields, max approval age, quantity caps). The LLM cannot override. |
| Independent verification by a third party | `python tools/verify.py --approval-id <id>` reproduces the entire chain from `./evidence/` + `./keys/trust_bundle.json` and prints `[OK]` for each of 8 checks plus dwell time. |

### What's stubbed for the demo vs. what production needs

| Demo concession | Production substitute |
|---|---|
| Ed25519 keys on local disk (`./keys/`) | KMS-resident, non-exportable signing keys (AWS KMS, Azure Key Vault, GCP KMS). The MCP server gets `kms:Sign` on a single key and never holds private material. |
| Filesystem evidence store with append-only journal | WORM object store with the same chain semantics — S3 Object Lock (Compliance mode), Azure Immutable Blob with time-based retention, or GCS Bucket Lock. MCP server role can write; nothing can delete or modify, including the same role. |
| Hardcoded `DEMO_PRINCIPAL` as approver | OIDC against your enterprise IdP (Okta, Entra, Ping). **Step-up auth** (MFA / WebAuthn) at the moment of approval, not just session reuse. Auth assurance level recorded in the receipt. |
| No event stream to SIEM | Emit `approval_requested`, `approval_decided`, `regulated_action_executed`, `policy_denied` events to your SIEM (Splunk, Sentinel, Chronicle, QRadar, Elastic). Detection rules: anomalous override rate, sub-second decisions, repeated denials with rephrased args (prompt-injection signal), out-of-hours approvals. **The artifact store is the system of record; the SIEM is the detection lens — not a replacement.** |
| Single approver (1-of-1) | N-of-M approval schema in the artifact format from day one. Several US sector rules (high-value wires, biometric ID, some PHI ops) require dual control. Bolting this on later means a schema migration of the chain, which is exactly the kind of thing that breaks audits. |
| All processes on one host | Confirmation surface and artifact store deployed in trust boundaries separate from the agent runtime. The agent's IAM principal cannot write to the artifact store; only the approval service can. |
| Static policy in Python | Externalized policy decision point (OPA, Cedar, or vendor PDP) with versioned rule changes going through change control. |

The point of the demo is to show that the architectural shape — **enforced policy gate outside the agent, deterministic confirmation surface, signed evidence chain** — works end-to-end with independent cryptographic verification, with no dependency on any vendor governance product.

---

## Components

| Component                | Port  | Stack                                |
|--------------------------|-------|--------------------------------------|
| Chat UI                  | 5173  | Vite + React + TypeScript            |
| Agent backend            | 8787  | Python, FastAPI, OpenAI SDK, MCP client + server |
| Confirmation surface     | 8788  | Python, FastAPI, Jinja2              |

The FastMCP server is mounted at **`http://localhost:8787/mcp`** as a real Streamable HTTP endpoint. The agent loop runs a `ClientSession` against that endpoint over loopback HTTP, so the tool surface looks identical to an external MCP host (Claude Desktop, an IDE plugin, etc.). The chat UI talks to the agent backend over **SSE-over-POST** (`POST /chat` returns a `text/event-stream` for one turn) — every transport in the project is HTTP, no WebSockets anywhere.

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
│   ├── app.py               # FastAPI; mounts FastMCP at /mcp, exposes POST /chat (SSE) + /elicitation/resolve
│   ├── config.py
│   ├── agent/agent_loop.py  # OpenAI tool-call loop driving an MCP ClientSession
│   ├── crypto/              # keys, JCS, sign/verify envelope
│   ├── evidence/            # store + hash-chained journal
│   └── mcp_server/
│       ├── server.py        # FastMCP instance (Streamable HTTP)
│       ├── policy.py
│       ├── verify.py        # 8 verification checks
│       ├── broker.py        # simulated fill
│       ├── elicitation.py   # PENDING: per-approval-id futures the OOB callback resolves
│       ├── intent_builder.py
│       └── tools/place_trade.py   # uses ctx.elicit_url + awaits PENDING
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

## How the wire actually moves (HTTP all the way down)

```
┌───────────┐  POST /chat  ┌─────────────────────────────┐  POST /elicitation/resolve  ┌──────────────┐
│  Chat UI  │ ───────────► │      Agent backend          │ ◄────────────────────────── │  Confirmer   │
│  (5173)   │              │  (8787)                     │                              │  (8788)      │
│           │ ◄── SSE ──── │                             │                              │              │
│           │ stream (one  │  ┌───────────────────────┐  │                              │ writes signed│
│           │ event per    │  │ OpenAI loop           │  │                              │ receipt;     │
│           │ turn)        │  │ + MCP ClientSession   │  │                              │ POSTs OOB    │
└───────────┘              │  │   elicitation_callback│  │                              │ decision     │
                           │  └────────────┬──────────┘  │                              └──────────────┘
                           │   POST + SSE on /mcp        │                                       ▲
                           │  ┌────────────▼──────────┐  │                                       │ user
                           │  │ FastMCP server        │  │                                       │ click
                           │  │   place_trade tool    │  │                                       │
                           │  │   ctx.elicit_url      │  │           sandboxed                   │
                           │  │   awaits PENDING fut  │──┼───────►  iframe in chat ──────────────┘
                           │  └───────────────────────┘  │
                           └─────────────────────────────┘
```

Three transports, all HTTP:

| Hop | Protocol | Endpoint |
|---|---|---|
| Chat UI ↔ Agent backend | `POST /chat` → `text/event-stream` (one SSE stream per turn) | `/chat` |
| Agent backend ↔ MCP server | MCP Streamable HTTP (POST + SSE) | `/mcp` |
| Confirmer → Agent backend | HTTP POST | `/elicitation/resolve/{id}` |
| Chat UI ↔ Confirmer | HTTP via sandboxed iframe | `/approve/{id}` |

A `place_trade` turn end-to-end:

1. Chat UI `POST`s `{text: "buy 100 ORCL", session_id}` to `/chat`. The server returns `Content-Type: text/event-stream` and opens an SSE stream for that turn.
2. Agent loop calls `mcp_session.call_tool("place_trade", …)` over the Streamable HTTP `/mcp` endpoint. The MCP server returns its own `text/event-stream` to the agent-backend-as-MCP-client and keeps that SSE stream open.
3. The `place_trade` tool signs and writes the intent, then calls `ctx.elicit_url(message, url, elicitation_id=approval_id)`. That sends an `elicitation/create` request (mode=`url`) back to the MCP client as a server-initiated JSON-RPC message on the open MCP SSE stream.
4. The MCP client's `elicitation_callback` fires. It pushes an `approval_required` event into the **chat turn's SSE queue** and immediately returns `ElicitResult(action="accept")` — the spec-defined "consent to navigate." The OOB decision is *not* carried by the elicitation response.
5. The chat UI receives the `approval_required` SSE event and renders the sandboxed iframe pointing at the confirmer's `/approve/{id}`.
6. The tool registers a `PENDING` future keyed by `approval_id`, then `await`s it.
7. User clicks Approve. The confirmer signs the receipt, writes `evidence/receipts/{id}.json`, and POSTs `{"action":"accept"}` to `/elicitation/resolve/{id}` on the agent backend.
8. That HTTP handler calls `PENDING.resolve(approval_id, "approve")`, completing the future.
9. The `place_trade` tool resumes — runs all 8 verification checks, atomically consumes the single-use sentinel, executes the simulated broker, writes the execution record, calls `ctx.session.send_elicit_complete(elicitation_id)` to formally close the elicitation, and returns the human summary.
10. The MCP server emits the `tools/call` response on its SSE stream, then closes it.
11. The agent loop sees the tool result, feeds it back to OpenAI, and pushes `assistant_text` + `assistant_done` events to the chat turn's SSE stream. The chat UI's `fetch` reader sees them and updates the UI; the SSE stream closes.

The protocol-level parts (steps 2, 3, 9, 10) are plain MCP Streamable HTTP. The chat transport (steps 1, 4–5, 11) is plain SSE-over-POST. The OOB approval (steps 6–8) is *deliberately* outside the protocol so a compromised MCP host can't fabricate a receipt that passes the server's verification.

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
- The MCP server is a real FastMCP instance exposed over Streamable HTTP at `/mcp`. The agent backend opens an MCP `ClientSession` per chat session, lists tools once, and reuses the session across turns — same shape any external MCP host would see.
- The chat UI's transport is **SSE-over-POST**. Each turn is one `POST /chat` whose response is a `text/event-stream`. Sessions are kept alive in a server-side registry keyed by `session_id`; the first POST without an id creates one and returns it via the `X-Session-Id` header.
- URL-mode elicitation (`ctx.elicit_url`) is the spec primitive. The protocol response carries the user's consent to navigate; the OOB decision (approve / decline) travels through the confirmer → `/elicitation/resolve/{id}` HTTP callback into the in-process `PENDING` future the tool is awaiting. When the tool completes, it calls `ctx.session.send_elicit_complete(elicitation_id)` to formally close the elicitation.
- No HSM. Keys live on disk at `./keys/` (gitignored). The confirmer key is distinct from the server key so an auditor can attribute who signed what.
- Conversational state lives only in the in-memory chat session. Process restart or session timeout creates a fresh session with a new MCP client; the evidence chain on disk is the durable record.

---

## Tests

```sh
.venv/bin/python -m pytest tests/ -v          # 9 failure-scenario tests
.venv/bin/python tests/check_dwell_live.py    # live dwell-time round-trip
.venv/bin/python tools/verify.py --all        # walk every artifact on disk
```
