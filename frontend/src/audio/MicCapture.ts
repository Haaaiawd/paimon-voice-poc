import { resampleToS16, UPLINK_RATE } from './pcm';

/**
 * MicCapture — stage 3 mic uplink (FRONTEND_DEMO_DESIGN.md §4.2).
 *
 * getUserMedia → AudioContext → AudioWorklet (inline via Blob URL, no extra
 * asset) → Float32 frames resampled to PCM 16kHz/16bit/mono → emitted as
 * ~80ms Int16Array chunks (≤100ms/frame, low-latency-pipeline C4).
 * Callers ship chunk.buffer as a WS binary frame.
 */

/** Uplink chunk size: 1280 samples @16kHz = 80ms per binary frame. */
const CHUNK_SAMPLES = (UPLINK_RATE * 80) / 1000;

// Runs inside the audio rendering thread; forwards each input block.
const WORKLET_SOURCE = `
class PcmCapture extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch && ch.length) this.port.postMessage(ch.slice(0));
    return true;
  }
}
registerProcessor('pcm-capture', PcmCapture);
`;

export class MicCapture {
  private ctx: AudioContext | null = null;
  private stream: MediaStream | null = null;
  private node: AudioWorkletNode | null = null;
  private srcRate = 48000;
  private carry: Int16Array = new Int16Array(0);

  /** True after start(); resolves false-y errors via onError instead of throw. */
  get active(): boolean {
    return this.ctx !== null;
  }

  async start(
    onChunk: (pcm: Int16Array) => void,
    onLevel?: (level: number) => void,
  ): Promise<void> {
    try {
      await this.startInner(onChunk, onLevel);
    } catch (e) {
      await this.stop(); // don't leak a live mic if the worklet path failed
      throw e;
    }
  }

  private async startInner(
    onChunk: (pcm: Int16Array) => void,
    onLevel?: (level: number) => void,
  ): Promise<void> {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
    this.ctx = new AudioContext();
    const url = URL.createObjectURL(
      new Blob([WORKLET_SOURCE], { type: 'application/javascript' }),
    );
    try {
      await this.ctx.audioWorklet.addModule(url);
    } finally {
      URL.revokeObjectURL(url);
    }
    const src = this.ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.ctx, 'pcm-capture');
    this.srcRate = this.ctx.sampleRate;
    this.node.port.onmessage = (ev: MessageEvent<Float32Array>) => {
      const pcm = resampleToS16(ev.data, this.srcRate);
      this.carry = concat(this.carry, pcm);
      while (this.carry.length >= CHUNK_SAMPLES) {
        const chunk = this.carry.slice(0, CHUNK_SAMPLES);
        this.carry = this.carry.slice(CHUNK_SAMPLES);
        onChunk(chunk);
        if (onLevel) onLevel(peakLevel(chunk));
      }
    };
    src.connect(this.node);
    // Pull the graph without audible monitoring: processor writes no output.
    this.node.connect(this.ctx.destination);
  }

  async stop(): Promise<void> {
    if (this.node) this.node.port.onmessage = null;
    this.node?.disconnect();
    this.node = null;
    this.stream?.getTracks().forEach((t) => t.stop());
    this.stream = null;
    if (this.ctx) {
      await this.ctx.close().catch(() => undefined);
      this.ctx = null;
    }
    this.carry = new Int16Array(0);
  }
}

function concat(a: Int16Array, b: Int16Array): Int16Array {
  const out = new Int16Array(a.length + b.length);
  out.set(a);
  out.set(b, a.length);
  return out;
}

function peakLevel(pcm: Int16Array): number {
  let peak = 0;
  for (let i = 0; i < pcm.length; i += 8) peak = Math.max(peak, Math.abs(pcm[i]));
  return peak / 0x8000;
}
