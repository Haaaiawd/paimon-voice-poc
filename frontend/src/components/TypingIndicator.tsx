import { SketchCard } from 'blackchalk';
import avatarUrl from '../assets/paimon-avatar.svg';

/**
 * Hand-drawn "派蒙正在想…" three-dot animation (§3.1 P0).
 * Shown during the MockBackend's 0.8–1.5s THINKING delay.
 */
export function TypingIndicator() {
  return (
    <div className="bubble-row bubble-row-paimon" aria-label="派蒙正在输入">
      <img
        src={avatarUrl}
        alt="派蒙"
        className="paimon-avatar"
        width={36}
        height={36}
      />
      <SketchCard className="bubble bubble-paimon typing-bubble">
        <span className="typing-dot" />
        <span className="typing-dot" />
        <span className="typing-dot" />
      </SketchCard>
    </div>
  );
}
