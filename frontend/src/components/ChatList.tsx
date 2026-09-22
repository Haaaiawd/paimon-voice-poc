import { useEffect, useRef } from 'react';
import type { ChatMessage, Notice } from '../types';
import { ChatBubble } from './ChatBubble';
import { TypingIndicator } from './TypingIndicator';

interface ChatListProps {
  messages: ChatMessage[];
  typing: boolean;
  notices: Notice[];
}

/** Bubble stream + auto-scroll to bottom (§1.3). */
export function ChatList({ messages, typing, notices }: ChatListProps) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, typing, notices]);

  return (
    <div className="chat-list">
      {messages.map((m) => (
        <ChatBubble key={m.id} message={m} />
      ))}
      {typing && <TypingIndicator />}
      {notices.map((n) => (
        <div key={n.id} className="chat-notice">
          ( {n.text} )
        </div>
      ))}
      <div ref={bottomRef} />
    </div>
  );
}
