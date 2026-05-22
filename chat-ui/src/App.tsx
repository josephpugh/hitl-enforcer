import React, { useEffect, useRef, useState } from 'react';
import { ServerFrame, TurnHandle, healthCheck, postTurn } from './transport';
import { ApprovalCard, ApprovalCardState } from './components/ApprovalCard';

type ChatItem =
  | { kind: 'user'; id: string; text: string }
  | { kind: 'assistant'; id: string; text: string }
  | { kind: 'tool_call'; id: string; name: string; args: unknown; call_id: string }
  | { kind: 'tool_result'; id: string; name: string; result: string; call_id: string }
  | { kind: 'approval'; id: string; card: ApprovalCardState };

type ConnState = 'checking' | 'connected' | 'disconnected';

function uid(): string {
  return Math.random().toString(36).slice(2, 10);
}

export function App() {
  const [conn, setConn] = useState<ConnState>('checking');
  const [items, setItems] = useState<ChatItem[]>([]);
  const [input, setInput] = useState('');
  const [thinking, setThinking] = useState(false);
  const sessionIdRef = useRef<string | null>(null);
  const turnRef = useRef<TurnHandle | null>(null);
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);

  // Periodic backend health probe so the status pip is meaningful even when
  // no chat turn is in flight.
  useEffect(() => {
    let cancelled = false;
    const probe = async () => {
      const ok = await healthCheck();
      if (!cancelled) setConn(ok ? 'connected' : 'disconnected');
    };
    probe();
    const intervalId = window.setInterval(probe, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
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

  // Abort any in-flight turn on unmount.
  useEffect(() => () => turnRef.current?.abort(), []);

  const send = async () => {
    const text = input.trim();
    if (!text || thinking) return;
    setItems((prev) => [...prev, { kind: 'user', id: uid(), text }]);
    setInput('');
    setThinking(true);

    const handle = await postTurn(
      text,
      sessionIdRef.current,
      (frame) => setItems((prev) => applyFrame(prev, frame)),
      (msg) => {
        setItems((prev) => [...prev, { kind: 'assistant', id: uid(), text: `⚠ ${msg}` }]);
        setThinking(false);
      },
    );
    sessionIdRef.current = handle.sessionId || sessionIdRef.current;
    turnRef.current = handle;
  };

  return (
    <div className="app">
      <header className="header">
        <h1>Regulated Trade Agent</h1>
        <div className={`status ${conn === 'connected' ? 'connected' : 'disconnected'}`}>
          {conn === 'connected'
            ? '● connected'
            : conn === 'checking'
            ? '○ checking…'
            : '○ disconnected'}
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
          disabled={conn !== 'connected' || thinking}
        />
        <button
          onClick={send}
          disabled={conn !== 'connected' || thinking || !input.trim()}
        >
          Send
        </button>
      </div>
    </div>
  );

  function setThinkingFromFrame(frame: ServerFrame) {
    if (frame.type === 'assistant_done') setThinking(false);
  }

  function applyFrame(prev: ChatItem[], frame: ServerFrame): ChatItem[] {
    setThinkingFromFrame(frame);
    switch (frame.type) {
      case 'assistant_text': {
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
          {
            kind: 'tool_call',
            id: uid(),
            name: frame.name,
            args: frame.args,
            call_id: frame.call_id,
          },
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
        return [...prev, { kind: 'assistant', id: uid(), text: `⚠ ${frame.message}` }];
      default:
        return prev;
    }
  }
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

function lastIndexOf<T>(arr: T[], pred: (x: T) => boolean): number {
  for (let i = arr.length - 1; i >= 0; i--) if (pred(arr[i])) return i;
  return -1;
}
