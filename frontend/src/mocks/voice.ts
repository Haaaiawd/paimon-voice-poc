/**
 * Mock voice — stage 3. Synthesizes a bright warbling tone shaped by the
 * reply text so the playback path (audio.chunk → AudioBufferSourceNode →
 * waveform) is exercisable without the real TTS. Mirrors the backend's
 * ToneTTS(sample_rate=24000, secs_per_chunk=0.15) demo shape.
 */

export const MOCK_TTS_RATE = 24000;
export const MOCK_TTS_FORMAT = 'pcm24k';
/** Same 0.15s chunking as the backend ToneTTS demo. */
export const MOCK_CHUNK_SAMPLES = (MOCK_TTS_RATE * 150) / 1000;

/** s16le mono tone; duration scales with speech length (~0.16s/字, 0.7–4.5s). */
export function synthSpeechPcm(speech: string): Int16Array {
  const secs = Math.min(4.5, Math.max(0.7, speech.length * 0.16));
  const n = Math.floor(MOCK_TTS_RATE * secs);
  const out = new Int16Array(n);
  let phase = 0;
  for (let i = 0; i < n; i++) {
    const t = i / MOCK_TTS_RATE;
    // Per-syllable chirps: ~5.5 syllables/sec, gated on/off with soft edges.
    const syl = t * 5.5;
    const gate = smooth(Math.sin(syl * Math.PI * 2) * 0.5 + 0.5);
    // Wandering bright pitch (~520–900Hz) with vibrato — reads as chatter.
    const step = Math.floor(syl) % speech.length;
    const base = 520 + ((speech.charCodeAt(step) || 0) % 380);
    const freq = base + 60 * Math.sin(t * 9);
    phase += (2 * Math.PI * freq) / MOCK_TTS_RATE;
    const env = Math.min(1, i / 800, (n - i) / 2400); // attack/release
    out[i] = Math.round(Math.sin(phase) * gate * env * 0.42 * 0x7fff);
  }
  return out;
}

function smooth(x: number): number {
  const c = Math.max(0, Math.min(1, x));
  return c * c * (3 - 2 * c);
}

/** Split one PCM buffer into chunkSize-sample pieces (last may be short). */
export function chunkPcm(pcm: Int16Array, chunkSize = MOCK_CHUNK_SAMPLES): Int16Array[] {
  const out: Int16Array[] = [];
  for (let i = 0; i < pcm.length; i += chunkSize) {
    out.push(pcm.slice(i, Math.min(i + chunkSize, pcm.length)));
  }
  return out;
}
