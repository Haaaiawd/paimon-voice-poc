# TASK-012 MVP 验收记录 — 2026-09-22

执行环境：Linux 宿主（本机无音频输入/输出设备），脚本化 provider
（`src/runtime/simulated.py`：ScriptedVAD/Turn/ASR/LLM + PaceTTS +
WavSinkPlayer）驱动**真实** `VoicePipeline` + `ConversationCore`，按真实
时序定速送帧（32ms/frame）。真实供应商 E2E 在本机不可行（无声卡/麦克风；
`.env` 主 DASHSCOPE key 为 workspace realtime 专用），真实延迟数据引用
TASK-003 实测。驱动脚本：`scripts/mvp_acceptance.py`（`--cases` / `--soak`）。

回归基线：`pytest tests/` = **191 passed, 3 skipped**（skip 均为环境项：
无 chat key / 无输出设备 / 状态机语义占位）。

## 1. 用例 A–G 逐条结果

证据：`data/mvp_acceptance/results_20260922T042748Z.json` +
`data/mvp_acceptance/latency/session_*.jsonl`（每用例一份逐轮账本）。

| 用例 | 结果 | 关键证据 |
|------|------|----------|
| A 普通一问一答 | **PASS** | 单轮 completed；SEFA 555ms；speculative=hit；回复"当然是甜甜花酿鸡！派蒙强烈推荐！"；结束回 IDLE |
| B 句中停顿 | **PASS** | 第一次停顿 → TURN_INCOMPLETE 裁决，**未**触发回应；第二个 final 放行后单轮 complete，turn_text="我觉得这个东西吧，嗯，其实还行"；LLM 仅调用 1 次，回复 1 次 |
| C 半句打断 | **PASS** | turn1 stop_reason=interrupted；barge_in_stop=0.14ms；TTS cancel 生效；heard history 截断为"派蒙是提瓦特最棒的向"；下一轮 LLM 输入含 `assistant_was_interrupted` + `assistant_generated_but_not_heard`（知道自己没说完） |
| D 用户要求安静 | **PASS** | "你先闭嘴15秒"→SILENCE_REQUESTED→SILENCED；窗口内用户再说话轮次照常裁决但 0 次 AGENT_CAN_RESPOND、0 次主动插话；"派蒙，继续聊吧"含唤醒词→退出 SILENCED（唤醒路径，silenced_for=14.7s<15s 窗口）→正常回应 |
| E 长时间沉默 | **PASS** | ~95s 沉默中 INITIATIVE_TRIGGERED ×2（reason=silence_duration），间隔 45.1s≥cooldown(45s)，不过频；LLM 调用数受触发数约束 |
| F 用户连续讲话 | **PASS** | 45s 连续语音期间 0 次 AGENT_CAN_RESPOND/AGENT_SPEAKING/INITIATIVE；结束后单轮单回复 |
| G 连续互怼 | **PASS** | 4 轮短句全 completed；每轮 SEFA 536–537ms；heard history 完整保留 4 轮用户文本（"说就说。"为末轮）；回复与上下文一致 |

结论：**7/7 通过**（脚本化环境、确定性断言；非真人试聊）。

## 2. 连续对话 ≥10 分钟（soak）

命令：`python scripts/mvp_acceptance.py --soak --minutes 10.5`

证据：`data/mvp_acceptance/results_20260922T043408Z.json`、
`data/latency_log/session_20260922T042330Z.jsonl` +
`summary_20260922T042330Z.json`、`data/mvp_acceptance/playback_soak.wav`。

- 墙钟 **637.9s**（≥600s），**0 pipeline error**，结束回 IDLE；
- **26 轮**全部落账：SEFA n=23 p50 **536.3ms** p95 553.7ms（脚本化延迟口径）；
- 分阶段：speech_end→turn_confirmed p50 159.9ms；llm_request→first_token
  p50 350.8ms（模拟 TTFT）；tts→first_audio p50 0.6ms；
- speculative hit 24/24（有响应轮次）；turn_source 全 model；
- 覆盖行为：A/B/C/D/E/F/G 全谱 + 12 个填充问答轮；
- SILENCED 窗口内 0 回应、唤醒词正常退出；主动开口 2 次（受 cooldown 约束）。

**结果：PASS**（连续运行 ≥10 分钟无崩溃，latency log 完整）。

## 3. doc 06 §8 MVP 完成定义逐条核对

