import React from 'react';

type Props = {
  label: string;
  sha?: string | null;
  present: boolean;
  onClick?: () => void;
};

export function ArtifactChip({ label, sha, present, onClick }: Props) {
  const klass = present ? 'chip' : 'chip pending';
  const text = present && sha ? `${label} (${sha.slice(7, 13)}…)` : `${label} (pending)`;
  return (
    <button
      type="button"
      className={klass}
      onClick={present ? onClick : undefined}
      disabled={!present}
      title={present && sha ? sha : 'Waiting…'}
    >
      {text}
    </button>
  );
}
