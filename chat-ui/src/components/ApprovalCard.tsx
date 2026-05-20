import React, { useEffect, useMemo, useState } from 'react';
import { ArtifactChip } from './ArtifactChip';
import { JsonModal } from './JsonModal';

export type ApprovalCardState = {
  approval_id: string;
  url: string;
  summary: string;
  expires_at: string;
  status: 'pending' | 'approved' | 'declined' | 'expired';
};

type ArtifactSnapshot = {
  intent: unknown;
  intent_sha256: string | null;
  receipt: unknown;
  receipt_sha256: string | null;
  execution: unknown;
  execution_sha256: string | null;
};

const BACKEND_HTTP = `http://${window.location.hostname}:8787`;

// We pin the trusted iframe origin so the postMessage listener can verify it.
// Production would derive this from a config/env value injected at build time.
const CONFIRMER_ORIGIN = (() => {
  try {
    return new URL(`http://${window.location.hostname}:8788`).origin;
  } catch {
    return 'http://localhost:8788';
  }
})();

type Props = {
  card: ApprovalCardState;
};

export function ApprovalCard({ card }: Props) {
  const [artifacts, setArtifacts] = useState<ArtifactSnapshot | null>(null);
  const [modal, setModal] = useState<{ title: string; data: unknown } | null>(null);
  // Optimistic flip when the iframe postMessages a decision back. The
  // canonical truth is the on-disk receipt (which the chip polling will
  // confirm shortly); this is only for snappy UI feedback.
  const [decided, setDecided] = useState<'approve' | 'decline' | null>(null);
  // Iframe self-reports its content height so we can size it to fit.
  const [iframeHeight, setIframeHeight] = useState<number>(220);

  // Memoize the iframe URL so React doesn't reload the iframe on each render.
  const iframeUrl = useMemo(() => card.url, [card.url]);

  useEffect(() => {
    let cancelled = false;
    let intervalId: number | null = null;

    const poll = async () => {
      try {
        const res = await fetch(`${BACKEND_HTTP}/artifacts/${card.approval_id}`);
        if (!res.ok) return;
        const data: ArtifactSnapshot = await res.json();
        if (!cancelled) setArtifacts(data);
        if (data.execution || card.status === 'declined' || card.status === 'expired') {
          if (intervalId !== null) {
            window.clearInterval(intervalId);
            intervalId = null;
          }
        }
      } catch {
        // ignore
      }
    };

    poll();
    intervalId = window.setInterval(poll, 1500);
    return () => {
      cancelled = true;
      if (intervalId !== null) window.clearInterval(intervalId);
    };
  }, [card.approval_id, card.status]);

  // Listen for the confirmer's postMessages: decision completion + size updates.
  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      // SECURITY: trust nothing without verifying the origin.
      if (e.origin !== CONFIRMER_ORIGIN) return;
      const data = e.data as {
        type?: string;
        approval_id?: string;
        decision?: string;
        height?: number;
      };
      if (data?.type === 'hitl_decision') {
        if (data.approval_id !== card.approval_id) return;
        if (data.decision === 'approve' || data.decision === 'decline') {
          setDecided(data.decision);
        }
        return;
      }
      if (data?.type === 'hitl_resize') {
        // Defensive clamp: ignore implausible values, including reports from
        // unrelated iframes (the resize bridge ships on all confirmer pages).
        if (typeof data.height !== 'number') return;
        const clamped = Math.min(2000, Math.max(80, Math.round(data.height)));
        setIframeHeight(clamped);
        return;
      }
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, [card.approval_id]);

  const effectiveStatus =
    card.status !== 'pending'
      ? card.status
      : decided === 'approve'
      ? 'approved'
      : decided === 'decline'
      ? 'declined'
      : 'pending';

  const statusLine = (() => {
    if (effectiveStatus === 'approved') return 'Approved ✓';
    if (effectiveStatus === 'declined') return 'Declined ✗';
    if (effectiveStatus === 'expired') return 'Expired ⌛';
    return 'Awaiting your approval…';
  })();

  return (
    <div className="approval-card">
      <h3>Approval required</h3>
      <p className="summary">{card.summary}</p>
      {effectiveStatus === 'pending' && (
        <div className="confirm-frame">
          <iframe
            src={iframeUrl}
            title={`Approval ${card.approval_id}`}
            sandbox="allow-forms allow-scripts allow-same-origin"
            style={{ height: `${iframeHeight}px` }}
          />
          <p className="fallback">
            Trouble seeing the form?{' '}
            <a href={card.url} target="_blank" rel="noreferrer">
              Open in a new tab
            </a>
          </p>
        </div>
      )}
      <p className="status-line">
        {statusLine}{' '}
        <span style={{ opacity: 0.6 }}>
          (id <code>{card.approval_id}</code>)
        </span>
      </p>
      <div className="chips">
        <ArtifactChip
          label="Intent signed"
          sha={artifacts?.intent_sha256}
          present={!!artifacts?.intent}
          onClick={() =>
            artifacts?.intent && setModal({ title: 'Intent', data: artifacts.intent })
          }
        />
        <ArtifactChip
          label="Receipt signed"
          sha={artifacts?.receipt_sha256}
          present={!!artifacts?.receipt}
          onClick={() =>
            artifacts?.receipt && setModal({ title: 'Receipt', data: artifacts.receipt })
          }
        />
        <ArtifactChip
          label="Execution signed"
          sha={artifacts?.execution_sha256}
          present={!!artifacts?.execution}
          onClick={() =>
            artifacts?.execution &&
            setModal({ title: 'Execution', data: artifacts.execution })
          }
        />
      </div>
      {modal && <JsonModal title={modal.title} data={modal.data} onClose={() => setModal(null)} />}
    </div>
  );
}
