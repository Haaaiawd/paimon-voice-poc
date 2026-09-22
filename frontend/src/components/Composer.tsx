import { useState } from 'react';
import { SketchButton, SketchIcon, SketchInput } from 'blackchalk';

interface ComposerProps {
  disabled: boolean;
  onSend: (text: string) => void;
  onMic: () => void;
}

/**
 * Input box + send button + Enter-to-send + mic button (mock, §3.2 P1).
 * Sent text hits the screen immediately — the reply arrives via backend frames.
 */
export function Composer({ disabled, onSend, onMic }: ComposerProps) {
  const [text, setText] = useState('');

  const submit = () => {
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setText('');
  };

  return (
    <div className="composer">
      <SketchButton
        variant="tertiary"
        aria-label="语音输入（demo 未接通）"
        onClick={onMic}
      >
        <SketchIcon name="mic" size={16} aria-label="mic" />
      </SketchButton>
      <div className="composer-input">
        <SketchInput
          value={text}
          placeholder="跟派蒙说点什么……（Enter 发送）"
          disabled={disabled}
          onChange={setText}
          inputProps={{
            onKeyDown: (e) => {
              if (e.key === 'Enter' && !e.nativeEvent.isComposing) submit();
            },
          }}
        />
      </div>
      <SketchButton
        variant="primary"
        aria-label="发送"
        disabled={disabled || !text.trim()}
        onClick={submit}
      >
        <SketchIcon name="send" size={16} aria-label="send" />
      </SketchButton>
    </div>
  );
}
