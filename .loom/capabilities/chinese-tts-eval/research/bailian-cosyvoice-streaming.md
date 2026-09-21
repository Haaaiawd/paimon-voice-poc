# 百炼 CosyVoice/Qwen-TTS 流式合成实测

Source: 阿里云百炼文档（CosyVoice WebSocket API / Python SDK / 音色列表）+ 本项目 2026-09-21 实测

协议与 DashScope ASR 同构：同一 `wss://` 端点，run-task → task-started →
continue-task（文本片段，服务端自动分句）→ binary 音频流 → finish-task 强制合成收尾。
官方建议复用 WS 连接跑多个任务（每轮新 task_id）——正好对应我们的常驻连接需求。

实测（本机直连，每次新建 WS）：cosyvoice-v3-flash 首音 626–796ms；
qwen-audio-3.0-tts-flash 首音 516–745ms；连接复用后预期更低。
cosyvoice-v3.5-flash 在本账号持续报引擎错误 418（不可用，选型时避开）。

音色层是亮点：v3 系统音色支持 Instruct 文本指令——情感值
（neutral/fearful/angry/sad/surprised/happy/disgusted）、场景（闲聊互动等）、
角色（傲娇公主等），格式如「你说话的情感是happy。」。我们的 emotion 标签
（neutral/happy/excited/teasing/annoyed/confused/smug/soft）可以映射到这组受控情感 +
prompt 侧措辞。longanhuan_v3（欢脱元气女）与 longhuhu_v3（天真烂漫女童）都贴近派蒙。

关键判断：境内直连路径让百炼 TTS 的热延迟天然优于跨境 Fish；但 Instruct 指令会作为
文本开销随每句发送（几十字），短句场景占比不可忽视——情绪映射层要把指令开销计入。
