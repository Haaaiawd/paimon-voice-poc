import { SketchBadge, SketchButton, SketchDivider, SketchIcon } from 'blackchalk';
import type { DemoMemory } from '../mocks/memory';

interface MemorySidebarProps {
  memory: DemoMemory;
  onClose: () => void;
}

/**
 * Memory sidebar — mock prototype only (TASK-018 boundary: not wired to the
 * real memory system). Entries follow MEMORY_SYSTEM_DESIGN.md §4: facts /
 * session summaries / paimon_state, written in Paimon's voice.
 */
export function MemorySidebar({ memory, onClose }: MemorySidebarProps) {
  return (
    <aside className="memory-panel" aria-label="派蒙的记忆">
      <div className="memory-header">
        <span className="memory-title">
          <SketchIcon name="bookmark" size={14} />
          派蒙的记忆
        </span>
        <SketchBadge variant="muted">mock</SketchBadge>
        <SketchButton variant="tertiary" aria-label="收起记忆栏" onClick={onClose}>
          <SketchIcon name="close" size={13} />
        </SketchButton>
      </div>

      <div className="memory-section">
        <div className="memory-section-title">facts</div>
        <ul className="memory-list">
          {memory.facts.map((f) => (
            <li key={f.id} className="memory-item">
              <span className="memory-text">「{f.text}」</span>
              <span className="memory-meta">{f.learned_at}</span>
            </li>
          ))}
        </ul>
      </div>

      <SketchDivider />

      <div className="memory-section">
        <div className="memory-section-title">sessions</div>
        <ul className="memory-list">
          {memory.sessions.map((s) => (
            <li key={s.date} className="memory-item">
              <span className="memory-text">{s.summary}</span>
              <span className="memory-meta">{s.date}</span>
            </li>
          ))}
        </ul>
      </div>

      <SketchDivider />

      <div className="memory-section">
        <div className="memory-section-title">paimon_state</div>
        <div className="memory-state">
          <SketchBadge variant="muted">grudge ×{memory.paimon_state.grudge}</SketchBadge>
          <SketchBadge variant="muted">mood: {memory.paimon_state.mood}</SketchBadge>
          <SketchBadge variant="muted">
            last: {memory.paimon_state.last_session}
          </SketchBadge>
        </div>
      </div>
    </aside>
  );
}
