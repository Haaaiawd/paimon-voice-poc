import type { ChatBackend } from './ChatBackend';
import type { ClientFrame, ServerFrame } from '../types';
import { isServerFrame } from '../types';

/**
 * WsBackend — stage 2 (FRONTEND_DEMO_DESIGN.md §4). Talks to the real
 * pipeline via src/runtime/ws_gateway.py (`python -m runtime.ws_gateway`);
 * select with VITE_BACKEND=ws (+ optional VITE_WS_URL / vite /ws proxy).
 *
 * Contract rules honoured here:
 *  - one connection per session; session.start on open, session.end on close;
 *  - unknown server frame types are ignored (forward-compat discipline);
 *  - binary audio frames are stage-3 and ignored for now;
 *  - frames sent while the socket is still CONNECTING are queued and
 *    flushed on open, so early sends are never silently dropped;
 *  - intentional close() does not surface as an error frame.
 */
export class WsBackend implements ChatBackend {
  private ws: WebSocket | null = null;
  private handlers = new Set<(frame: ServerFrame) => void>();
  private pending: ClientFrame[] = [];
  private closing = false;

  constructor(private url: string) {}

  connect(): void {
    this.closing = false;
    this.ws = new WebSocket(this.url);
    this.ws.onopen = () => {
      this.send({ type: 'session.start' });
      for (const f of this.pending.splice(0)) this.send(f);
    };
    this.ws.onmessage = (ev) => {
      if (typeof ev.data !== 'string') return; // binary audio frames: stage 3
      let parsed: unknown;
      try {
        parsed = JSON.parse(ev.data);
      } catch {
        return; // malformed frame — not our vocabulary, drop it
      }
      if (!isServerFrame(parsed)) return; // unknown type — ignore per §4.3
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

  close(): void {
    this.closing = true;
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.send({ type: 'session.end' });
    }
    this.ws?.close();
    this.ws = null;
    this.pending = [];
    this.handlers.clear();
  }

  onFrame(handler: (frame: ServerFrame) => void): () => void {
    this.handlers.add(handler);
    return () => this.handlers.delete(handler);
  }

  private send(frame: ClientFrame): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(frame));
    } else if (this.ws?.readyState === WebSocket.CONNECTING) {
      this.pending.push(frame);
    }
  }

  private emit(frame: ServerFrame): void {
    for (const h of this.handlers) h(frame);
  }
}
