import type { ChatBackend } from './ChatBackend';
import type { AudioChunkMeta, ClientFrame, ServerFrame } from '../types';
import { isServerFrame } from '../types';

/**
 * WsBackend — stage 2/3 (FRONTEND_DEMO_DESIGN.md §4). Talks to the real
 * pipeline via src/runtime/ws_gateway.py (`python -m runtime.ws_gateway`);
 * select with VITE_BACKEND=ws (+ optional VITE_WS_URL / vite /ws proxy).
 *
 * Contract rules honoured here:
 *  - one connection per session; session.start on open, session.end on close;
 *  - unknown server frame types are ignored (forward-compat discipline);
 *  - uplink: user.audio.start/end bracket binary PCM 16kHz/16bit/mono frames
 *    (§4.2); downlink: an audio.chunk JSON header pairs with the binary
 *    payload frame that follows it (§4.3);
 *  - frames sent while the socket is still CONNECTING are queued and
 *    flushed on open, so early sends are never silently dropped;
 *  - intentional close() does not surface as an error frame.
 */
export class WsBackend implements ChatBackend {
  private ws: WebSocket | null = null;
  private handlers = new Set<(frame: ServerFrame) => void>();
  private audioHandlers = new Set<
    (payload: ArrayBuffer, meta: AudioChunkMeta) => void
  >();
  private pending: (ClientFrame | ArrayBuffer)[] = [];
  private closing = false;
  /** Header of the most recent audio.chunk frame; pairs with next binary. */
  private lastAudioMeta: AudioChunkMeta = { seq: -1, format: 'pcm24k' };

  constructor(private url: string) {}

  connect(): void {
    this.closing = false;
    this.ws = new WebSocket(this.url);
    this.ws.binaryType = 'arraybuffer';
    this.ws.onopen = () => {
      this.send({ type: 'session.start' });
      for (const f of this.pending.splice(0)) this.sendRaw(f);
    };
    this.ws.onmessage = (ev) => {
      if (ev.data instanceof ArrayBuffer) {
        for (const h of this.audioHandlers) h(ev.data, this.lastAudioMeta);
        return;
      }
      if (typeof ev.data !== 'string') return;
      let parsed: unknown;
      try {
        parsed = JSON.parse(ev.data);
      } catch {
        return; // malformed frame — not our vocabulary, drop it
      }
      if (!isServerFrame(parsed)) return; // unknown type — ignore per §4.3
      if (parsed.type === 'audio.chunk') {
        this.lastAudioMeta = { seq: parsed.seq, format: parsed.format };
      }
      for (const h of this.handlers) h(parsed);
    };
    this.ws.onerror = () =>
      this.emit({ type: 'error', message: 'WebSocket error' });
    this.ws.onclose = () => {
      if (!this.closing)
        this.emit({ type: 'error', message: 'WebSocket closed' });
    };
  }

  sendText(text: string, clientMsgId: string): void {
    this.send({ type: 'user.text', text, client_msg_id: clientMsgId });
  }

  sendAudioStart(): void {
    this.send({ type: 'user.audio.start' });
  }

  sendAudioChunk(pcm: ArrayBuffer): void {
    this.sendRaw(pcm);
  }

  sendAudioEnd(): void {
    this.send({ type: 'user.audio.end' });
  }

  close(): void {
    this.closing = true;
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.send({ type: 'session.end' });
    }
    this.ws?.close();
    this.ws = null;
    this.pending = [];
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

  private send(frame: ClientFrame): void {
    this.sendRaw(frame);
  }

  private sendRaw(frame: ClientFrame | ArrayBuffer): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(frame instanceof ArrayBuffer ? frame : JSON.stringify(frame));
    } else if (this.ws?.readyState === WebSocket.CONNECTING) {
      this.pending.push(frame);
    }
  }

  private emit(frame: ServerFrame): void {
    for (const h of this.handlers) h(frame);
  }
}
