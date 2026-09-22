import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { SketchPageHeader, SketchPaper } from 'blackchalk';
import type {
  AgentReply,
  ChatMessage,
  PipelineState,
  ServerFrame,
} from './types';
import type { ChatBackend } from './backend/ChatBackend';
import { MockBackend } from './backend/MockBackend';
import { WsBackend } from './backend/WsBackend';
import { MicCapture } from './audio/MicCapture';
import { ReplyPlayer, type NowPlaying } from './audio/ReplyPlayer';
import { MOCK_MEMORY } from './mocks/memory';
import { ChatList } from './components/ChatList';
import { Composer } from './components/Composer';
import { MemorySidebar } from './components/MemorySidebar';
import { NowPlayingBar } from './components/NowPlayingBar';
import { StateBar } from './components/StateBar';

// ---- state (useReducer + Context — FRONTEND_DEMO_DESIGN.md §1.2) ----

interface ChatState {
  messages: ChatMessage[];
  pipeline: PipelineState;
  /** True while the backend is THINKING and no reply.delta has arrived yet. */
  typing: boolean;
  lastSefaMs: number | null;
  /**
   * Text of the last locally-sent user message awaiting its asr.final echo.
   * ws_gateway projects text sends back as asr.final; we skip that echo so
   * typed text isn't double-rendered — but a voice turn's asr.final is the
   * only place the user's spoken utterance appears, so it must render.
   */
  pendingEcho: string | null;
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
        pendingEcho: action.text,
        messages: [
          ...state.messages,
          { id: action.id, role: 'user', text: action.text },
        ],
      };
    case 'notice':
      return {
        ...state,
        messages: [
          ...state.messages,
          { id: uid(), role: 'notice', text: action.text },
        ],
      };
    case 'frame': {
      const f = action.frame;
      switch (f.type) {
        case 'state':
          return { ...state, pipeline: f.state, typing: f.state === 'THINKING' };
        case 'asr.final': {
          if (state.pendingEcho !== null && f.text === state.pendingEcho) {
            return { ...state, pendingEcho: null };
          }
          return {
            ...state,
            pendingEcho: null,
            messages: [
              ...state.messages,
              { id: uid(), role: 'user', text: f.text },
            ],
          };
        }
        case 'reply.delta': {
          if (!f.text) return { ...state, typing: false }; // 空增量不建气泡
          // speech 增量进主气泡；followup 增量进第二气泡（追问）。
          const last = state.messages[state.messages.length - 1];
          const isFollowup = f.field === 'followup';
          if (last?.role === 'paimon' && last.streaming) {
            const merged = isFollowup
              ? { ...last, followup: (last.followup ?? '') + f.text }
              : { ...last, text: last.text + f.text };
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
              {
                id: uid(),
                role: 'paimon',
                text: isFollowup ? '' : f.text,
                followup: isFollowup ? f.text : undefined,
                streaming: true,
              },
            ],
          };
        }
        case 'reply.final': {
          const reply: AgentReply = {
            speech: f.speech,
            followup: f.followup,
            emotion: f.emotion,
            energy: f.energy,
          };
          const last = state.messages[state.messages.length - 1];
          // NOOP：模型选择不说（speech 空且无追问）。reply.final 是轮次
          // 生命周期信号必须照常消费，但不渲染空气泡；若有空的
          // streaming 残留气泡一并清掉。
          if (!reply.speech.trim() && !(reply.followup ?? '').trim()) {
            if (
              last?.role === 'paimon' &&
              last.streaming &&
              !last.text.trim() &&
              !(last.followup ?? '').trim()
            ) {
              return {
                ...state,
                typing: false,
                messages: state.messages.slice(0, -1),
              };
            }
            return { ...state, typing: false };
          }
          if (last?.role === 'paimon' && last.streaming) {
            const done = {
              ...last,
              text: reply.speech,
              followup: reply.followup ?? last.followup,
              reply,
              streaming: false,
            };
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
              {
                id: uid(),
                role: 'paimon',
                text: reply.speech,
                followup: reply.followup,
                reply,
              },
            ],
          };
        }
        case 'latency':
          return { ...state, lastSefaMs: f.sefa_ms };
        case 'interrupted': {
          // 打断提示进入消息时间线：先把在播的流式气泡收掉，
          // 提示插在它后面——后续对话自然排在提示下方。
          const last = state.messages[state.messages.length - 1];
          const base =
            last?.role === 'paimon' && last.streaming
              ? [
                  ...state.messages.slice(0, -1),
                  { ...last, streaming: false },
                ]
              : state.messages;
          return {
            ...state,
            typing: false,
            messages: [
              ...base,
              {
                id: uid(),
                role: 'notice',
                text: `派蒙被打断（听到：${f.heard_text}）`,
              },
            ],
          };
        }
        case 'error':
          return {
            ...state,
            messages: [
              ...state.messages,
              { id: uid(), role: 'notice', text: `连接异常：${f.message}` },
            ],
          };
        // asr.partial / audio.chunk: partials aren't rendered; audio chunk
        // payloads arrive as binary frames on the backend's onAudio channel.
        case 'asr.partial':
        case 'reply.emotion':
        case 'audio.chunk':
          return state;
      }
    }
  }
}

