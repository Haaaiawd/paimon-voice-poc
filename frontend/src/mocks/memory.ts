/**
 * Mock memory — MEMORY_SYSTEM_DESIGN.md §4 (Demo 假记忆, 方案 B 的展示面).
 *
 * Shape mirrors the future `demo_memory.json` / MemoryProvider record layout:
 *   { facts[], sessions[], paimon_state }
 * Entries are written as Paimon-voice recollections ("用户上次说…"), not
 * encyclopedic statements — §4 防穿帮纪律. Boundary: mock prototype, NOT
 * wired to the real memory system (TASK-018 boundaries).
 */

export interface MemoryFact {
  id: string;
  /** Paimon-voice recollection text. */
  text: string;
  learned_at: string;
}

export interface SessionSummary {
  date: string;
  summary: string;
}

export interface PaimonState {
  grudge: number;
  mood: string;
  last_session: string;
}

export interface DemoMemory {
  facts: MemoryFact[];
  sessions: SessionSummary[];
  paimon_state: PaimonState;
}

export const MOCK_MEMORY: DemoMemory = {
  facts: [
    {
      id: 'f1',
      text: '你上周说在调一个叫 paimon-voice-poc 的语音 pipeline',
      learned_at: '2026-09-15',
    },
    {
      id: 'f2',
      text: '你说过要少喝咖啡——派蒙可是记着呢',
      learned_at: '2026-09-15',
    },
    {
      id: 'f3',
      text: '你不喜欢被叫"您"，要叫就叫名字',
      learned_at: '2026-09-18',
    },
  ],
  sessions: [
    {
      date: '2026-09-21',
      summary: '上次我们争论 SEFA 目标没谈拢，你说 500–800ms 才算及格',
    },
    {
      date: '2026-09-20',
      summary: '你答应下次带派蒙"出去玩"（原话，派蒙没夸张）',
    },
  ],
  paimon_state: {
    grudge: 1,
    mood: 'smug',
    last_session: '2026-09-21',
  },
};
