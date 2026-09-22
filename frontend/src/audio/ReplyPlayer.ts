import { pcmEnvelope, pcmFormatToRate } from './pcm';

/**
 * ReplyPlayer — stage 3 downlink playback (FRONTEND_DEMO_DESIGN.md §4.3).
 *
 * True streaming: every audio.chunk binary payload becomes a mono AudioBuffer
 * scheduled gaplessly on a shared AudioContext — the first chunk starts the
 * turn, later chunks chain at nextStartTime (the browser resamples to the
 * device rate). stopAll() is the local half of barge-in: all scheduled
 * sources halt at once and the turn bookkeeping is dropped. The AudioContext
 * is kept for reuse; unlock() from a user gesture satisfies autoplay policy.
 */

export interface NowPlaying {
  /** 0..1 peak envelope — grows as chunks arrive; waveform source data. */
  readonly envelope: number[];
  /** Scheduled turn length in seconds (grows while chunks arrive). */
  readonly duration: number;
  /** Playhead in seconds, clamped to 0..duration. */
  readonly currentTime: number;
}

type Listener = (now: NowPlaying | null) => void;

/** Envelope bars appended per chunk / kept in total — rAF reads, no React churn. */
const ENVELOPE_BUCKETS_PER_CHUNK = 8;
const ENVELOPE_MAX_BARS = 96;

export class ReplyPlayer {
  private ctx: AudioContext | null = null;
  private sources = new Set<AudioBufferSourceNode>();
  private listeners = new Set<Listener>();
  private readonly envelope: number[] = [];
  /** ctx.currentTime when the current turn's first chunk starts. */
  private turnStart = 0;
  /** Schedule cursor: end time of the last scheduled chunk. */
  private nextStart = 0;
  /** A turn is being scheduled/played. */
  private active = false;
  /** endTurn() seen — no more chunks will arrive for this turn. */
  private ended = false;
  /** NowPlaying already emitted for the current turn. */
  private emitted = false;

  /** User-gesture entry point: create/resume the shared AudioContext. */
  async unlock(): Promise<void> {
    const ctx = this.ensureCtx();
    if (ctx.state === 'suspended') await ctx.resume().catch(() => {});
  }

  /** One binary payload + its audio.chunk header meta — plays immediately. */
  push(payload: ArrayBuffer, format: string): void {
    const samples = new Int16Array(payload);
    if (samples.length === 0) return;
    const ctx = this.ensureCtx();
    // Autoplay policy may still hold the context suspended; best effort.
    if (ctx.state === 'suspended') void ctx.resume().catch(() => {});

    const floats = new Float32Array(samples.length);
    for (let i = 0; i < samples.length; i++) floats[i] = samples[i] / 0x8000;
    const buf = ctx.createBuffer(
      1,
      samples.length,
      pcmFormatToRate(format),
    );
    buf.copyToChannel(floats, 0);

    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);
    const start = Math.max(ctx.currentTime + 0.02, this.nextStart);
    src.start(start);
    if (!this.active) {
      // First chunk of a new turn anchors the schedule.
      this.active = true;
      this.ended = false;
      this.turnStart = start;
    }
    this.nextStart = start + buf.duration;
    src.onended = () => {
      this.sources.delete(src);
      this.checkFinish();
    };
    this.sources.add(src);

    this.envelope.push(...pcmEnvelope(samples, ENVELOPE_BUCKETS_PER_CHUNK));
    if (this.envelope.length > ENVELOPE_MAX_BARS) {
      this.envelope.splice(0, this.envelope.length - ENVELOPE_MAX_BARS);
    }
    if (!this.emitted) {
      this.emitted = true;
      this.emit(this.snapshot());
    }
  }

  /** Active or queued playback exists (scheduled sources not yet ended). */
  get buffered(): boolean {
    return this.sources.size > 0;
  }

  /** Turn boundary: no more chunks. Playback already started; emit null once
   *  every scheduled source has ended. */
  endTurn(): void {
    this.ended = true;
    this.checkFinish();
  }

  /** Barge-in: stop everything scheduled now and drop the turn. */
  stopAll(): void {
    for (const src of this.sources) {
      try {
        src.stop();
      } catch {
        // already ended
      }
    }
    this.sources.clear();
    this.active = false;
    this.ended = false;
    this.emitted = false;
    this.nextStart = 0;
    this.envelope.length = 0;
    this.emit(null);
  }

  onState(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private ensureCtx(): AudioContext {
    if (!this.ctx) {
      this.ctx = new AudioContext({ latencyHint: 'interactive' });
    }
    return this.ctx;
  }

  /** Live view: duration/currentTime read straight off the ctx schedule. */
  private snapshot(): NowPlaying {
    const player = this;
    return {
      envelope: player.envelope,
      get duration() {
        return Math.max(0, player.nextStart - player.turnStart);
      },
      get currentTime() {
        const ctx = player.ctx;
        if (!ctx) return 0;
        const t = ctx.currentTime - player.turnStart;
        return Math.min(Math.max(0, t), this.duration);
      },
    };
  }

  private checkFinish(): void {
    if (!this.active || !this.ended || this.sources.size > 0) return;
    this.active = false;
    this.ended = false;
    this.emitted = false;
    this.nextStart = 0;
    this.envelope.length = 0;
    this.emit(null);
  }

  private emit(now: NowPlaying | null): void {
    for (const l of this.listeners) l(now);
  }
}
