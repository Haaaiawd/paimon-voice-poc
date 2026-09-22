import type { ChatBackend } from './ChatBackend';
import type { ClientFrame, ServerFrame } from '../types';
import { isServerFrame } from '../types';

/**
 * WsBackend — stage 2 (FRONTEND_DEMO_DESIGN.md §4). Implemented against the
 * contract now so swapping VITE_BACKEND=ws is a pure config change once the
 * backend ws_gateway module exists (TASK-017).
 *
 * Contract rules honoured here:
 *  - one connection per session; session.start on open, session.end on close;
 *  - unknown server frame types are ignored (forward-compat discipline);
 *  - binary audio frames are stage-3 and ignored for now.
 */
export class WsBackend implements ChatBackend {
  private ws: WebSocket | null = null;
  private handlers = new Set<(frame: ServerFrame) => void>();

  constructor(private url: string) {}

  connect(): void {
    this.ws = new WebSocket(this.url);
    this.ws.onopen = () => this.send({ type: 'session.start' });
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
    this.ws.onclose = () =>
      this.emit({ type: 'error', message: 'WebSocket closed' });
  }

  sendText(text: string, clientMsgId: string): void {
    this.send({ type: 'user.text', text, client_msg_id: clientMsgId });
  }

  close(): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.send({ type: 'session.end' });
    }
    this.ws?.close();
    this.ws = null;
    this.handlers.clear();
  }

  onFrame(handler: (frame: ServerFrame) => void): () => void {
    this.handlers.add(handler);
    return () => this.handlers.delete(handler);
  }

  private send(frame: ClientFrame): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(frame));
    }
  }

  private emit(frame: ServerFrame): void {
    for (const h of this.handlers) h(frame);
  }
}