| # | 条件 | 判定 | 证据/备注 |
|---|------|------|-----------|
| 1 | 连续运行至少 10 分钟 | ✅ | soak 637.9s、0 error、session log 完整 |
| 2 | 句中停顿不频繁抢话 | ✅ | 用例 B：首次停顿 INCOMPLETE、不回应、整句单回复 |
| 3 | 用户可随时打断 | ✅ | 用例 C + soak：barge_in_stop p50 0.1ms（脚本化），六步打断+TTS cancel+停播 |
| 4 | 被打断后的历史正确 | ✅ | heard 按已播截断、interrupted 标记、interruption context 进下一轮 LLM 输入 |
| 5 | 对话延迟达到可接受范围 | ⚠️ 部分 | 链路记账正确（脚本化 SEFA p50 536ms 入 500–800 区间）；**但真实供应商 qwen3.7-flash TTFT p50≈13.8s（TASK-003 bench），真实 SEFA≈14s 远超目标 → DEF-002** |
| 6 | 派蒙人格明显 | ✅（间接） | TASK-009：persona+BehaviorPolicy+结构化输出，test_persona 25 passed，真实 qwen 结构化输出 100% 合法、情绪∈标签集；本验收 scripted 回复维持派蒙口吻 |
| 7 | ≥2 LLM Provider 可替换 | ✅ | LLMProvider 抽象 + OpenAICompatibleLLM（base_url/key/model 可换：百炼 compatible-mode ↔ DeepSeek）；bench 中 deepseek 通道 MISSING_KEY 优雅跳过，接口层面即满足"可替换" |
| 8 | ≥2 TTS Provider 可替换 | ✅ | TASK-008/D-014：FishAudioTTS + BailianCosyVoiceTTS 双 adapter 同一 TTSProvider 契约 |
| 9 | 有基础 latency log | ✅ | 逐轮九时间戳 + SEFA + barge_in_stop + speculative + 分阶段 p50/p95，`data/latency_log/` |

## 4. 缺陷记录（不扩大范围修，见 TASK-012 boundaries）

- **DEF-001（模拟件，中）**：`src/runtime/simulated.py` `WavSinkPlayer` 的
  `_written` 跨 utterance 累计，`stop()` 不归零——`pending_seconds` 含全部
  历史已写音频，`_finish_playback` 排空等待被拉长，PLAYBACK_STOPPED 延迟、
  SPEAKING 状态虚长，后续用户语音被记为 barge-in（soak 中 19/26 轮标
  interrupted 主要由此）。真实 `StreamingPlayer.stop()` 归零 `_played_bytes`
  不受影响；但无声卡降级路径（main.py fallback）会踩到。建议：stop() 清空
  `_written`/`_chunks` 或按 utterance 分段记账。
- **DEF-002（供应商，高）**：真实 LLM（qwen3.7-flash 百炼 compatible-mode）
  TTFT p50 13.8s / total p50 14.0s（`data/llm_benchmark/latest.md`），距
  SEFA 500–800ms 目标区间差距 ~17×。链路层（投机命中、分句、TTS 先行）已无
  明显可压缩空间；瓶颈在供应商 TTFT。方向：换更快模型/供应商、或接受目标
  区间修正（doc 06 §3 注明"非硬承诺，benchmark 后修正"）。
- 备注（非缺陷）：soak 的 B 类停顿在 scripted turn 下判 complete → 提前
  响应被用户续说正确打断并恢复——打断恢复链路再获验证；真实"停顿不抢话"
  依赖 Smart Turn 端点裁决，以用例 B 断言为准。

## 5. 主观评分（doc 06 §5，1–5 分）

脚本化会话无法替代真人体验；以下为 Agent 基于 soak 行为的**初评**，
待人类在真实设备上复评：

| 维度 | 初评 | 依据 |
|------|------|------|
| 抢话自然度 | 4 | B 不抢话、F 不插嘴、E 主动开口受 cooldown 约束 |
| 回应速度 | 3 | 链路 536ms 达标；真实 TTFT ~14s 待解（DEF-002） |
| 被打断反应 | 4 | 即停（0.1ms）、历史截断正确、下轮知情 |
| 派蒙人格稳定 | 4 | 结构化输出+情绪标签稳定（TASK-009 实测 100%） |
| 语言口语感 | 4 | 短句、口语化回复（scripted 样本+真实 LLM 短回复能力已验） |
| 声音好听程度 | — | 脚本化正弦音，无法评价；待真人试听（Fish/百炼赛马见 TASK-008） |
| 是否愿意继续聊 | — | 需真人判断 |

## 6. 结论

- **验收主体通过**：A–G 全过；10min 连续运行稳定；§8 第 1/2/3/4/6/7/8/9
  条满足，第 5 条部分满足（链路达标、真实供应商延迟不达标）。
- 整体判定：**MVP 行为验收 PASS**，遗留 DEF-001（模拟件记账）、
  DEF-002（真实 TTFT 与目标差距）两项缺陷进入后续迭代。
