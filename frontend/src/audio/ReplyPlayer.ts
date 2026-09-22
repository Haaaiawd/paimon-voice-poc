import { pcmEnvelope, pcmFormatToRate, pcmToWavBlob } from './pcm';

/**
 * ReplyPlayer — stage 3 downlink playback (FRONTEND_DEMO_DESIGN.md §4.3).
 *
 * audio.chunk JSON headers carry {seq, format}; their payloads arrive as
 * binary frames. Chunks accumulate per turn; endTurn() assembles them into a
 * WAV Blob and plays through an HTMLAudioElement (boundary: browser
 * MediaSource/Audio only, no native wrapping). stopAll() is the local half of
 * barge-in: the moment the user starts talking (or an `interrupted` frame
 * lands), current playback halts and the pending buffer is dropped.
 */

export interface NowPlaying {
  /** 0..1 peak envelope, one bar per bucket — waveform source data. */
  envelope: number[];
  /** Seconds. */
  duration: number;
  /** Live element; the waveform reads currentTime for the playhead. */
  audio: HTMLAudioElement;
}

type Listener = (now: NowPlaying | null) => void;

export class ReplyPlayer {
  private chunks: Int16Array[] = [];
  private rate = 24000;
  private current: { audio: HTMLAudioElement; url: string } | null = null;
  private listeners = new Set<Listener>();

  /** One binary payload + its audio.chunk header meta. */
  push(payload: ArrayBuffer, format: string): void {
    if (this.chunks.length === 0) this.rate = pcmFormatToRate(format);
    this.chunks.push(new Int16Array(payload));
  }

  get buffered(): boolean {
    return this.chunks.length > 0;
  }

  /** Turn boundary reached: assemble buffered PCM and start playback. */
  endTurn(): void {
    if (this.chunks.length === 0) return;
    const samples = concat(this.chunks);
    const rate = this.rate;
    this.chunks = [];
    const blob = pcmToWavBlob([samples], rate);
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    const now: NowPlaying = {
      envelope: pcmEnvelope(samples),
      duration: samples.length / rate,
      audio,
    };
    this.halt();
    this.current = { audio, url };
    audio.onended = () => this.finish(audio);
    audio.onerror = () => this.finish(audio);
    void audio.play().catch(() => this.finish(audio));
    this.emit(now);
  }

  /** Barge-in: stop playback immediately and drop the pending buffer. */
  stopAll(): void {
    this.chunks = [];
    this.halt();
    this.emit(null);
  }

  onState(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private halt(): void {
    if (!this.current) return;
    this.current.audio.onended = null;
    this.current.audio.onerror = null;
    this.current.audio.pause();
    URL.revokeObjectURL(this.current.url);
    this.current = null;
  }

  private finish(audio: HTMLAudioElement): void {
    if (this.current?.audio !== audio) return;
    URL.revokeObjectURL(this.current.url);
    this.current = null;
    this.emit(null);
  }

  private emit(now: NowPlaying | null): void {
    for (const l of this.listeners) l(now);
  }
}

function concat(parts: Int16Array[]): Int16Array {
  const total = parts.reduce((n, p) => n + p.length, 0);
  const out = new Int16Array(total);
  let off = 0;
  for (const p of parts) {
    out.set(p, off);
    off += p.length;
  }
  return out;
}
