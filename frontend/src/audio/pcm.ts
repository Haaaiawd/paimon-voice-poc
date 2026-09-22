/**
 * PCM helpers — FRONTEND_DEMO_DESIGN.md §4.2: uplink is raw PCM
 * 16kHz/16bit/mono binary frames (aligned with backend MicCapture);
 * downlink payloads are declared by audio.chunk frames' `format`
 * (e.g. "pcm24k" = 24kHz s16le mono).
 */

/** Contract uplink rate: PCM 16kHz/16bit/mono (§4.2). */
export const UPLINK_RATE = 16000;

/** "pcm24k" → 24000, "pcm44.1k" → 44100; unknown formats fall back to 24k. */
export function pcmFormatToRate(format: string): number {
  const m = /^pcm(\d+(?:\.\d+)?)k$/i.exec(format.trim());
  return m ? Number(m[1]) * 1000 : 24000;
}

/** Float32 samples (any rate) → s16le PCM at UPLINK_RATE (linear resample). */
export function resampleToS16(input: Float32Array, srcRate: number): Int16Array {
  if (srcRate === UPLINK_RATE) return floatToS16(input);
  const ratio = srcRate / UPLINK_RATE;
  const outLen = Math.floor(input.length / ratio);
  const out = new Float32Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const pos = i * ratio;
    const i0 = Math.floor(pos);
    const i1 = Math.min(i0 + 1, input.length - 1);
    out[i] = input[i0] + (input[i1] - input[i0]) * (pos - i0);
  }
  return floatToS16(out);
}

function floatToS16(input: Float32Array): Int16Array {
  const out = new Int16Array(input.length);
  for (let i = 0; i < input.length; i++) {
    const s = Math.max(-1, Math.min(1, input[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

/** Concatenate s16le chunks → WAV Blob for HTMLAudioElement playback. */
export function pcmToWavBlob(chunks: Int16Array[], sampleRate: number): Blob {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const buf = new ArrayBuffer(44 + total * 2);
  const v = new DataView(buf);
  const writeStr = (off: number, s: string) => {
    for (let i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i));
  };
  writeStr(0, 'RIFF');
  v.setUint32(4, 36 + total * 2, true);
  writeStr(8, 'WAVE');
  writeStr(12, 'fmt ');
  v.setUint32(16, 16, true);
  v.setUint16(20, 1, true); // PCM
  v.setUint16(22, 1, true); // mono
  v.setUint32(24, sampleRate, true);
  v.setUint32(28, sampleRate * 2, true);
  v.setUint16(32, 2, true);
  v.setUint16(34, 16, true);
  writeStr(36, 'data');
  v.setUint32(40, total * 2, true);
  let off = 44;
  for (const c of chunks) {
    new Int16Array(buf, off, c.length).set(c);
    off += c.length * 2;
  }
  return new Blob([buf], { type: 'audio/wav' });
}

/** Peak envelope of s16le samples → `buckets` values in 0..1 (waveform bars). */
export function pcmEnvelope(samples: Int16Array, buckets = 48): number[] {
  const out = new Array<number>(buckets).fill(0);
  if (samples.length === 0) return out;
  const step = samples.length / buckets;
  for (let b = 0; b < buckets; b++) {
    const from = Math.floor(b * step);
    const to = Math.max(from + 1, Math.floor((b + 1) * step));
    let peak = 0;
    for (let i = from; i < to && i < samples.length; i++) {
      peak = Math.max(peak, Math.abs(samples[i]));
    }
    out[b] = peak / 0x8000;
  }
  return out;
}
