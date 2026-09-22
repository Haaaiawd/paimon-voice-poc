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

## 阶段 2 — WsBackend 接真管线（TASK-017，等后端 ws_gateway）

- [ ] 后端 `src/runtime/ws_gateway.py`（后端任务，不在本目录）
- [ ] `VITE_BACKEND=ws` 实测对话
- [ ] 契约一致性核对（§4 全字段 vs EventBus/AgentReply）
- [ ] 可选：浏览器播放 TTS 音频

## 阶段 3 — 语音上行 / 打断 / 记忆 UI（TASK-018）

- [ ] 浏览器麦克风采集 → PCM 16kHz/16bit/mono 二进制帧上行
- [ ] barge-in 交互
- [ ] 记忆 UI（mock 原型）
