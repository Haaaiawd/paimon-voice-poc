# frontend/ demo 进度清单

契约与设计的唯一事实源：`.loom/design/FRONTEND_DEMO_DESIGN.md`。
本文件只跟踪实现进度，不藏设计决策。

## 阶段 1 — 脚手架 + 气泡界面 + MockBackend（TASK-016）

- [x] Vite + React + TS 脚手架（`npm run dev` → localhost:5173）
- [x] blackchalk@0.3.0 手绘组件接入（styles.css + fonts.css + Caveat/楷体回退链）
- [x] ChatBubble：用户右对齐 / 派蒙左对齐 + 头像占位 + 情绪徽标槽位
- [x] Composer：输入框 + 发送按钮 + Enter 发送（发出去立即上屏）
- [x] MockBackend：剧本回复（`mocks/script.ts`，真 AgentReply 形状，§8 情绪标签）
- [x] TypingIndicator：回复前 0.8–1.5s 随机延迟 + 三点手绘动画
- [x] EmotionBadge：单色图标 + 文字标签（不用颜色）
- [x] 语音按钮：视觉存在；点击 = 2s LISTENING 模拟 + "未接通"提示
- [x] StateBar：管线状态 + 最近一轮 SEFA ms
- [x] `.env.example`（VITE_BACKEND / VITE_WS_URL / VITE_HTTP_URL）
- [x] `vite.config.ts` dev proxy `/ws` → 后端 WS gateway
- [x] WsBackend：§4 契约实现已写好（接口不变），默认未启用

## 阶段 2 — WsBackend 接真管线（TASK-017）

- [x] 后端 `src/runtime/ws_gateway.py`（后端任务，不在本目录）
- [x] `VITE_BACKEND=ws` 实测对话
- [x] 契约一致性核对（§4 全字段 vs EventBus/AgentReply）
- [x] 浏览器播放 TTS 音频 → ws_gateway 经 _AudioTapPlayer 向 WS 广播
  `audio.chunk` 头 + PCM 二进制帧（2026-09-22 后端缺口已补）

## 阶段 3 — 语音上行 / 打断 / 记忆 UI（TASK-018）

- [x] 浏览器麦克风采集 → PCM 16kHz/16bit/mono 二进制帧上行
  （`audio/MicCapture.ts`：AudioWorklet → 重采样 16k → 80ms s16 帧 →
  `user.audio.start` + binary + `user.audio.end`）
- [x] TTS 音频播放：`audio/ReplyPlayer.ts` 首个 PCM 块到达即用
  AudioContext + AudioBufferSourceNode 连续调度（不等 reply.final），
  `Waveform.tsx` 手绘波形 + 播放头（真网关 audio.chunk + binary 已通）
- [x] barge-in 交互：SPEAKING 中点麦克风 → 本地立即停播 + 上行开窗；
  `interrupted` 帧 → 停播 + 丢弃缓冲
- [x] 情绪可视化：EmotionBadge 单色图标+标签（§8 八标签全集，TASK-016 已落）
- [x] 记忆 UI（mock 原型）：`mocks/memory.ts`（facts/sessions/paimon_state，
  派蒙语气引用）+ `MemorySidebar.tsx`，StateBar「记忆」按钮开合
- [x] 语音轮次的 asr.final 渲染为用户气泡（文本发送的 asr.final 回声去重）
