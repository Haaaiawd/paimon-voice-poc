import { useEffect, useRef } from 'react';
import type { ChatMessage } from '../types';
import { ChatBubble } from './ChatBubble';
import { TypingIndicator } from './TypingIndicator';

interface ChatListProps {
  messages: ChatMessage[];
  typing: boolean;
}

/** Bubble stream + auto-scroll to bottom (§1.3). */
export function ChatList({ messages, typing }: ChatListProps) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, typing]);

  return (
    <div className="chat-list">
      {messages.map((m) => (
        <ChatBubble key={m.id} message={m} />
      ))}
      {typing && <TypingIndicator />}
      <div ref={bottomRef} />
    </div>
  );
}
