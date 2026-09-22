import type { ChatBackend } from './ChatBackend';
import type { ServerFrame } from '../types';
import { pickReply } from '../mocks/script';

const rand = (min: number, max: number) => min + Math.random() * (max - min);
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/**
 * MockBackend — stage 1 (FRONTEND_DEMO_DESIGN.md §1.4).
 *
 * Drives the §4.3 event vocabulary off the scripted replies so the UI sees the
 * exact frame sequence a real pipeline will emit:
 *   state THINKING → (0.8–1.5s typing delay) → state SPEAKING
 *   → reply.delta chunks → reply.final → latency → state IDLE
 */
export class MockBackend implements ChatBackend {
  private handlers = new Set<(frame: ServerFrame) => void>();
  private turn = 0;
  private closed = false;

  connect(): void {
    this.closed = false;
    queueMicrotask(() => this.emit({ type: 'state', state: 'IDLE' }));
  }

  sendText(text: string, _clientMsgId: string): void {
    const reply = pickReply(text, this.turn++);
    const t0 = performance.now();
    void (async () => {
      this.emit({ type: 'state', state: 'THINKING' });
      await sleep(rand(800, 1500)); // typing delay — acceptance: 0.8–1.5s random
      if (this.closed) return;
      this.emit({ type: 'state', state: 'SPEAKING' });
      // Stream the reply as a few delta chunks, then the final AgentReply frame.
      const chunkCount = Math.min(3, Math.max(1, Math.ceil(reply.speech.length / 8)));
      const step = Math.ceil(reply.speech.length / chunkCount);
      for (let i = 0; i < reply.speech.length; i += step) {
        this.emit({ type: 'reply.delta', text: reply.speech.slice(i, i + step) });
        await sleep(rand(60, 140));
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
    })();
  }

  close(): void {
    this.closed = true;
    this.handlers.clear();
  }

  onFrame(handler: (frame: ServerFrame) => void): () => void {
    this.handlers.add(handler);
    return () => this.handlers.delete(handler);
  }

  private emit(frame: ServerFrame): void {
    if (this.closed) return;
    for (const h of this.handlers) h(frame);
  }
}
