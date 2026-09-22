import { SketchCard } from 'blackchalk';
import type { ChatMessage } from '../types';
import { EmotionBadge } from './EmotionBadge';
import avatarUrl from '../assets/paimon-avatar.svg';

/**
 * Chat bubble — user right-aligned, Paimon left-aligned with avatar slot and
 * emotion badge slot (FRONTEND_DEMO_DESIGN.md §3.1 P0).
 */
export function ChatBubble({ message }: { message: ChatMessage }) {
  if (message.role === 'notice') {
    return <div className="chat-notice">( {message.text} )</div>;
  }
  if (message.role === 'user') {
    return (
      <div className="bubble-row bubble-row-user">
        <SketchCard inverted className="bubble bubble-user">
          {message.text}
        </SketchCard>
      </div>
    );
  }
  return (
    <div className="bubble-row bubble-row-paimon">
      <img
        src={avatarUrl}
        alt="派蒙"
        className="paimon-avatar"
        width={36}
        height={36}
      />
      <div className="bubble-stack">
        {message.text && (
          <SketchCard className="bubble bubble-paimon">
            {message.text}
            {message.streaming && !message.followup && (
              <span className="streaming-caret">▏</span>
            )}
          </SketchCard>
        )}
        {message.followup && (
          <SketchCard className="bubble bubble-paimon bubble-followup">
            {message.followup}
            {message.streaming && (
              <span className="streaming-caret">▏</span>
            )}
          </SketchCard>
        )}
        {message.reply && !message.streaming && (
          <EmotionBadge emotion={message.reply.emotion} />
        )}
      </div>
    </div>
  );
}
