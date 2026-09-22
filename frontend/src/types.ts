// Contract types — field names are copied verbatim from the backend schema and
// must not drift. Single source of truth for the vocabulary:
//   .loom/design/FRONTEND_DEMO_DESIGN.md §4 (WS frames)
//   03_CONVERSATION_CORE.md §6 (AgentReply)
//   05_PAIMON_PERSONA.md §8 (emotion label set)
//   02_SYSTEM_ARCHITECTURE.md §3 (state machine states)

/** 05_PAIMON_PERSONA.md §8 — the eight emotion labels. */
export const EMOTION_LABELS = [
  'neutral',
  'happy',
  'excited',
  'teasing',
  'annoyed',
  'confused',
  'smug',
  'soft',
] as const;
export type Emotion = (typeof EMOTION_LABELS)[number];

/** 02_SYSTEM_ARCHITECTURE.md §3 — the seven pipeline states. */
export const PIPELINE_STATES = [
  'IDLE',
  'LISTENING',
  'POSSIBLE_END',
  'THINKING',
  'SPEAKING',
  'INTERRUPTED',
  'SILENCED',
] as const;
export type PipelineState = (typeof PIPELINE_STATES)[number];

/** AgentReply — 03_CONVERSATION_CORE.md §6, verbatim field names. */
export interface AgentReply {
  speech: string;
  /** 接住之后顺势往旅行上引的追问；空串 = 本轮不追问。 */
  followup?: string;
  emotion: Emotion;
  /** 0..1 */
  energy: number;
}

export type ChatRole = 'user' | 'paimon' | 'notice';

export interface ChatMessage {
  id: string;
  role: ChatRole;
  text: string;
  /** Paimon 追问（followup 字段）——渲染为同头像下的第二个气泡。 */
  followup?: string;
  /** Paimon messages carry the full AgentReply so the emotion badge can render. */
  reply?: AgentReply;
  /** True while reply.delta frames are still arriving. */
  streaming?: boolean;
}

// ---- WS contract frames (FRONTEND_DEMO_DESIGN.md §4) ----

/** §4.2 client → server. */
export type ClientFrame =
  | { type: 'session.start' }
  | { type: 'user.text'; text: string; client_msg_id: string }
  | { type: 'user.audio.start' }
  | { type: 'user.audio.end' }
  | { type: 'session.end' };

/** §4.3 server → client. Unknown `type`s must be ignored (forward compat). */
export type ServerFrame =
  | { type: 'state'; state: PipelineState }
  | { type: 'asr.partial'; text: string }
  | { type: 'asr.final'; text: string }
  | { type: 'reply.delta'; text: string; field?: 'speech' | 'followup' }
  | { type: 'reply.emotion'; emotion: Emotion; energy: number }
  | {
      type: 'reply.final';
      speech: string;
      followup?: string;
      emotion: Emotion;
      energy: number;
    }
  | { type: 'audio.chunk'; seq: number; format: string }
  | { type: 'interrupted'; heard_text: string }
  | { type: 'latency'; sefa_ms: number; barge_in_ms: number }
  | { type: 'error'; message: string };

/** Header meta of a §4.3 audio.chunk frame; its payload rides a binary frame. */
export interface AudioChunkMeta {
  seq: number;
  format: string;
}

export const SERVER_FRAME_TYPES = [
  'state',
  'asr.partial',
  'asr.final',
  'reply.delta',
  'reply.final',
  'reply.emotion',
  'audio.chunk',
  'interrupted',
  'latency',
  'error',
] as const;

export function isServerFrame(value: unknown): value is ServerFrame {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as { type?: unknown }).type === 'string' &&
    (SERVER_FRAME_TYPES as readonly string[]).includes(
      (value as { type: string }).type,
    )
  );
}
