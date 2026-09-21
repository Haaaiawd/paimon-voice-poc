# 07 — Decisions & Open Questions

## 已决定

### 产品范围

- 第一阶段 PC。
- 纯语音。
- 不做 3D。
- 不做直播。
- 不做手机。
- 不做 X4 Air。

### 角色

- 内部原型直接使用“派蒙”。
- 当前重点是派蒙式实时互动，而不是 IP 产品化问题。

### 开源策略

- 使用成熟开源底盘。
- 不重复造 VAD / Turn Detection / RTC。
- Conversation Core 自己写。

### Runtime

优先：

- Python
- Pipecat

### Turn Detection

优先：

```text
Silero VAD
+
Pipecat Smart Turn
```

### LLM

做 Provider 抽象。

优先先写：

```text
OpenAI-compatible
```

然后用现有 API 赛马。

### TTS

核心标准：

1. 低 TTFA；
2. 中文自然；
3. 情绪好；
4. 可 streaming；
5. 易取消。

优先赛马：

- Fish Audio
- MiniMax
- ElevenLabs

后续：

- CosyVoice 自部署

### ASR

暂未锁定。

第一阶段优先：

> 低延迟 Streaming ASR API

因为当前重点不是证明 ASR 能本地跑。

后续再评估：

- FunASR
- SenseVoice
- Whisper 类方案

### Audio Output

开发第一阶段建议戴耳机。

原因：

暂时跳过复杂 AEC / 回声消除。

## 尚未决定

> 更新（2026-09-21）：1–3 已拍板——ASR=阿里云百炼 paraformer-realtime-v2（D-010）、
> LLM 赛马=DeepSeek + 通义千问、TTS=Fish + 百炼 CosyVoice 双 adapter 赛马（D-014）。
> 保留原文备查。

### 1. 第一轮 ASR Provider

需要选择一个能最快打通 Streaming 的。

建议下一步根据现有 API Key 情况决定。

### 2. 第一轮 LLM

需要做 2–4 个候选 TTFT benchmark。

不要靠印象决定。

### 3. 第一轮 TTS

Fish / MiniMax / ElevenLabs 至少测两个。

### 4. 主动插话频率

第一版用规则。

实际聊天后调参数。

### 5. 派蒙是否允许主动打断用户

建议：

第一版默认 **不真正打断正在讲话的用户**。

“主动插话”先限定在用户轮次结束后、长沉默、明显留白等场景。

以后再实验真正 full-duplex overlap。

原因：

这是体验最容易失控的地方。

### 6. Echo Cancellation

第一版耳机。

产品化以后再加入：

- WebRTC AEC；
- system loopback；
- double-talk protection。

## 下一阶段真正开始开发前需要确认

只需要确认：

1. 可直接使用的 ASR API；✅ 阿里云百炼 paraformer-realtime-v2（key 已到位，实测通过）
2. 第一批 LLM endpoint；✅ DeepSeek + 通义千问（DashScope key 已到位，qwen-flash TTFT ~250ms 实测）
3. 第一批 TTS API Key；✅ Fish Audio + 百炼 CosyVoice（双 key 就位，双 adapter 赛马）
4. 开发机系统（Windows/macOS/Linux）；✅ Windows 原生（不走 WSL）
5. Python 版本/是否允许 Docker。✅ Python 3.12 + Docker 用于依赖打包

（2026-09-21 全部确认，详见 .loom/DECISIONS.md D-009–D-011）

## 后续阶段

Voice Core 成立后：

```text
Phase 2
手机端

Phase 3
直播接入

Phase 4
视觉 / Host Pose

Phase 5
3D Avatar + Lip Sync + Animation
```

不要提前进入后面的阶段。
