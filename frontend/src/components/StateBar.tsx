import { SketchBadge, SketchButton, SketchIcon } from 'blackchalk';
import type { PipelineState } from '../types';

interface StateBarProps {
  state: PipelineState;
  backendKind: string;
  lastSefaMs: number | null;
  memoryOpen: boolean;
  onToggleMemory: () => void;
}

/**
 * Pipeline state bar — carries the terminal UI's information (06 §2) into the
 * bubble UI: current state machine state + last SEFA number (§3.2 P1).
 */
export function StateBar({
  state,
  backendKind,
  lastSefaMs,
  memoryOpen,
  onToggleMemory,
}: StateBarProps) {
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
      <SketchButton
        variant="tertiary"
        aria-label={memoryOpen ? '收起记忆栏' : '展开记忆栏'}
        onClick={onToggleMemory}
      >
        <span className="state-bar-item">
          <SketchIcon name="sidebar" size={13} />
          记忆
        </span>
      </SketchButton>
      <span className="state-bar-item state-backend">
        backend: {backendKind}
      </span>
    </div>
  );
}
