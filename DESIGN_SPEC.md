# Regulated Trade Agent — End-to-End Demo Design Spec

A working demo of a regulated trading agent built on:

- **Chat UX** (browser) — standard chat interface, plus an inline approval card surfaced from MCP elicitation.
- **Agent backend** — Python service hosting the OpenAI Responses API loop and an embedded MCP server.
- **Embedded MCP server** — exposes `place_trade(ticker, quantity)`, enforces three-phase Prepare → Approve → Commit, uses elicitation-URL mode to direct the user to the confirmation surface.
- **Confirmation surface** — a controlled, deterministic web page that renders the staged intent and produces a signed approval receipt.
- **Evidence store** — append-only file-system artifacts for every intent, receipt, and execution record. All artifacts are canonicalized and signed.

The whole thing runs locally. Nothing leaves the box except calls to the OpenAI API.

---

## 1. Goals and Non-Goals

### Goals
- Demonstrate the full Prepare / Approve / Commit lifecycle with cryptographically linked, signed artifacts at every stage.
- Show MCP elicitation-URL mode delivering the approval URL through the protocol (not through LLM prose).
- Show the chat UI rendering an inline approval card in response to the elicitation, with the user clicking through to the confirmation surface.
- Produce an auditable evidence bundle on disk that a regulator could reconstruct the entire decision chain from.
- Be runnable with `python -m server` and `npm run dev` (or equivalent) — no cloud infra.

### Non-Goals
- Real brokerage integration. The "execution" writes a fill record to disk.
- Real identity. The "approver" is identified by a header value or hardcoded SSO subject.
- HSM-backed key management. We use a local Ed25519 keypair stored on disk, generated at first boot.
- Multi-user concurrency. Single user, single session.

---

## 2. High-Level Architecture

```
┌──────────────────┐        ┌────────────────────────────────────────┐
│                  │  WS    │           Agent Backend                │
│   Chat UI        │◄──────►│  ┌──────────────────────────────────┐  │
│   (React/Vite)   │        │  │  OpenAI Responses API loop       │  │
│                  │        │  │  + embedded MCP client           │  │
│                  │        │  └──────────────┬───────────────────┘  │
│  + Approval Card │        │                 │ in-process MCP        │
│                  │        │  ┌──────────────▼───────────────────┐  │
│                  │        │  │  MCP server: trade-server        │  │
│                  │        │  │   tool: place_trade              │  │
│                  │        │  │   - Phase 1: stage intent        │  │
│                  │        │  │   - Phase 2: elicit_url          │  │
│                  │        │  │   - Phase 3: verify + commit     │  │
│                  │        │  └──────────────┬───────────────────┘  │
└──────────┬───────┘        │                 │                       │
           │ HTTP redirect  │  ┌──────────────▼───────────────────┐  │
           │ to approval    │  │  Evidence Store (file system)    │  │
           │ URL            │  │   ./evidence/intents/            │  │
           │                │  │   ./evidence/receipts/           │  │
           ▼                │  │   ./evidence/executions/         │  │
┌──────────────────┐        │  │   ./evidence/journal.ndjson      │  │
│ Confirmation     │◄───────┤  └──────────────────────────────────┘  │
│ Surface          │  HTTP  │                                        │
│ (FastAPI page)   │        └────────────────────────────────────────┘
│ + Approve / Decline
└──────────────────┘
```

Three processes / three ports:

| Component                | Port  | Tech                                        |
|--------------------------|-------|---------------------------------------------|
| Chat UI                  | 5173  | Vite + React                                |
| Agent backend (WS + MCP) | 8787  | Python, FastAPI, `mcp` SDK, OpenAI SDK       |
| Confirmation surface     | 8788  | Python, FastAPI, Jinja2 template            |

The MCP server is **in-process** with the agent backend (FastMCP mounted directly), satisfying the "embedded with the agent for simplicity" requirement.

---

## 3. Core Design Principles (non-negotiable)

These come directly from prior discussion and are what make the artifact chain audit-defensible:

