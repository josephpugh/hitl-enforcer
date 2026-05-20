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
  | {
      type: 'approval_status';
      approval_id: string;
      intent_present: boolean;
      receipt_present: boolean;
      execution_present: boolean;
      decision?: 'approve' | 'decline' | null;
    }
  | { type: 'error'; message: string }
  | { type: 'pong' };

export type ClientFrame =
  | { type: 'user_message'; text: string }
  | { type: 'approval_status_check'; approval_id: string }
  | { type: 'ping' };

export type ConnectionStatus = 'connecting' | 'open' | 'closed';

export class AgentSocket {
  private ws: WebSocket | null = null;
  private url: string;
  private onFrame: (f: ServerFrame) => void;
  private onStatus: (s: ConnectionStatus) => void;
  private reconnectTimer: number | null = null;

  constructor(
    url: string,
    onFrame: (f: ServerFrame) => void,
    onStatus: (s: ConnectionStatus) => void,
  ) {
    this.url = url;
    this.onFrame = onFrame;
    this.onStatus = onStatus;
  }

  connect(): void {
    this.onStatus('connecting');
    const ws = new WebSocket(this.url);
    this.ws = ws;
    ws.onopen = () => this.onStatus('open');
    ws.onclose = () => {
      this.onStatus('closed');
      this.scheduleReconnect();
    };
    ws.onerror = () => {
      ws.close();
    };
    ws.onmessage = (e) => {
      try {
        const frame: ServerFrame = JSON.parse(e.data);
        this.onFrame(frame);
      } catch {
        // ignore malformed
      }
    };
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer !== null) return;
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, 1500);
  }

  send(frame: ClientFrame): boolean {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(frame));
      return true;
    }
    return false;
  }

  close(): void {
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.ws?.close();
    this.ws = null;
  }
}
