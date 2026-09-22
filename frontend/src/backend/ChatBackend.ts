import type { AudioChunkMeta, ServerFrame } from '../types';

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
  /** §4.2 stage 3: open the binary uplink window (user.audio.start). */
  sendAudioStart(): void;
  /** §4.2 stage 3: one binary PCM 16kHz/16bit/mono frame. */
  sendAudioChunk(pcm: ArrayBuffer): void;
  /** §4.2 stage 3: close the uplink window (user.audio.end). */
  sendAudioEnd(): void;
  /** Tear down. */
  close(): void;
  /**
   * Subscribe to server→client frames. Returns an unsubscribe function.
   * Implementations only ever emit the §4.3 vocabulary; unknown types are
   * filtered at the implementation boundary, never in components.
   */
  onFrame(handler: (frame: ServerFrame) => void): () => void;
  /**
   * Subscribe to downlink binary audio payloads (§4.3 audio.chunk data).
   * Each payload is paired with the meta of its preceding audio.chunk
   * header frame. Returns an unsubscribe function.
   */
  onAudio(handler: (payload: ArrayBuffer, meta: AudioChunkMeta) => void): () => void;
}
