# 前端 Demo 设计：Blackchalk 手绘风聊天气泡界面

- Kind: system（子系统设计文档）
- Status: draft —— 方向已由用户拍板（方案一：独立 React 前端 + Blackchalk，与 Python 后端解耦），
  本阶段**只策划不实现**；开工前需用户对 §5 路线图点头
- Date: 2026-09-22
- 关联：本文件是前端 demo 的唯一规范源；后端行为规范仍是根目录 `00_`–`07_*.md`

## TL;DR

在仓库内新增 `frontend/` 子目录（**不独立成 repo**）：Vite + React + TypeScript +
Blackchalk，单页聊天气泡界面。前后端解耦点是一纸 **WebSocket JSON 契约**（§4），
不是文件系统边界——契约字段直接复用现有 `AgentReply` schema、状态机七态与 EventBus
事件名，保证契约漂移成本接近零。Demo 阶段前端跑本地 mock（`ChatBackend` 接口的
Mock 实现，与后端 Provider 抽象同一思想）；后端在 TASK-010 之后新增一个 WS gateway
模块时，前端换实现即可接上真管线。**前端不引入自己的 `.loom/`**，全部项目真相留在
根 `.loom/`；demo 进度用 `frontend/TODO.md` 轻量管理。如未来纳入工作图，建议
TASK-016 起（TASK-013–015 已被记忆系统预占，见 `MEMORY_SYSTEM_DESIGN.md` §3.3）。

---

## 1. 前端项目结构

### 1.1 放在哪：`frontend/` 子目录，不独立成 repo

| 选项 | 优点 | 缺点 | 结论 |
|------|------|------|------|
| `frontend/` 子目录 | 单一 LOOM 真相源；契约与后端同仓演进；一个 git 历史；`loom context` 一次恢复全部上下文 | JS 工具链进入 Python 仓库（node_modules、第二套 lock 文件） | **采用** |
| 独立 repo | 物理隔离最彻底；可独立部署 | 契约漂移无人察觉；两份 LOOM 或一份缺失；单人 PoC 维护两面墙 | 不采用 |

"完全解耦"的落点是**运行时只经 §4 的 WS 契约通信、前端零 import 后端代码**，
而不是仓库边界。独立 repo 的触发条件（记录备查）：demo 变成对外产物、需要独立
部署/版本节奏、或接入第二支队伍。

工程细节：
- `.gitignore` 需补 `node_modules/`（`dist/`、`build/` 已被现有规则覆盖）；
- 引入 Node.js 工具链是本仓库第一个非 Python 依赖——PoC 内部原型可接受，
  Node >= 20 LTS + npm 即可，不引入 monorepo 工具（turborepo 等过度设计）；
- 前端 dev server 跑哪都行（Windows/Linux/浏览器端），不受 D-011 Windows
  原生约束影响——那条约束针对的是音频闭环。

### 1.2 技术栈

| 层 | 选型 | 理由 |
|----|------|------|
| 构建 | Vite | 轻量启动、dev server 自带 WS proxy |
| 框架 | React 18+ + TypeScript | Blackchalk 是 React 组件库；TS 契约对齐后端 schema |
| 组件库 | `blackchalk`（npm，MIT，v0.3.x） | rough.js 手绘线框风、单色可调 hue、~70 组件、
  单 stylesheet 无 runtime CSS-in-JS、"deliberately unfinished"
  的美学恰好向观众声明"这是原型" |
| 路由 | 不加 | 单页 demo |
| 状态 | React `useReducer` + Context | 会话状态机很小；不引 zustand/redux |
| 测试 | 暂不配 | demo 阶段人工验证；接 WS 后再补契约测试 |

中文排版注意：Blackchalk 单色描边是英文线框语境设计，中文手绘感主要靠字体栈
兜底（`"Kaiti SC","KaiTi","STKaiti",cursive` 一类的楷体/手写回退链），设计稿
中标注为样式约定，不阻塞实现。

### 1.3 目录结构（`frontend/` 内）

