import { useEffect, useRef } from 'react';
import type { NowPlaying } from '../audio/ReplyPlayer';

/**
 * Waveform — monochrome hand-drawn bars from the reply's PCM peak envelope,
 * with a playhead driven by the HTMLAudioElement's currentTime (rAF).
 */
export function Waveform({ playing }: { playing: NowPlaying }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext('2d');
    if (!canvas || !ctx) return;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    ctx.scale(dpr, dpr);

    let raf = 0;
    const draw = () => {
      const fg = getComputedStyle(canvas).color || '#2b2b2b';
      const n = playing.envelope.length;
      const bw = w / n;
      const progress = playing.audio.duration
        ? playing.audio.currentTime / playing.audio.duration
        : 0;
      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = fg;
      for (let i = 0; i < n; i++) {
        const barH = Math.max(2, playing.envelope[i] * (h - 6));
        const wobble = (((i * 37) % 7) - 3) * 0.4; // slight hand-drawn jitter
        const x = i * bw + bw * 0.25;
        const y = (h - barH) / 2 + wobble;
        ctx.globalAlpha = (i + 0.5) / n <= progress ? 1 : 0.32;
        ctx.beginPath();
        if (typeof ctx.roundRect === 'function') {
          ctx.roundRect(x, y, bw * 0.5, barH, 2);
        } else {
          ctx.rect(x, y, bw * 0.5, barH);
        }
        ctx.fill();
      }
      ctx.globalAlpha = 1;
      raf = requestAnimationFrame(draw);
    };
    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
  }, [playing]);

  return <canvas ref={canvasRef} className="waveform" aria-label="语音波形" />;
}
