import { SketchButton, SketchIcon } from 'blackchalk';
import type { NowPlaying } from '../audio/ReplyPlayer';
import { Waveform } from './Waveform';

interface NowPlayingBarProps {
  playing: NowPlaying | null;
  onStop: () => void;
}

/** Thin strip under the StateBar: waveform + stop while a reply plays. */
export function NowPlayingBar({ playing, onStop }: NowPlayingBarProps) {
  if (!playing) return null;
  return (
    <div className="now-playing">
      <SketchIcon name="volume" size={14} />
      <span className="now-playing-label">派蒙语音</span>
      <Waveform playing={playing} />
      <SketchButton variant="tertiary" aria-label="停止播放" onClick={onStop}>
        <SketchIcon name="close" size={13} />
      </SketchButton>
    </div>
  );
}