```text
frontend/
  index.html
  package.json
  vite.config.ts          # dev proxy: /ws → 后端 WS gateway
  .env.example            # 提交；.env.local 不入库
  TODO.md                 # demo 功能轻量清单（§2 责任边界）
  src/
    main.tsx
    App.tsx               # 单页装配：ChatList + Composer + 状态栏
    components/
      ChatBubble.tsx      # 用户/派蒙气泡（Blackchalk 手绘框）
      ChatList.tsx        # 气泡流 + 自动滚底
      Composer.tsx        # 输入框 + 发送 + 语音按钮（mock）
      EmotionBadge.tsx    # 情绪指示器（图标+标签，见 §3）
      TypingIndicator.tsx # 手绘风 "派蒙正在想…" 动画
      StateBar.tsx        # 管线状态条（LISTENING/THINKING/SPEAKING…）
    backend/
      ChatBackend.ts      # 接口：send(text) / events 订阅 / connect / close
      MockBackend.ts      # 阶段 1：剧本化回复
      WsBackend.ts        # 阶段 2：§4 契约实现（接口不变）
    mocks/
      script.ts           # 预设对话（含 emotion 字段，用真 schema）
    styles/               # Blackchalk theme 覆盖 + 中文字体栈
    assets/               # 派蒙头像手绘占位等
```

关键设计：`ChatBackend` 接口 + Mock/Ws 双实现，与后端"Provider 抽象 + adapter
赛马"（D-004）是同一权责形状——demo 期写的前端代码在接真管线时零返工。

### 1.4 与后端通信方式

| 方式 | 判断 |
|------|------|
| 本地 mock | **阶段 1 唯一方式**。后端此时甚至没有 WS server，mock 是唯一现实路径 |
| WebSocket | **阶段 2 主路**。后端管线是事件流（ASR partial、reply delta、打断、状态迁移），WS 与 EventBus 天然 1:1 |
| `POST /chat` HTTP | 可选调试旁路（文本进、整段文本出）。不接主流式事件，仅作"无 WS 环境下的冒烟入口" |
| HTTP polling | 否决。事件流用轮询等于自己重造推送，且拿不到打断/状态的时序 |

### 1.5 环境变量

前端（`frontend/.env.example`）：

```text
VITE_BACKEND=mock              # mock | ws
VITE_WS_URL=ws://localhost:8765/ws/chat
VITE_HTTP_URL=http://localhost:8765
```

后端（阶段 2 才需要，届时写入根 `.env.example`）：

```text
WS_GATEWAY_HOST=127.0.0.1
WS_GATEWAY_PORT=8765
WS_GATEWAY_CORS_ORIGINS=http://localhost:5173
```

## 2. LOOM 集成方案

### 2.1 前端不引入自己的 `.loom/`

- LOOM 是本项目的连续性设施，真相必须单一。`frontend/` 是同仓子目录，
  再开一个 `.loom/` 会造成"两份 PROJECT.md、两套 tasks"的漂移面——与独立
  repo 同理被否。
- 前端设计真相 = 本文件；前端任务真相 = 根 `tasks.json`（若 §6 提议被采纳）；
  前端日常进度 = `frontend/TODO.md`（普通项目文件，不进 LOOM 层）。
- 触发重新评估的条件：前端独立成 repo 或独立产品化（同 §1.1 触发条件）。

### 2.2 责任边界

| 层 | 谁维护 | 规则 |
|----|--------|------|
| 根 `.loom/`（含本文件、tasks.json、DECISIONS.md） | 在本仓库工作的 Agent（LOOM 协议：Agent 静默维护，人不操作） | 前端实现前如需新增 Task/Decision，由当时会话的 Agent 走 `loom` CLI 完成 |
| `frontend/` 源码 + `frontend/TODO.md` | 做前端实现的会话 | 把本文件当 contract 读；设计变更回本文件改，不在 TODO.md 里藏设计决策 |
| §4 WS 契约 | 前后端共同的唯一接口事实 | 契约变更 = 改本文件 + 前后端同步改实现；任一侧单方面改字段即违约 |

一句话：**策划归 LOOM（本文件），实现进度归 TODO.md，契约归本文件 §4。**

## 3. Demo 功能清单（最小可用）

### 3.1 必做（P0）

| 功能 | 说明 |
|------|------|
| 聊天气泡 | 用户右、派蒙左；Blackchalk 手绘描边气泡；派蒙气泡带头像占位与情绪徽标槽位 |
| 文本输入 | 输入框 + 发送按钮 + Enter 发送；发出去立即上屏 |
| Mock 回复 | `mocks/script.ts` 预设 4–6 轮剧本，回复对象直接用真 `AgentReply` 形状 `{speech, emotion, energy}`——mock 数据结构就是未来契约，不留假形状 |
| 打字中动画 | 派蒙回复前 0.8–1.5s 随机延迟 + 手绘风三点动画（TypingIndicator） |

### 3.2 应做（P1，同在第一批）

