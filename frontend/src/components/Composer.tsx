import { useState } from 'react';
import { SketchButton, SketchIcon, SketchInput } from 'blackchalk';

interface ComposerProps {
  disabled: boolean;
  /** True while the mic is capturing (PCM uplink window open). */
  recording: boolean;
  /** 0..1 live mic level while recording. */
  micLevel: number;
  onSend: (text: string) => void;
  /** Toggle voice capture: start → user.audio.start + PCM frames; stop → end. */
  onMicToggle: () => void;
}

/**
 * Input box + send button + Enter-to-send + real mic toggle (stage 3, §4.2).
 * Sent text hits the screen immediately — the reply arrives via backend frames.
 */
export function Composer({
  disabled,
  recording,
  micLevel,
  onSend,
  onMicToggle,
}: ComposerProps) {
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
        variant={recording ? 'primary' : 'tertiary'}
        aria-label={recording ? '停止语音输入' : '语音输入'}
        onClick={onMicToggle}
      >
        <SketchIcon name={recording ? 'close' : 'mic'} size={16} aria-label="mic" />
      </SketchButton>
      {recording && (
        <span className="mic-level" aria-label="麦克风电平">
          <span
            className="mic-level-fill"
            style={{ width: `${Math.min(100, Math.round(micLevel * 100))}%` }}
          />
        </span>
      )}
      <div className="composer-input">
        <SketchInput
          value={text}
          placeholder={
            recording ? '正在听……再点一次麦克风结束' : '跟派蒙说点什么……（Enter 发送）'
          }
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
