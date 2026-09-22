import { SketchBadge, SketchIcon, type IconName } from 'blackchalk';
import type { Emotion } from '../types';

/**
 * Emotion badge — FRONTEND_DEMO_DESIGN.md §3.2.
 * Monochrome component library ⇒ icon + text label, never colour.
 */
const EMOTION_ICON: Record<Emotion, IconName> = {
  neutral: 'minus',
  happy: 'star',
  excited: 'zap',
  teasing: 'message-circle',
  annoyed: 'x-circle',
  confused: 'help-circle',
  smug: 'check-circle',
  soft: 'heart',
};

export function EmotionBadge({ emotion }: { emotion: Emotion }) {
  return (
    <span className="emotion-badge" title={`emotion: ${emotion}`}>
      <SketchBadge variant="muted">
        <span className="emotion-badge-inner">
          <SketchIcon name={EMOTION_ICON[emotion]} size={12} />
          {emotion}
        </span>
      </SketchBadge>
    </span>
  );
}
