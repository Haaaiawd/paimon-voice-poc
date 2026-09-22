import type { ServerFrame } from '../types';

/**
 * ChatBackend — the frontend's only coupling to the world behind the bubbles.
 *
 * Same shape of responsibility as the backend's Provider abstraction (D-004):
 * stage 1 = MockBackend (scripted), stage 2 = WsBackend (§4 contract).
 * Swapping implementations must not touch any component.
 */
export interface ChatBackend {
  /** Open the session (Mock: no-op + initial state frame; Ws: socket + session.start). */
  connect(): void;
  /** Send one user utterance. clientMsgId lets the backend echo/ignore stale sends. */
  sendText(text: string, clientMsgId: string): void;
  /** Tear down. */
  close(): void;
  /**
   * Subscribe to server→client frames. Returns an unsubscribe function.
   * Implementations only ever emit the §4.3 vocabulary; unknown types are
   * filtered at the implementation boundary, never in components.
   */
  onFrame(handler: (frame: ServerFrame) => void): () => void;
}