interface ChatContextValue {
  state: ChatState;
  send: (text: string) => void;
  recording: boolean;
  micLevel: number;
  toggleVoice: () => void;
  nowPlaying: NowPlaying | null;
  stopPlayback: () => void;
  memoryOpen: boolean;
  toggleMemory: () => void;
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
    lastSefaMs: null,
    pendingEcho: null,
  });
  const backendRef = useRef<ChatBackend | null>(null);
  const micRef = useRef(new MicCapture());
  const playerRef = useRef(new ReplyPlayer());
  const [recording, setRecording] = useState(false);
  const [micLevel, setMicLevel] = useState(0);
  const [nowPlaying, setNowPlaying] = useState<NowPlaying | null>(null);
  const [memoryOpen, setMemoryOpen] = useState(false);
  // Latest pipeline state for use inside stable callbacks (barge-in check).
  const pipelineRef = useRef<PipelineState>('IDLE');
  pipelineRef.current = state.pipeline;

  useEffect(() => {
    const backend = makeBackend();
    backendRef.current = backend;
    const player = playerRef.current;

    const handleFrame = (frame: ServerFrame) => {
      dispatch({ kind: 'frame', frame });
      // Audio playback lifecycle (stage 3): chunks are scheduled as they
      // arrive; reply.final closes the turn, interrupted drops everything.
      if (frame.type === 'interrupted') {
        player.stopAll();
      } else if (frame.type === 'reply.final') {
        player.endTurn();
      }
    };
    const offFrame = backend.onFrame(handleFrame);
    const offAudio = backend.onAudio((payload, meta) =>
      player.push(payload, meta.format),
    );
    const offPlayer = player.onState(setNowPlaying);
    backend.connect();
    return () => {
      offFrame();
      offAudio();
      offPlayer();
      void micRef.current.stop();
      player.stopAll();
      backend.close();
    };
  }, []);

  const value = useMemo<ChatContextValue>(
    () => ({
      state,
      send: (text) => {
        const id = uid();
        // User gesture: unlock the AudioContext so reply audio can play.
        void playerRef.current.unlock();
        dispatch({ kind: 'user.sent', text, id });
        backendRef.current?.sendText(text, id);
      },
      recording,
      micLevel,
      toggleVoice: () => {
        const backend = backendRef.current;
        if (!backend) return;
        if (recording) {
          backend.sendAudioEnd();
          void micRef.current.stop();
          setRecording(false);
          setMicLevel(0);
          return;
        }
        // Mic toggle is a user gesture: unlock the AudioContext first.
        void playerRef.current.unlock();
        backend.sendAudioStart();
        // Local half of barge-in: user starts talking → Paimon hushes now,
        // without waiting for the server's interrupted frame.
        if (pipelineRef.current === 'SPEAKING') playerRef.current.stopAll();
        micRef.current
          .start(
            (pcm) =>
              backend.sendAudioChunk(
                pcm.buffer.slice(
                  pcm.byteOffset,
                  pcm.byteOffset + pcm.byteLength,
                ) as ArrayBuffer,
              ),
            setMicLevel,
          )
          .then(() => setRecording(true))
          .catch(() => {
            backend.sendAudioEnd();
            dispatch({
              kind: 'notice',
              text: '麦克风没接通——检查浏览器权限，或先打字聊',
            });
          });
      },
      nowPlaying,
      stopPlayback: () => playerRef.current.stopAll(),
      memoryOpen,
      toggleMemory: () => setMemoryOpen((v) => !v),
    }),
    [state, recording, micLevel, nowPlaying, memoryOpen],
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
  const {
    state,
    send,
    recording,
    micLevel,
    toggleVoice,
    nowPlaying,
    stopPlayback,
    memoryOpen,
    toggleMemory,
  } = useChat();

  return (
    <div className="page">
      <SketchPageHeader
        title="派蒙 Voice PoC"
        description="Blackchalk 手绘风 demo · 阶段 3（语音上行 + 播放 + 记忆 UI）"
      />
      <div className="content-row">
        <div className="chat-col">
          <StateBar
            state={state.pipeline}
            backendKind={BACKEND_KIND}
            lastSefaMs={state.lastSefaMs}
            memoryOpen={memoryOpen}
            onToggleMemory={toggleMemory}
          />
          <NowPlayingBar playing={nowPlaying} onStop={stopPlayback} />
          <SketchPaper className="chat-paper">
            <ChatList messages={state.messages} typing={state.typing} />
          </SketchPaper>
          <Composer
            disabled={false}
            recording={recording}
            micLevel={micLevel}
            onSend={send}
            onMicToggle={toggleVoice}
          />
        </div>
        {memoryOpen && (
          <MemorySidebar memory={MOCK_MEMORY} onClose={toggleMemory} />
        )}
      </div>
    </div>
  );
}
