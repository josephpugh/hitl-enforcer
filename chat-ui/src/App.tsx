import React, { useEffect, useMemo, useRef, useState } from 'react';
import { AgentSocket, ServerFrame, ConnectionStatus } from './ws';
import { ApprovalCard, ApprovalCardState } from './components/ApprovalCard';

type ChatItem =
  | { kind: 'user'; id: string; text: string }
  | { kind: 'assistant'; id: string; text: string }
  | { kind: 'tool_call'; id: string; name: string; args: unknown; call_id: string }
  | { kind: 'tool_result'; id: string; name: string; result: string; call_id: string }
  | { kind: 'approval'; id: string; card: ApprovalCardState };

const BACKEND_WS = `ws://${window.location.hostname}:8787/ws`;

function uid(): string {
  return Math.random().toString(36).slice(2, 10);
}

export function App() {
  const [status, setStatus] = useState<ConnectionStatus>('connecting');
  const [items, setItems] = useState<ChatItem[]>([]);
  const [input, setInput] = useState('');
  const [thinking, setThinking] = useState(false);
  const socketRef = useRef<AgentSocket | null>(null);
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const onFrame = (frame: ServerFrame) => {
      setItems((prev) => applyFrame(prev, frame));
      if (frame.type === 'assistant_done') setThinking(false);
    };
    const socket = new AgentSocket(BACKEND_WS, onFrame, setStatus);
    socketRef.current = socket;
    socket.connect();
    return () => socket.close();
  }, []);

  useEffect(() => {
    const content = contentRef.current;
    const scroller = scrollerRef.current;
    if (!content || !scroller) return;
    const ro = new ResizeObserver(() => {
      scroller.scrollTo({ top: scroller.scrollHeight });
    });
    ro.observe(content);
    return () => ro.disconnect();
  }, []);

  const send = () => {
    const text = input.trim();
    if (!text) return;
    setItems((prev) => [...prev, { kind: 'user', id: uid(), text }]);
    socketRef.current?.send({ type: 'user_message', text });
    setInput('');
    setThinking(true);
  };

  return (
    <div className="app">
      <header className="header">
        <h1>Regulated Trade Agent</h1>
        <div className={`status ${status === 'open' ? 'connected' : 'disconnected'}`}>
          {status === 'open' ? '● connected' : status === 'connecting' ? '○ connecting…' : '○ disconnected'}
        </div>
      </header>
      <div className="messages" ref={scrollerRef}>
        <div className="messages-inner" ref={contentRef}>
          {items.length === 0 && (
            <div className="message assistant">
              Hi — I can place stock trades. Try: <em>buy 100 ORCL</em>. Every trade requires your explicit approval.
            </div>
          )}
          {items.map((item) => (
            <Item key={item.id} item={item} />
          ))}
          {thinking && <div className="message assistant" style={{ opacity: 0.5 }}>…</div>}
        </div>
      </div>
      <div className="composer">
        <input
          type="text"
          placeholder="Type a message…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
          disabled={status !== 'open'}
        />
        <button onClick={send} disabled={status !== 'open' || !input.trim()}>
          Send
        </button>
      </div>
    </div>
  );
}

function Item({ item }: { item: ChatItem }) {
  switch (item.kind) {
    case 'user':
      return <div className="message user">{item.text}</div>;
    case 'assistant':
      return <div className="message assistant">{item.text}</div>;
    case 'tool_call':
      return (
        <div className="message tool-call">
          → {item.name}({JSON.stringify(item.args)})
        </div>
      );
    case 'tool_result':
      return (
        <div className="message tool-result">
          ← {item.name}: {item.result}
        </div>
      );
    case 'approval':
      return <ApprovalCard card={item.card} />;
  }
}

function applyFrame(prev: ChatItem[], frame: ServerFrame): ChatItem[] {
  switch (frame.type) {
    case 'assistant_text': {
      // Append to the last assistant message in the most recent turn, else add a new one.
      const lastIdx = lastIndexOf(prev, (x) => x.kind === 'assistant');
      const lastNonApprovalIdx = prev.length - 1;
      if (lastIdx >= 0 && lastIdx === lastNonApprovalIdx) {
        const next = prev.slice();
        const cur = next[lastIdx];
        if (cur.kind === 'assistant') {
          next[lastIdx] = { ...cur, text: cur.text + frame.delta };
        }
        return next;
      }
      return [...prev, { kind: 'assistant', id: uid(), text: frame.delta }];
    }
    case 'tool_call':
      return [
        ...prev,
        { kind: 'tool_call', id: uid(), name: frame.name, args: frame.args, call_id: frame.call_id },
      ];
    case 'approval_required':
      return [
        ...prev,
        {
          kind: 'approval',
          id: frame.approval_id,
          card: {
            approval_id: frame.approval_id,
            url: frame.url,
            summary: frame.summary,
            expires_at: frame.expires_at,
            status: 'pending',
          },
        },
      ];
    case 'tool_result': {
      // Determine card status from the result text — simple substring match.
      const next = prev.map((it) => {
        if (it.kind !== 'approval') return it;
        const r = frame.result.toLowerCase();
        let nextStatus = it.card.status;
        if (r.includes('order filled')) nextStatus = 'approved';
        else if (r.includes('declin')) nextStatus = 'declined';
        else if (r.includes('expire')) nextStatus = 'expired';
        return { ...it, card: { ...it.card, status: nextStatus } };
      });
      return [
        ...next,
        {
          kind: 'tool_result',
          id: uid(),
          name: frame.name,
          result: frame.result,
          call_id: frame.call_id,
        },
      ];
    }
    case 'assistant_done':
      return prev;
    case 'error':
      return [
        ...prev,
        { kind: 'assistant', id: uid(), text: `⚠ ${frame.message}` },
      ];
    default:
      return prev;
  }
}

function lastIndexOf<T>(arr: T[], pred: (x: T) => boolean): number {
  for (let i = arr.length - 1; i >= 0; i--) if (pred(arr[i])) return i;
  return -1;
}
