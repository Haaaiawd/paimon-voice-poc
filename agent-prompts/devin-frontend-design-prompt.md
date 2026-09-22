# Devin 任务：Paimon 前端 Demo 策划 + LOOM 集成方案

## 工作目录
`/home/haa/sites/paimon-voice-poc`

## 角色
你是架构设计师 + 前端策划，**只写设计文档，不写代码**。

## 背景
用户决定采用**方案一**：另起一个 React 前端项目，用 Blackchalk 手绘风组件库做 demo 界面，与 Python 后端完全解耦。

## 你的任务
产出 `.loom/design/FRONTEND_DEMO_DESIGN.md`，包含：

### 1. 前端项目结构
- 项目放在哪里？建议：`/home/haa/sites/paimon-voice-poc/frontend/` 或独立 repo？
- 技术栈：React + Blackchalk + Vite（轻量启动）
- 目录结构：`src/` 下怎么分（components / pages / hooks / styles / assets）
- 与后端通信方式：WebSocket / HTTP polling / 本地 mock？
- 环境变量：后端 API 地址、WebSocket 地址

### 2. LOOM 集成方案
- 前端项目是否也需要 LOOM？如果后端已经有 `.loom/`，前端要不要自己的 `.loom/`？
- 建议：前端保持轻量，不引入 LOOM，用简单的 `FEATURES.md` 或 `TODO.md` 管理 demo 功能
- 或者：前端作为后端 `.loom/` 的"交付物"之一，由后端 Devin 负责策划，前端 Devin 负责实现
- 明确责任边界：谁管前端 LOOM？谁管后端 LOOM？

### 3. Demo 功能清单（最小可用）
用户要求"气泡显示"，Demo 阶段不需要完整 pipeline，只需要：
- 一个聊天气泡界面（用户消息 + 派蒙回复）
- Blackchalk 手绘风样式
- 能打字输入，能显示回复
- 后端通信：先 mock（预设几条对话），后续再接真实 pipeline
- 可能的额外 demo 功能：
  - 语音输入按钮（视觉上存在，实际可以 mock）
  - 情绪指示器（用 Blackchalk 的图标/颜色表达）
  - 打字中动画（手绘风loading）

### 4. 与后端 pipeline 的对接计划
- Demo 阶段：前端 mock 数据，后端独立运行
- 后续阶段：前端通过 WebSocket 连接后端 pipeline
- 对接点：`POST /chat` 或 `WS /ws/chat`，请求/响应格式定义
- 不需要写代码，只需要定义接口契约

### 5. 实施路线图
- 阶段 1（现在）：前端项目初始化 + Blackchalk 气泡界面（Devin 实现）
- 阶段 2（TASK-012 后）：对接真实 pipeline
- 阶段 3（后续）：加入语音输入、情绪指示器、记忆系统 UI

### 6. 对现有 Task 路线图的影响
- 前端 Demo 是否要新增 LOOM Task？
- 如果加，Task ID 怎么安排？（TASK-013? 还是在现有 Task 里扩展？）
- 如果不新增 Task，怎么管理前端进度？
- 用户明确说"具体实现先不做"，所以方案里要写清楚：**现在只策划，不实现**

## 约束
- **不修改任何代码**
- **不修改 00_READ_ME_FIRST.md 到 07_DECISIONS_AND_OPEN_QUESTIONS.md**
- **不修改 .loom/tasks.json**
- 只产出 `.loom/design/FRONTEND_DEMO_DESIGN.md`

## 你需要读的
- `.loom/design/system.md`
- `.loom/design/REALTIME_PLUS_EVALUATION.md`
- `.loom/design/MEMORY_SYSTEM_DESIGN.md`
- `.loom/tasks.json`（TASK-010 ~ TASK-012）
- `.loom/PROJECT.md`
- Blackchalk 官网：https://www.blackchalk.design/（已提取关键信息）
