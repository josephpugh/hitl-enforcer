import React from 'react';

type Props = {
  title: string;
  data: unknown;
  onClose: () => void;
};

export function JsonModal({ title, data, onClose }: Props) {
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <header>
          <h3>{title}</h3>
          <button className="close" onClick={onClose}>
            Close
          </button>
        </header>
        <pre>{JSON.stringify(data, null, 2)}</pre>
      </div>
    </div>
  );
}