| 功能 | 说明 |
|------|------|
| 情绪指示器 | 每条派蒙回复携带 `emotion`（05 §8 八标签集）。**单色组件库下用图标/文字标签表达，不用颜色**——如 `(teasing)` 手绘小标签或简笔表情 glyph |
| 语音输入按钮 | 视觉存在的麦克风按钮；点击给出"demo 阶段未接通"的轻提示或模拟 2s "聆听中"动画。明确是 mock，不假装能用 |
| 管线状态条 | StateBar 展示当前状态（IDLE/LISTENING/THINKING/SPEAKING…），mock 阶段随剧本驱动——把终端 UI（06 §2）的信息量搬进气泡界面 |

### 3.3 不做（本阶段）

语音采集/播放、打断交互、真实 WS、记忆 UI、多会话、设置页。其中"浏览器播放
TTS 音频"列为阶段 2 的可选增强（§5）——听见派蒙声音的 demo 杀伤力远高于纯文字。

### 3.4 Mock 剧本要求

剧本台词按 05 §4/§8 人格写：短句、高能量、可吐槽、情绪标签真实取自八标签集。
示例形状（非实现）：

```json
{"user": "你在吗", "reply": {"speech": "在呢在呢！又有什么麻烦事要找派蒙？", "emotion": "excited", "energy": 0.8}}
```

## 4. 与后端 pipeline 的对接契约

### 4.1 形态

`WS /ws/chat`，一条连接承载一个会话，双向 JSON 文本帧 + 可选二进制音频帧
（阶段 3）。契约即 EventBus（03 §4）的序列化投影：**事件名、状态机状态名、
AgentReply 字段名原样复用**，不发明第二套词汇。

### 4.2 客户端 → 服务端

```json
{"type": "session.start"}
{"type": "user.text", "text": "…", "client_msg_id": "…"}
{"type": "user.audio.start"}                        // 阶段 3
{"type": "user.audio.end"}                          // 阶段 3
{"type": "session.end"}
```

音频数据走二进制帧（PCM 16kHz/16bit/mono，与 MicCapture 对齐），不进 JSON。

### 4.3 服务端 → 客户端

```json
{"type": "state",          "state": "IDLE|LISTENING|POSSIBLE_END|THINKING|SPEAKING|INTERRUPTED|SILENCED"}
{"type": "asr.partial",    "text": "…"}
{"type": "asr.final",      "text": "…"}
{"type": "reply.delta",    "text": "…"}                    // 可选：流式上屏
{"type": "reply.final",    "speech": "…", "emotion": "teasing", "energy": 0.7}
{"type": "audio.chunk",    "seq": 3, "format": "pcm24k"}   // 数据走二进制帧，阶段 2/3 可选
{"type": "interrupted",    "heard_text": "…"}              // AGENT_INTERRUPTED 投影
{"type": "latency",        "sefa_ms": 623, "barge_in_ms": 93}  // metrics 投影，UI 可展示
{"type": "error",          "message": "…"}
```

设计要点：
- `reply.final` 字段名 = `AgentReply`（03 §6）原字段，emotion 受 05 §8 八标签集约束；
- `state` 枚举 = 状态机七态原名；
- `latency` 帧让 demo 能秀出 SEFA 数字——呼应"延迟可观测"是项目卖点；
- 未知 `type` 前端必须忽略（向前兼容纪律）。

### 4.4 后端侧缺口（阶段 2 的工作，非本阶段）

后端当前无 WS server（UI 是终端）。阶段 2 需新增一个 gateway 模块
（建议落 `src/runtime/ws_gateway.py`，权责：EventBus 事件 → WS 帧、
WS 帧 → pipeline 输入注入）。它属于后端工作图，不进 `frontend/`。

## 5. 实施路线图

| 阶段 | 内容 | 前置 | 产出 |
|------|------|------|------|
| **0（现在）** | 本文件定稿；**不写代码** | — | 本文件 |
| **1** | `frontend/` 脚手架 + Blackchalk 气泡界面 + MockBackend + P0/P1 功能 | 用户对本文点头 | 可跑的纯前端 demo（`npm run dev` 即见气泡对话） |
| **2** | 后端 WS gateway + 前端 `WsBackend`；可选：浏览器播放 TTS 音频 | TASK-010 完成（pipeline 可端到端跑）之后；不必等 TASK-012 | 真实管线驱动的气泡界面 |
| **3** | 浏览器麦克风上行、barge-in 交互、记忆 UI | TASK-012 验收后；记忆 UI 另受 `MEMORY_SYSTEM_DESIGN.md` §6 触发条件约束 | 完整语音 demo |

