import type { ChatBackend } from './ChatBackend';
import type { AgentReply, AudioChunkMeta, ServerFrame } from '../types';
import { pickReply } from '../mocks/script';
import { MOCK_TTS_FORMAT, chunkPcm, synthSpeechPcm } from '../mocks/voice';

const rand = (min: number, max: number) => min + Math.random() * (max - min);
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Canned transcript for mock voice turns (real ASR text needs the gateway). */
const VOICE_TRANSCRIPT = '派蒙，听到我说话吗';

/**
 * MockBackend — stage 1/3 (FRONTEND_DEMO_DESIGN.md §1.4, §4).
 *
 * Drives the §4.3 event vocabulary off the scripted replies so the UI sees the
 * exact frame sequence a real pipeline will emit:
 *   state THINKING → (0.8–1.5s typing delay) → state SPEAKING
 *   → reply.delta chunks → audio.chunk headers + binary PCM payloads
 *   → reply.final → latency → state IDLE
 * Voice uplink is simulated: sendAudioStart→LISTENING, sendAudioEnd runs a
 * scripted voice turn (asr.final echo + reply), sendAudioChunk counts bytes.
 */
export class MockBackend implements ChatBackend {
  private handlers = new Set<(frame: ServerFrame) => void>();
  private audioHandlers = new Set<
    (payload: ArrayBuffer, meta: AudioChunkMeta) => void
  >();
  private turn = 0;
  private closed = false;
  private capturing = false;
  private uplinkBytes = 0;

  connect(): void {
    this.closed = false;
    queueMicrotask(() => this.emit({ type: 'state', state: 'IDLE' }));
  }

  sendText(text: string, _clientMsgId: string): void {
    const reply = pickReply(text, this.turn++);
    void this.runTurn(text, reply);
  }

  sendAudioStart(): void {
    this.capturing = true;
    this.uplinkBytes = 0;
    this.emit({ type: 'state', state: 'LISTENING' });
  }

  sendAudioChunk(pcm: ArrayBuffer): void {
    if (this.capturing) this.uplinkBytes += pcm.byteLength;
  }

  sendAudioEnd(): void {
    if (!this.capturing) return;
    this.capturing = false;
    const reply = pickReply(VOICE_TRANSCRIPT, this.turn++);
    void this.runTurn(VOICE_TRANSCRIPT, reply);
  }

  close(): void {
    this.closed = true;
    this.handlers.clear();
    this.audioHandlers.clear();
  }

  onFrame(handler: (frame: ServerFrame) => void): () => void {
    this.handlers.add(handler);
    return () => this.handlers.delete(handler);
  }

  onAudio(
    handler: (payload: ArrayBuffer, meta: AudioChunkMeta) => void,
  ): () => void {
    this.audioHandlers.add(handler);
    return () => this.audioHandlers.delete(handler);
  }

  /** One scripted turn: the same frame sequence ws_gateway projects. */
  private async runTurn(userText: string, reply: AgentReply): Promise<void> {
    const t0 = performance.now();
    // ws_gateway injects asr.partial/asr.final for user.text turns; mirror it.
    this.emit({ type: 'asr.partial', text: userText });
    this.emit({ type: 'asr.final', text: userText });
    this.emit({ type: 'state', state: 'THINKING' });
    await sleep(rand(800, 1500)); // typing delay — acceptance: 0.8–1.5s random
    if (this.closed) return;
    this.emit({ type: 'state', state: 'SPEAKING' });
    // Stream the reply as a few delta chunks.
    const chunkCount = Math.min(3, Math.max(1, Math.ceil(reply.speech.length / 8)));
    const step = Math.ceil(reply.speech.length / chunkCount);
    for (let i = 0; i < reply.speech.length; i += step) {
      this.emit({ type: 'reply.delta', text: reply.speech.slice(i, i + step) });
      await sleep(rand(60, 140));
      if (this.closed) return;
    }
    // Then TTS audio: audio.chunk JSON header + binary PCM payload, §4.3 shape.
    const pcmChunks = chunkPcm(synthSpeechPcm(reply.speech));
    for (let seq = 0; seq < pcmChunks.length; seq++) {
      const meta: AudioChunkMeta = { seq, format: MOCK_TTS_FORMAT };
      this.emit({ type: 'audio.chunk', seq: meta.seq, format: meta.format });
      const pcm = pcmChunks[seq];
      this.emitAudio(pcm.buffer.slice(0) as ArrayBuffer, meta);
      await sleep(rand(30, 70)); // faster than realtime — demo feel
      if (this.closed) return;
    }
    this.emit({
      type: 'reply.final',
      speech: reply.speech,
      emotion: reply.emotion,
      energy: reply.energy,
    });
    const sefa = performance.now() - t0;
    this.emit({ type: 'latency', sefa_ms: Math.round(sefa), barge_in_ms: 0 });
    this.emit({ type: 'state', state: 'IDLE' });
  }

  private emit(frame: ServerFrame): void {
    if (this.closed) return;
    for (const h of this.handlers) h(frame);
  }

  private emitAudio(payload: ArrayBuffer, meta: AudioChunkMeta): void {
    if (this.closed) return;
    for (const h of this.audioHandlers) h(payload, meta);
  }
}
