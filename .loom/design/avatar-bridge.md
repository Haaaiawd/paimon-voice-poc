# 派蒙 × 虚拟形象衔接（Avatar Bridge）

- Kind: experience
- Status: draft
- Branch: `feat/avatar-bridge`

## Responsibility in the whole

派蒙从"纯语音"升级为"有身体的旅行精灵"：伏肩、落手、围观拍照、
入镜合影。Conversation Core 保持纯净——**她决定做什么（动作/
用工具），虚拟形象项目负责怎么演**。衔接面 = 动作事件出 +
感官结果进，两条通道。

## Inputs, outputs, and boundaries

### 出（Agent → 世界）：`action` 字段

结构化输出契约扩展一个字段——与 speech/followup 同级，
模型每轮显式决定"要不要做点什么"：

```json
{"speech": "...", "followup": "...",
 "action": {"kind": "gesture|camera", "name": "...", "args": {}}}
```

动作清单（首版，名字即协议）：

| action | 语义 | 谁执行 |
|---|---|---|
| `gesture.perch_shoulder` | 伏到肩上 | avatar runtime |
| `gesture.land_hand` | 落到手上（配"伸出手来！"） | avatar runtime |
| `gesture.peek` | 探脑袋围观 | avatar runtime |
| `gesture.point` | 指向景物 | avatar runtime |
| `gesture.hide` | 躲起来 | avatar runtime |
| `camera.snapshot` | 抓一帧给她"看"（vision 输入） | 前端 getUserMedia |
| `camera.photo` | 拍照 → 发回给用户 | 前端 |
| `camera.record.start` / `camera.record.stop` | 录像 | 前端 |
| `none` / 缺省 | 不做动作 | — |

动作是**即发即忘事件**，不阻塞 speech——她一边说"别动别动！"
一边 `camera.photo`，拍照和说话并行。

### 进（世界 → Agent）：感官结果回流

- `camera.snapshot` 结果：一帧 JPEG → vision model（qwen-vl
  系列）→ 描述文本进当前轮上下文 → 她开口评论
  （"哇这个就是鸡鸣寺？"）。异步——拍照指令发出后她先说话，
  描述回来后补一句观感。
- `camera.photo` 结果：照片本体 → WS 二进制帧下行 → 前端渲染
  为图片气泡 + 可保存。不需要 vision（拍照是为了给用户，不是看）。
- `camera.record` 结果：录制状态/文件路径 → 气泡通知。

## Components and control flow

```
LLM ──stream──> extractor(speech) ──> chunker ──> TTS ──> 播放
          └───> extractor(followup) ─┘（第二气泡）
          └───> extractor(action) ──> AGENT_ACTION 事件
                                         │
                        ┌────────────────┼─────────────────┐
                   gesture.*        camera.snapshot    camera.photo
                   (ws action帧      (ws 相机指令)       (ws 相机指令)
                   → avatar 演)          │                    │
                                   帧上行 → vision        照片下行
                                   → 描述进上下文           → 图片气泡
                                   → "补一句观感"轮
```

事件类型新增（事件驱动原则不动）：

- `AGENT_ACTION`（payload: kind/name/args/turn_id）——动作派发
- `CAMERA_FRAME`（payload: jpeg bytes + turn_id）——帧上行
- `VISION_RESULT`（payload: text）——vision 描述回流
- `PHOTO_READY`（payload: bytes/path）——照片交付

WS 帧契约（§4.3 扩展，向前兼容——老前端忽略未知 type）：

- 下行：`action.gesture {name}`、`camera.cmd {op: snapshot|photo|record.start|record.stop}`、
  `photo {bytes, mime}`（头帧+二进制，同 audio.chunk 双帧制）
- 上行：`camera.frame {bytes}`（client → server，snapshot 回执）

## Data and state

- `AgentReply.action: dict | None`——契约字段，parse 容错
  （非 dict → None，不判垃圾）。
- vision 描述走**记忆槽位语义之外的第三条路**：不常驻、不进
  heard_history，作为下一轮 turn 输入的 `scene:` 行
  （"眼前看到的：…"）——像 interruption_context 一样是一过性
  上下文。

## Interfaces and dependencies

- **Avatar runtime 是消费者**：gesture 帧谁订阅谁演——本期前端
  先用占位实现（状态栏图标/文字提示），虚拟形象项目接入时
  订阅同一条事件流，零改动切换。
- **相机是前端工具**：getUserMedia 已可用于 MicCapture 同源
  权限模型；帧上行复用现有二进制上行通道。
- **vision provider 可换**：`VisionProvider` 抽象（frame → text），
  首版 DashScope qwen-vl，同 LLM 赛马哲学。

## Failure, safety, and recovery

- action 必须可取消吗？——即发即忘、不带生命周期；唯一例外
  `camera.record` 有 start/stop 配对，打断不该掐断录像
  （record 是用户资产不是她的输出）。
- vision 失败 → scene 行缺省，她不知道看到了什么——静默降级，
  不挡对话。
- 用户没给相机权限 → camera.* 全部静默降级为"她以为拍了"。
- **打断与动作正交**：打断掐的是 speech/playback，动作已派发
  不撤回（她说了"伸出手"你还没伸，话被打断了——动作线索
  留在上下文里，她下轮还记得自己说过）。

## MVP slices（本分支建议顺序）

1. **契约 + 事件**：`action` 字段、AGENT_ACTION、gesture 帧
   下行 + 前端占位渲染——"她说'伸出手'时 avatar 侧收到
   land_hand"。最小闭环，无相机。
2. **camera.photo**：拍照 → 图片气泡。纯下行资产交付。
3. **camera.snapshot + vision**：她"看一眼"——异步感官回流，
   scene 行进下一轮上下文。延迟容忍度高（1–2s 视觉描述）。
4. `camera.record`：录像配对指令。

## Open questions（接虚拟形象项目时对齐）

- avatar runtime 跑在哪：同前端页内嵌？独立 app 订阅 WS？
  两条都行，事件流不变。
- gesture 是否要有"进行中"状态（伏肩上之后一直伏着 vs 一次性
  动作）——首版全部当一次性瞬态事件，持续姿态由 avatar 侧
  自己维持 idle 状态机。
- 合影模式（她入镜）是 gesture 的一种还是独立模式——建议
  `gesture.pose_for_photo`，语义上是"摆姿势等拍"。

## Related documents and capabilities

- `03_CONVERSATION_CORE.md`（结构化输出契约 §6——action 字段扩展处）
- `paimon-persona.md`（主动开口/推进槽位——动作的触发面）
- FRONTEND_DEMO_DESIGN.md §4（WS 帧契约扩展处）
