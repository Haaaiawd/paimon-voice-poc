import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  type ReactNode,
} from 'react';
import { SketchPageHeader, SketchPaper } from 'blackchalk';
import type {
  AgentReply,
  ChatMessage,
  Notice,
  PipelineState,
  ServerFrame,
} from './types';
import type { ChatBackend } from './backend/ChatBackend';
import { MockBackend } from './backend/MockBackend';
import { WsBackend } from './backend/WsBackend';
import { ChatList } from './components/ChatList';
import { Composer } from './components/Composer';
import { StateBar } from './components/StateBar';

// ---- state (useReducer + Context — FRONTEND_DEMO_DESIGN.md §1.2) ----

interface ChatState {
  messages: ChatMessage[];
  pipeline: PipelineState;
  /** True while the backend is THINKING and no reply.delta has arrived yet. */
  typing: boolean;
  notices: Notice[];
  lastSefaMs: number | null;
}

type Action =
  | { kind: 'user.sent'; text: string; id: string }
  | { kind: 'frame'; frame: ServerFrame }
  | { kind: 'notice'; text: string };

let nextId = 0;
const uid = () => `m${++nextId}`;

function reducer(state: ChatState, action: Action): ChatState {
  switch (action.kind) {
    case 'user.sent':
      return {
        ...state,
        messages: [
          ...state.messages,
          { id: action.id, role: 'user', text: action.text },
        ],
      };
    case 'notice':
      return {
        ...state,
        notices: [...state.notices, { id: uid(), text: action.text }],
      };
    case 'frame': {
      const f = action.frame;
      switch (f.type) {
        case 'state':
          return { ...state, pipeline: f.state, typing: f.state === 'THINKING' };
        case 'reply.delta': {
          // Fold deltas into a single streaming Paimon bubble.
          const last = state.messages[state.messages.length - 1];
          if (last?.role === 'paimon' && last.streaming) {
            const merged = { ...last, text: last.text + f.text };
            return {
              ...state,
              typing: false,
              messages: [...state.messages.slice(0, -1), merged],
            };
          }
          return {
            ...state,
            typing: false,
            messages: [
              ...state.messages,
              { id: uid(), role: 'paimon', text: f.text, streaming: true },
            ],
          };
        }
        case 'reply.final': {
          const reply: AgentReply = {
            speech: f.speech,
            emotion: f.emotion,
            energy: f.energy,
          };
          const last = state.messages[state.messages.length - 1];
          if (last?.role === 'paimon' && last.streaming) {
            const done = { ...last, text: reply.speech, reply, streaming: false };
            return {
              ...state,
              typing: false,
              messages: [...state.messages.slice(0, -1), done],
            };
          }
          return {
            ...state,
            typing: false,
            messages: [
              ...state.messages,
              { id: uid(), role: 'paimon', text: reply.speech, reply },
            ],
          };
        }
        case 'latency':
          return { ...state, lastSefaMs: f.sefa_ms };
        case 'interrupted':
          return {
            ...state,
            notices: [
              ...state.notices,
              { id: uid(), text: `派蒙被打断（听到：${f.heard_text}）` },
            ],
          };
        case 'error':
          return {
            ...state,
            notices: [
              ...state.notices,
              { id: uid(), text: `连接异常：${f.message}` },
            ],
          };
        // asr.* / audio.chunk: contract vocabulary the bubble UI doesn't render yet.
        case 'asr.partial':
        case 'asr.final':
        case 'audio.chunk':
          return state;
      }
    }
  }
}

interface ChatContextValue {
  state: ChatState;
  send: (text: string) => void;
  simulateMic: () => void;
}

const ChatContext = createContext<ChatContextValue | null>(null);

export function useChat(): ChatContextValue {
  const ctx = useContext(ChatContext);
  if (!ctx) throw new Error('useChat outside ChatProvider');
  return ctx;
}

// ---- backend selection (VITE_BACKEND=mock | ws) ----

const BACKEND_KIND = import.meta.env.VITE_BACKEND ?? 'mock';

function makeBackend(): ChatBackend {
  if (BACKEND_KIND === 'ws') {
    const url = import.meta.env.VITE_WS_URL ?? '/ws/chat';
    // Same-origin relative path → absolute ws URL (vite dev proxy forwards /ws).
    const absolute = url.startsWith('ws')
      ? url
      : `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${url}`;
    return new WsBackend(absolute);
  }
  return new MockBackend();
}

// ---- provider + page ----

function ChatProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, {
    messages: [],
    pipeline: 'IDLE',
    typing: false,
    notices: [],
    lastSefaMs: null,
  });
  const backendRef = useRef<ChatBackend | null>(null);

  useEffect(() => {
    const backend = makeBackend();
    backendRef.current = backend;
    const off = backend.onFrame((frame) => dispatch({ kind: 'frame', frame }));
    backend.connect();
    return () => {
      off();
      backend.close();
    };
  }, []);

  const value = useMemo<ChatContextValue>(
    () => ({
      state,
      send: (text) => {
        const id = uid();
        dispatch({ kind: 'user.sent', text, id });
        backendRef.current?.sendText(text, id);
      },
      simulateMic: () => {
        // Mic is visual-only this stage: 2s fake LISTENING then an honest notice.
        dispatch({ kind: 'frame', frame: { type: 'state', state: 'LISTENING' } });
        window.setTimeout(() => {
          dispatch({ kind: 'frame', frame: { type: 'state', state: 'IDLE' } });
          dispatch({
            kind: 'notice',
            text: '语音输入 demo 阶段还没接通——先打字聊吧',
          });
        }, 2000);
      },
    }),
    [state],
  );

  return <ChatContext.Provider value={value}>{children}</ChatContext.Provider>;
}

export default function App() {
  return (
    <ChatProvider>
      <Page />
    </ChatProvider>
  );
}

function Page() {
  const { state, send, simulateMic } = useChat();

  return (
    <div className="page">
      <SketchPageHeader
        title="派蒙 Voice PoC"
        description="Blackchalk 手绘风 demo · 阶段 1（mock）"
      />
      <StateBar
        state={state.pipeline}
        backendKind={BACKEND_KIND}
        lastSefaMs={state.lastSefaMs}
      />
      <SketchPaper className="chat-paper">
        <ChatList
          messages={state.messages}
          typing={state.typing}
          notices={state.notices}
        />
      </SketchPaper>
      <Composer disabled={false} onSend={send} onMic={simulateMic} />
    </div>
  );
}
