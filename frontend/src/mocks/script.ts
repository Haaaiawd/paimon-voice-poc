import type { AgentReply, Emotion } from '../types';

/**
 * Stage-1 script — FRONTEND_DEMO_DESIGN.md §3.4.
 *
 * Replies are REAL AgentReply objects ({speech, emotion, energy}); emotion is
 * constrained to the §8 label set, so mock data == future wire data.
 * Persona: short sentences, high energy, teasing allowed (05 §4/§8).
 */
export interface ScriptEntry {
  /** Any substring match (case-insensitive) on the user text selects this entry. */
  triggers: string[];
  reply: AgentReply;
}

const e = (emotion: Emotion, speech: string, energy: number): AgentReply => ({
  speech,
  emotion,
  energy,
});

export const SCRIPT: ScriptEntry[] = [
  {
    triggers: ['在吗', '在不在', '你好', 'hi', 'hello', '派蒙'],
    reply: e('excited', '在呢在呢！又有什么麻烦事要找派蒙？', 0.8),
  },
  {
    triggers: ['吃什么', '吃啥', '饿'],
    reply: e('teasing', '当然是甜甜花酿鸡！……欸，你不会又想让派蒙看着你吃吧？', 0.7),
  },
  {
    triggers: ['是谁', '叫什么', '名字'],
    reply: e('smug', '派蒙！最好的向导，应急食品那个绰号派蒙不承认！', 0.75),
  },
  {
    triggers: ['闭嘴', '安静', '别说话'],
    reply: e('annoyed', '哼，行吧行吧……就安静一小会儿哦。', 0.3),
  },
  {
    triggers: ['谢谢', '感谢'],
    reply: e('soft', '嘿嘿……跟派蒙客气什么嘛。', 0.5),
  },
  {
    triggers: ['再见', '拜拜', '走了'],
    reply: e('happy', '这么快就要走啦？下次再带派蒙出去玩哦！', 0.6),
  },
];

/** Off-script input still gets a real AgentReply, not a fake shape. */
export const FALLBACK_REPLIES: AgentReply[] = [
  e('confused', '唔……派蒙没听懂，但派蒙假装听懂了！', 0.5),
  e('teasing', '你在嘀咕什么呀？大声点，派蒙的耳朵可是很灵的！', 0.65),
  e('neutral', '嗯嗯，然后呢？派蒙听着呢。', 0.4),
];

export function pickReply(text: string, fallbackIndex: number): AgentReply {
  const lower = text.toLowerCase();
  for (const entry of SCRIPT) {
    if (entry.triggers.some((t) => lower.includes(t.toLowerCase()))) {
      return entry.reply;
    }
  }
  return FALLBACK_REPLIES[fallbackIndex % FALLBACK_REPLIES.length];
}
