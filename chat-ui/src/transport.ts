/**
 * Chat transport: per-turn POST /chat returning an SSE stream.
 *
 * The chat session is identified by an X-Session-Id header that comes back
 * from the backend on the first POST; subsequent POSTs echo it so the
 * server can reuse the same MCP client and message history.
 */

export type ServerFrame =
  | { type: 'assistant_text'; delta: string }
  | { type: 'tool_call'; name: string; args: unknown; call_id: string }
  | {
      type: 'approval_required';
      approval_id: string;
      url: string;
      summary: string;
      expires_at: string;
    }
  | { type: 'tool_result'; name: string; result: string; call_id: string }
  | { type: 'assistant_done' }
  | { type: 'error'; message: string };

export type TurnHandle = {
  sessionId: string;
  abort: () => void;
};

const BACKEND_HTTP = `http://${window.location.hostname}:8787`;

export async function postTurn(
  text: string,
  sessionId: string | null,
  onFrame: (f: ServerFrame) => void,
  onError: (message: string) => void,
): Promise<TurnHandle> {
  const controller = new AbortController();
  const handle: TurnHandle = {
    sessionId: sessionId ?? '',
    abort: () => controller.abort(),
  };

  let resp: Response;
  try {
    resp = await fetch(`${BACKEND_HTTP}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify({ text, session_id: sessionId }),
      signal: controller.signal,
    });
  } catch (e) {
    onError(`Network error: ${(e as Error).message}`);
    return handle;
  }

  if (!resp.ok || !resp.body) {
    onError(`HTTP ${resp.status} ${resp.statusText}`);
    return handle;
  }

  const newSessionId = resp.headers.get('X-Session-Id');
  if (newSessionId) handle.sessionId = newSessionId;

  // Consume the SSE stream. We don't use EventSource here because EventSource
  // can't POST; instead we parse SSE frames out of the fetch body stream.
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  (async () => {
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        // Normalize CRLF; the HTML5 SSE spec allows CR, LF, or CRLF.
        buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n').replace(/\r/g, '\n');
        let sep: number;
        while ((sep = buffer.indexOf('\n\n')) >= 0) {
          const raw = buffer.slice(0, sep);
          buffer = buffer.slice(sep + 2);
          const frame = parseSseEvent(raw);
          if (frame) onFrame(frame);
        }
      }
    } catch (e) {
      if ((e as Error).name !== 'AbortError') {
        onError(`Stream error: ${(e as Error).message}`);
      }
    }
  })();

  return handle;
}

function parseSseEvent(block: string): ServerFrame | null {
  let data = '';
  for (const line of block.split('\n')) {
    if (line.startsWith(':')) continue; // comment / keepalive
    if (line.startsWith('data:')) {
      data += line.slice(5).replace(/^ /, '');
    }
  }
  if (!data) return null;
  try {
    return JSON.parse(data) as ServerFrame;
  } catch {
    return null;
  }
}

export async function healthCheck(): Promise<boolean> {
  try {
    const r = await fetch(`${BACKEND_HTTP}/healthz`);
    return r.ok;
  } catch {
    return false;
  }
}