1. **Deterministic templates, not LLM-generated confirmations.** The confirmation surface renders from a versioned Jinja2 template (`trade-confirm-v1`). The LLM never composes approval text.
2. **Canonical payloads before signing.** All signed objects use [RFC 8785 JSON Canonicalization Scheme (JCS)](https://datatracker.ietf.org/doc/html/rfc8785). Defaults (`order_type=MARKET`, `time_in_force=DAY`) are resolved and serialized into the canonical form.
3. **Single-use receipts with short expiry.** Approval receipts are nonce-bound, time-bound (60s default), and atomically consumed at execution. Replay is detected and rejected.
4. **Server-side policy enforcement.** The MCP server decides that `place_trade` requires HITL. The LLM cannot disable this. The client cannot bypass this.
5. **Append-only evidence storage.** Files are written, never modified. An NDJSON journal records every state transition with content hashes linking back to the artifact files.
6. **Display manifest in the receipt.** The receipt records `template_id`, `template_version`, the SHA-256 digest of the rendered HTML, and the explicit list of `material_fields` shown with their values. The MCP server refuses to commit if any policy-required field is missing.
7. **Linked artifacts.** Every artifact carries the hash of its predecessor (intent → receipt → execution). The chain is independently verifiable from disk alone.

---

## 4. Cryptographic Model

### 4.1 Keys
- **Server signing key** — Ed25519. Generated at first boot to `./keys/server_ed25519.{priv,pub}`. Used to sign intents and execution records.
- **Confirmation surface key** — Ed25519. Generated at first boot to `./keys/confirmer_ed25519.{priv,pub}`. Used to sign approval receipts.
- Distinct keys per component so an auditor can attribute who signed what.
- Public keys are also written to `./keys/trust_bundle.json` for the verifier tool.

### 4.2 Canonicalization
All signed payloads pass through JCS (RFC 8785) before signing. Library: [`rfc8785`](https://pypi.org/project/rfc8785/) for Python.

### 4.3 Signature envelope
Every signed artifact uses this envelope:

```json
{
  "payload": { ... },                 // the canonical object
  "signature": {
    "alg": "Ed25519",
    "kid": "server-2026-05",          // matches a key in trust_bundle.json
    "sig": "base64url(...)"           // signature over JCS(payload)
  }
}
```

Verification: re-canonicalize `payload`, look up `kid` in `trust_bundle.json`, verify `sig`. A standalone `verify.py` CLI does this and can be run by anyone with the disk contents.

---

## 5. The Three Artifacts

### 5.1 Order Intent (Phase 1 — Prepare)

Written to `./evidence/intents/{approval_id}.json`. Signed by the **server key**.

```json
{
  "payload": {
    "artifact_type": "order_intent",
    "schema_version": "1.0.0",
    "approval_id": "01J...ULID",
    "nonce": "base64url(16 bytes)",
    "created_at": "2026-05-19T14:22:01.123Z",
    "expires_at": "2026-05-19T14:23:01.123Z",
    "caller": {
      "agent_session_id": "sess_abc",
      "tool_name": "place_trade",
      "llm_model": "gpt-4.1",
      "principal": "user@example.com"
    },
    "action": {
      "tool": "place_trade",
      "args_raw": { "ticker": "ORCL", "quantity": 100 },
      "args_resolved": {
        "ticker": "ORCL",
        "quantity": 100,
        "side": "BUY",
        "order_type": "MARKET",
        "time_in_force": "DAY",
        "account_id": "DEMO-0001"
      }
    },
    "policy": {
      "rule_id": "TRADE_REQUIRES_HITL",
      "rule_version": "1.0.0",
      "required_material_fields": [
        "side", "ticker", "quantity", "order_type",
        "time_in_force", "account_id"
      ],
      "max_approval_age_seconds": 60,
      "single_use": true
    },
    "risk": {
      "destructive": true,
      "reversible": false,
      "estimated_notional_usd": null
    }
  },
  "signature": { "alg": "Ed25519", "kid": "server-...", "sig": "..." }
}
```

`approval_id` is a [ULID](https://github.com/ulid/spec) for time-orderable, human-grepable IDs.

### 5.2 Approval Receipt (Phase 2 — Approve)

Written to `./evidence/receipts/{approval_id}.json`. Signed by the **confirmer key**.

```json
{
  "payload": {
    "artifact_type": "approval_receipt",
    "schema_version": "1.0.0",
    "approval_id": "01J...ULID",
    "intent_digest": "sha256:hex(JCS(intent.payload))",
    "decision": "approve",
    "approver": {
      "subject": "user@example.com",
      "auth_method": "demo_session_header",
      "auth_assertion_digest": "sha256:..."
    },
    "approved_at": "2026-05-19T14:22:18.901Z",
    "display_manifest": {
      "template_id": "trade-confirm",
      "template_version": "1.0.0",
      "rendered_digest": "sha256:hex(rendered HTML)",
      "material_fields_shown": {
        "side": "BUY",
        "ticker": "ORCL",
        "quantity": 100,
        "order_type": "MARKET",
        "time_in_force": "DAY",
        "account_id": "DEMO-0001"
      },
      "locale": "en-US",
      "timezone": "America/New_York"
    },
    "client_meta": {
      "user_agent": "...",
      "ip": "127.0.0.1"
    }
  },
  "signature": { "alg": "Ed25519", "kid": "confirmer-...", "sig": "..." }
}
```

A **decline** receipt has `decision: "decline"` and no `display_manifest.material_fields_shown` requirement, but the rest of the envelope is the same.

### 5.3 Execution Record (Phase 3 — Commit)

Written to `./evidence/executions/{approval_id}.json`. Signed by the **server key**.

```json
{
  "payload": {
    "artifact_type": "execution_record",
    "schema_version": "1.0.0",
    "approval_id": "01J...ULID",
    "intent_digest": "sha256:...",
    "receipt_digest": "sha256:...",
    "executed_at": "2026-05-19T14:22:19.412Z",
    "outcome": "filled",
    "broker": {
      "name": "DemoBroker",
      "order_id": "DB-0001-...",
      "fill_price_usd": 142.31,
      "fill_quantity": 100,
      "venue": "SIMULATED"
    },
    "verification_checks": {
      "intent_signature_valid": true,
      "receipt_signature_valid": true,
      "intent_not_expired": true,
      "receipt_within_max_age": true,
      "payload_match": true,
      "material_fields_complete": true,
      "single_use_not_consumed": true,
      "approver_authorized": true
    }
  },
  "signature": { "alg": "Ed25519", "kid": "server-...", "sig": "..." }
}
```

### 5.4 Journal

`./evidence/journal.ndjson` — one line per state transition. Each line:

```json
{"ts":"...","event":"intent_staged","approval_id":"...","artifact":"intents/01J....json","sha256":"..."}
{"ts":"...","event":"receipt_recorded","approval_id":"...","artifact":"receipts/01J....json","sha256":"...","decision":"approve"}
{"ts":"...","event":"execution_committed","approval_id":"...","artifact":"executions/01J....json","sha256":"..."}
{"ts":"...","event":"receipt_consumed","approval_id":"..."}
```

The journal is append-only via `O_APPEND`. Each entry is hash-chained: every line includes `prev_hash` (sha256 of the previous line's serialized JSON), making truncation or insertion detectable.

---

## 6. Component Specs

### 6.1 Agent Backend (`/backend`)

**Stack:** Python 3.11+, FastAPI, `uvicorn`, `openai`, `mcp` (Python SDK), `pynacl`, `rfc8785`, `python-ulid`.

**Entry point:** `python -m backend.app`

**Responsibilities:**
- Host a WebSocket endpoint `/ws` for the chat UI.
- For each user message, run an OpenAI Responses API turn with tool access.
- Mount the embedded MCP server in-process (FastMCP), exposing `place_trade`.
- Bridge MCP elicitation events back to the WebSocket as a structured frame the chat UI knows how to render.

**WebSocket frames (server → client):**

```json
{ "type": "assistant_text", "delta": "..." }
{ "type": "tool_call", "name": "place_trade", "args": {...}, "call_id": "..." }
{ "type": "approval_required",
  "approval_id": "01J...",
  "url": "http://localhost:8788/approve/01J...",
  "summary": "BUY 100 ORCL — MARKET, DAY, account DEMO-0001",
  "expires_at": "..." }
{ "type": "tool_result", "name": "place_trade", "result": "...", "call_id": "..." }
{ "type": "assistant_done" }
```

**Client → server frames:**

```json
{ "type": "user_message", "text": "..." }
{ "type": "approval_status_check", "approval_id": "01J..." }   // optional poll trigger
```

**OpenAI loop:** Use the Responses API in streaming mode. Tools are described to the model with explicit purpose/when-to-use/when-not. The `place_trade` tool description tells the model:

> "Calling this tool initiates a regulated trade. The server will require human approval via a separate confirmation surface. The tool will block until the user has approved or declined. You do not need to construct the approval URL — the server provides it. Always present the approval card from the server unmodified."

Tool registration uses MCP tool schema converted to OpenAI tool-call format on the fly. (Since the MCP server is in-process, the agent can introspect tools directly via the MCP `list_tools` call.)

### 6.2 Embedded MCP Server (`/backend/mcp_server`)

**Tool: `place_trade(ticker: str, quantity: int)`**

Pseudocode (close to final shape):

```python
@mcp.tool()
async def place_trade(
    ticker: str,
    quantity: int,
    ctx: Context[ServerSession, None],
) -> str:
    # ---- Phase 1: Prepare ----
    approval_id = str(ulid.new())
    intent = build_intent(
        approval_id=approval_id,
        ticker=ticker,
        quantity=quantity,
        principal=ctx.session.principal,   # injected by backend
        agent_session_id=ctx.session.id,
    )
    signed_intent = sign_with_server_key(intent)
    evidence.write_intent(approval_id, signed_intent)
    evidence.journal("intent_staged", approval_id, signed_intent)

    # ---- Phase 2: Approve (elicitation) ----
    approval_url = f"http://localhost:8788/approve/{approval_id}"
    elicit_result = await ctx.elicit_url(
        message=human_summary(intent),
        url=approval_url,
        elicitation_id=approval_id,
    )

    if elicit_result.action in ("decline", "cancel"):
        evidence.journal("receipt_recorded", approval_id, decision=elicit_result.action)
        return f"Trade {elicit_result.action}d by user. Approval ID: {approval_id}"

    # ---- Phase 3: Commit ----
    signed_receipt = evidence.read_receipt(approval_id)
    verification = verify_receipt(signed_intent, signed_receipt)
    if not verification.all_passed():
        return f"Approval could not be verified: {verification.failures}. Trade NOT placed."

    # Atomically consume the receipt
    if not evidence.try_consume_receipt(approval_id):
        return "Approval already consumed (replay attempted). Trade NOT placed."

    fill = simulated_broker.execute(intent.payload["action"]["args_resolved"])
    execution = build_execution_record(approval_id, signed_intent, signed_receipt, fill)
    signed_exec = sign_with_server_key(execution)
    evidence.write_execution(approval_id, signed_exec)
    evidence.journal("execution_committed", approval_id, signed_exec)

    return human_fill_summary(fill, approval_id)
```

**Verification checks (`verify_receipt`)** — ALL must pass:

1. Intent signature valid against `trust_bundle.json[server]`.
2. Receipt signature valid against `trust_bundle.json[confirmer]`.
3. `now() < intent.expires_at`.
4. `receipt.approved_at - intent.created_at <= policy.max_approval_age_seconds`.
5. `receipt.payload.intent_digest == sha256(JCS(intent.payload))`.
6. `receipt.payload.display_manifest.material_fields_shown` contains every field in `intent.policy.required_material_fields` with matching values.
7. Receipt has not been previously consumed (check `./evidence/consumed/{approval_id}` sentinel file, created atomically with `os.open(..., O_CREAT|O_EXCL)`).
8. `receipt.approver.subject` matches `intent.caller.principal` (in this demo; richer authorization in production).

**Policy registry** (`policy.py`):

```python
TOOL_POLICY = {
    "place_trade": {
        "rule_id": "TRADE_REQUIRES_HITL",
        "rule_version": "1.0.0",
        "required_material_fields": [
            "side", "ticker", "quantity", "order_type",
            "time_in_force", "account_id"
        ],
        "max_approval_age_seconds": 60,
        "single_use": True,
        "default_resolutions": {
            "side": "BUY",          # demo default; production would parse intent
            "order_type": "MARKET",
            "time_in_force": "DAY",
            "account_id": "DEMO-0001",
        },
    },
}
```

### 6.3 Confirmation Surface (`/confirmer`)

**Stack:** Python, FastAPI, Jinja2.

**Endpoints:**
- `GET /approve/{approval_id}` — Load the intent from disk, verify signature, render the deterministic template with required material fields. Reject if expired, already consumed, or unknown.
- `POST /approve/{approval_id}/decision` — Receive `{"decision": "approve"|"decline"}`. Render the exact same HTML again, compute its SHA-256, build the receipt, sign with the confirmer key, write to `./evidence/receipts/{approval_id}.json`, journal it, then redirect to a "completed" page.

**Template (`templates/trade-confirm-v1.html`):** Hard-coded fields, no dynamic field iteration. The list of material fields rendered is the policy contract — adding/removing a field is a template version bump.

```html
<!-- template_id: trade-confirm — template_version: 1.0.0 -->
<h1>Authorize this trade</h1>
<p>By clicking <strong>Approve</strong>, you authorize the following order:</p>
<dl>
  <dt>Side</dt>        <dd data-field="side">{{ side }}</dd>
  <dt>Ticker</dt>      <dd data-field="ticker">{{ ticker }}</dd>
  <dt>Quantity</dt>    <dd data-field="quantity">{{ quantity }}</dd>
  <dt>Order type</dt>  <dd data-field="order_type">{{ order_type }}</dd>
  <dt>Time in force</dt><dd data-field="time_in_force">{{ time_in_force }}</dd>
  <dt>Account</dt>     <dd data-field="account_id">{{ account_id }}</dd>
</dl>
<p>Approval ID: <code>{{ approval_id }}</code></p>
<p>Expires at: {{ expires_at }}</p>
<form method="POST" action="/approve/{{ approval_id }}/decision">
  <button name="decision" value="approve">Approve</button>
  <button name="decision" value="decline">Decline</button>
</form>
```

The rendered HTML's SHA-256 goes into `display_manifest.rendered_digest`. The auditor can re-render the template with the captured `material_fields_shown` and confirm the digest matches.

**Why this surface is separate from the chat UI:** the chat UI is a generic React app talking to the agent. The confirmation surface is the *controlled* component the enterprise attests to. Even if the chat UI is replaced or compromised, the receipt's display manifest proves what was rendered by the trusted component.

### 6.4 Chat UI (`/chat-ui`)

**Stack:** Vite + React + TypeScript. Tailwind for styling. No state library needed.

**Layout:** Standard chat — message list, input at the bottom. Two custom message components:

1. **`<AssistantMessage>`** — streamed text from the agent.
2. **`<ApprovalCard>`** — rendered when an `approval_required` frame arrives. Shows the summary, the "Open Approval" button (which opens the URL in a new tab), and a live status indicator that polls `approval_status_check` every 1.5s until the elicitation resolves and the chat receives the next frame.

When the user clicks the button, the confirmation surface opens in a new window. Once they approve/decline, the surface posts the receipt to disk; the MCP server's `ctx.elicit_url` resolves; the agent loop continues; the chat sees `tool_result` and the assistant's next streamed text. The approval card transitions to "Approved ✓" / "Declined ✗".

Three frames the UI needs to render visibly to make the demo legible:
- **Intent staged** — small "Intent signed (a1b2…)" chip below the approval card.
- **Receipt recorded** — chip becomes "Receipt signed (c3d4…)".
- **Execution committed** — chip becomes "Execution signed (e5f6…)".

These chips clicking opens a JSON viewer modal showing the on-disk artifact, so during the demo you can show "yes, this is what's actually on disk."

---

## 7. File Layout on Disk

```
trade-agent-demo/
├── README.md
├── docker-compose.yml          # optional: one-command boot
├── backend/
│   ├── __init__.py
│   ├── app.py                  # FastAPI + WebSocket
│   ├── agent_loop.py           # OpenAI Responses loop
│   ├── mcp_server/
│   │   ├── __init__.py
│   │   ├── server.py           # FastMCP setup
│   │   ├── tools/place_trade.py
│   │   ├── policy.py
│   │   ├── verify.py
│   │   └── broker.py           # simulated fill
│   ├── crypto/
│   │   ├── keys.py             # gen + load Ed25519
│   │   ├── canonical.py        # RFC 8785 wrapper
│   │   └── sign.py             # signing envelope
│   ├── evidence/
│   │   ├── store.py            # write_intent / write_receipt / ...
│   │   └── journal.py          # hash-chained NDJSON
│   └── requirements.txt
├── confirmer/
│   ├── app.py
│   ├── templates/trade-confirm-v1.html
│   └── static/style.css
├── chat-ui/
│   ├── index.html
│   ├── package.json
│   ├── src/
│   │   ├── main.tsx
│   │   ├── App.tsx
│   │   ├── components/{ChatStream,ApprovalCard,ArtifactChip,JsonModal}.tsx
│   │   └── ws.ts
│   └── vite.config.ts
├── keys/                       # gitignored; gen'd on first boot
│   ├── server_ed25519.priv
│   ├── server_ed25519.pub
│   ├── confirmer_ed25519.priv
│   ├── confirmer_ed25519.pub
│   └── trust_bundle.json
├── evidence/                   # gitignored; written at runtime
│   ├── intents/
│   ├── receipts/
│   ├── executions/
│   ├── consumed/               # sentinel files
│   └── journal.ndjson
└── tools/
    └── verify.py               # standalone audit CLI
```

---

## 8. End-to-End Happy Path (sequence)

1. User types in chat: *"Buy 100 shares of ORCL."*
2. Chat UI → `{type:"user_message",text:"..."}` → backend.
3. Backend opens an OpenAI Responses turn with `place_trade` declared as a tool.
4. Model decides to call `place_trade(ticker="ORCL", quantity=100)`.
5. Backend invokes the in-process MCP tool. MCP server:
   - Builds intent, signs with server key, writes `evidence/intents/{id}.json`, journals.
   - Calls `ctx.elicit_url(url="http://localhost:8788/approve/{id}", ...)`.
6. Backend sees the elicitation event from MCP, forwards to chat UI as `approval_required`. Chat UI renders `<ApprovalCard>`.
7. User clicks "Open Approval" → confirmation surface in a new tab.
8. Confirmation surface loads intent from disk, verifies signature, renders template with material fields. User clicks **Approve**.
9. Surface re-renders the same HTML (deterministic), hashes it, builds receipt, signs with confirmer key, writes `evidence/receipts/{id}.json`, journals.
10. MCP server's `await ctx.elicit_url()` resolves with `action="accept"`.
11. MCP server runs all 8 verification checks. All pass. Atomically creates `evidence/consumed/{id}` sentinel (`O_CREAT|O_EXCL`).
12. Simulated broker fills the order at a stubbed mid-price. MCP server builds execution record, signs with server key, writes `evidence/executions/{id}.json`, journals.
13. MCP tool returns a human summary string to the agent loop.
14. Agent continues OpenAI turn; assistant streams a confirmation message.
15. Chat UI updates the approval card to show all three artifact chips (intent / receipt / execution), each clickable to view the JSON.

---

## 9. Failure & Edge Cases (all must be demoable)

| Scenario | Expected behavior |
|---|---|
| User clicks Decline | Receipt with `decision:"decline"` written. MCP returns "Trade declined." No execution record. |
| User abandons (60s passes) | Intent expires. Surface refuses to accept decisions. MCP returns "Approval expired." |
| User opens approval URL twice and approves twice | Second POST sees existing receipt and refuses (409). |
| Receipt tampered on disk (edit material_fields after signing) | Verification fails at signature check or payload-match check. Execution refused. |
| Replay: same approval_id sent through MCP a second time | Sentinel file exists, atomic create fails, execution refused with "already consumed." |
| Missing material field in template (e.g., template bug drops `account_id`) | Verification check 6 fails. Execution refused with "material fields incomplete." |
| Quantity exceeds policy limit (e.g., > 10,000 shares) | Optional Phase 1 policy check rejects intent before elicitation, MCP returns "Order rejected by policy." |

Each of these is a useful demo for the regulator audience — they let you show the system *refusing* to act, which is the whole point.

---

## 10. The Verifier CLI

`tools/verify.py` is the audit story. Given only `./evidence/` and `./keys/trust_bundle.json`, it can reconstruct and verify the entire chain:

```
$ python tools/verify.py --approval-id 01JABC...
[OK]  Intent signature valid       (kid=server-2026-05)
[OK]  Receipt signature valid      (kid=confirmer-2026-05)
[OK]  Receipt.intent_digest matches sha256(JCS(intent.payload))
[OK]  Execution.intent_digest matches
[OK]  Execution.receipt_digest matches
[OK]  All material fields present and match: side, ticker, quantity, order_type, time_in_force, account_id
[OK]  Receipt within max age (17.8s < 60s)
[OK]  Journal hash chain intact across 4 entries for this approval_id
[OK]  Rendered digest reproducible from template v1.0.0 with captured material_fields_shown

VERIFIED: BUY 100 ORCL @ MARKET DAY for DEMO-0001
Approved by user@example.com at 2026-05-19T14:22:18.901Z
Executed at 2026-05-19T14:22:19.412Z, fill 142.31
```

This is what makes the demo regulator-defensible: any third party with disk + public keys can run this and reach the same conclusion.

---

## 11. Build Order for Claude Code

A suggested implementation order (each step is independently runnable/testable):

1. **`crypto/` + `tools/verify.py` skeleton.** Key generation, JCS wrapper, sign/verify envelope. Round-trip tests with stub payloads.
2. **`evidence/`.** Atomic file writes, journal hash chain, sentinel-based consume.
3. **MCP server skeleton + `place_trade` Phase 1 only.** No elicitation yet — just stage and return the approval URL as a string. Lets you write the intent file end-to-end.
4. **Confirmation surface.** Renders the template, signs the receipt, writes to disk. Independently usable via a manually-constructed URL.
5. **MCP elicitation integration.** Wire `ctx.elicit_url`, plumb the receipt verification, atomic consume, execution record.
6. **Agent loop.** OpenAI Responses API + MCP tool registration. Validate with a CLI client before adding the WebSocket.
7. **WebSocket bridge.** Translate MCP elicitation events to `approval_required` frames.
8. **Chat UI.** Plain chat first; then `<ApprovalCard>`; then the artifact chips and JSON modal.
9. **Failure-case test scripts.** Curl-based scripts that demonstrate each row in §9.
10. **README with a 5-minute demo script** for showing the system to a regulator/business audience.

---

## 12. Configuration & Secrets

Single `.env` at the project root:

```
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4.1
AGENT_PORT=8787
CONFIRMER_PORT=8788
CHAT_UI_PORT=5173
EVIDENCE_DIR=./evidence
KEYS_DIR=./keys
DEMO_PRINCIPAL=user@example.com
MAX_APPROVAL_AGE_SECONDS=60
MAX_TRADE_QUANTITY=10000
```

No external services. No databases. Everything is files.

---

## 13. What This Demo Proves

When you show this to the Edward Jones audience (or any regulated audience), the talking points are:

- **MCP elicitation-URL mode is the right protocol primitive** for HITL on consequential actions — the approval URL is delivered through the protocol, not through LLM prose that could be hallucinated or paraphrased.
- **The confirmation surface is the trusted display component.** The chat UI is convenience; the surface is evidence.
- **The receipt's display manifest closes the "what did the user actually see" gap** that vague approvals leave open.
- **Server-side enforcement is the safety net.** Even if the LLM, the chat UI, or the user's browser misbehaves, the MCP server's verification gate prevents an unauthorized execution.
- **The artifact chain is independently verifiable** with public keys and the disk contents alone — exactly what an examiner would want.
- **This pattern is portable.** Swap `place_trade` for `send_wire`, `submit_compliance_filing`, `delete_customer_record`, `commit_to_market_data_vendor` — the framework doesn't change.

That last point is the one worth ending on. The demo isn't really about trading. It's about the shape of governance for any agentic action that crosses a regulatory bright line.