纪律：**前端 demo 不替代 TASK-010 的 Terminal UI 验收**（06 §2："终端版本不好聊，
加 GUI 没意义"）。两条 UI 轨并行：终端管验收，浏览器管展示。

## 6. 对现有 Task 路线图的影响

### 6.1 现在不动 `tasks.json`

用户明确"具体实现先不做"，且本任务约束禁止改 `tasks.json`。以下为**提案**，
开工时由当时会话的 Agent 经 `loom` CLI 落图。

### 6.2 若纳入工作图（推荐在阶段 1 开工时落）

| 提案 | 内容 | covers |
|------|------|--------|
| TASK-016 | `frontend/` 脚手架 + 气泡界面 + MockBackend（§3 P0/P1） | 新增 DLV-012 `demo-web-ui` |
| TASK-017 | 后端 `ws_gateway` + 前端 `WsBackend` + §4 契约一致性验证 | DLV-012 |
| TASK-018 | 语音上行/音频播放/情绪与记忆 UI（阶段 3，可再拆） | DLV-012 |

编号说明：**TASK-013–015 已被 `MEMORY_SYSTEM_DESIGN.md` §3.3 预占**
（MemoryProvider 抽象/会话摘要/memsearch 选型），前端从 TASK-016 起，避免撞号。
依赖关系：TASK-016 无后端依赖（可立即开工）；TASK-017 `depends_on` 含
TASK-010（pipeline 端到端）与 TASK-016；TASK-018 视届时范围再定。

### 6.3 若不纳入工作图

前端进度全部由 `frontend/TODO.md` 承载。不推荐作为长期形态：WS gateway 是
跨前后端的真实工作，脱离工作图则 LOOM 恢复上下文时看不见它。轻量清单只适合
阶段 1 的纯前端窗口期。

### 6.4 对现有 Task 的零影响确认

TASK-010/011/012 验收口径不变；前端 demo 不挤占 SEFA 预算、不改 EventBus、
不动状态机。唯一后端改动面是阶段 2 新增的 `ws_gateway`（纯增量模块）。

## 7. 风险与诚实记录

- **"demo 抢跑核心"风险**：气泡界面好看但管线还没通。纪律已写进 §5——终端验收
  优先，demo 是展示层不是验收层。
- **Blackchalk 成熟度**：v0.3.x、单色线框定位；复杂组件（气泡流、动画）大概率
  要以其导出的 tokens 自绘而非现成组件拼装。这不影响选型成立（demo 要的就是
  手绘质感），但实现工时预估应含自绘量。
- **契约即承诺**：§4 字段名绑死了 `AgentReply`/状态机词汇。若未来 doc 03
  schema 演进，契约要同步改——这是故意耦合（换词汇一致性），不是疏漏。
- **语音 demo 的坑在阶段 3**：浏览器麦克风 → WS 上行 → 管线 → TTS 音频回流的
  全链路跨端延迟是新的未知面；阶段 2 若先演"文字气泡 + 本机终端出声"可绕开。
- **Node 工具链入仓**：Python-only 仓库引入 package-lock；CI/Docker 暂不碰
  前端（Dockerfile 只管 Python 依赖打包，见 D-011 语境），此决定开工时可再议。
- **本文件即决策记录载体**：方向已定但尚未走 `loom decision` 落 `D-xxx`；
  建议阶段 1 开工时补记一条"前端 demo 架构"决策（方案一 + 仓内子目录 + WS 契约）。

## 8. 来源

- 本仓库：`02_SYSTEM_ARCHITECTURE.md`、`03_CONVERSATION_CORE.md`（§4 事件面 /
  §5–6 AgentReply）、`05_PAIMON_PERSONA.md`（§8 情绪标签集）、
  `06_MVP_AND_EVALUATION.md`（§2 终端 UI、§8 MVP 定义）、
  `.loom/PROJECT.md`、`.loom/STRUCTURE.md`、`.loom/DECISIONS.md`（D-004/D-011/D-012）、
  `.loom/tasks.json`（TASK-010–012）、`.loom/deliverables.json`（DLV-008/010）、
  `.loom/design/MEMORY_SYSTEM_DESIGN.md`（§3.3 TASK-013–015 预占、§4 demo 假记忆）、
  `.loom/design/REALTIME_PLUS_EVALUATION.md`（评估文档格式先例）
- Blackchalk：`blackchalk.design`（v0.3.0，rough.js 手绘、单色 themeable、
  ~70 组件、tokens 导出、`npm install blackchalk`）、
  `github.com/free-agent83/blackchalk`
