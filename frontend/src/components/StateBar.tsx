import { SketchBadge, SketchIcon } from 'blackchalk';
import type { PipelineState } from '../types';

interface StateBarProps {
  state: PipelineState;
  backendKind: string;
  lastSefaMs: number | null;
}

/**
 * Pipeline state bar — carries the terminal UI's information (06 §2) into the
 * bubble UI: current state machine state + last SEFA number (§3.2 P1).
 */
export function StateBar({ state, backendKind, lastSefaMs }: StateBarProps) {
  return (
    <div className="state-bar">
      <span className="state-bar-item">
        <SketchIcon name="activity" size={13} />
        <SketchBadge>{state}</SketchBadge>
      </span>
      {lastSefaMs !== null && (
        <span className="state-bar-item state-sefa">
          <SketchIcon name="timer" size={13} />
          <SketchBadge variant="muted">SEFA {lastSefaMs}ms</SketchBadge>
        </span>
      )}
      <span className="state-bar-spacer" />
      <span className="state-bar-item state-backend">
        backend: {backendKind}
      </span>
    </div>
  );
}
