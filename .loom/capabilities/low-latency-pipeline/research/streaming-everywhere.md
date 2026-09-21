# 全链路 streaming 与慢轮次兜底

Source: Twig latency budgets、Picovoice ch.3 acknowledgment strategies

让预算可行的不是某个快组件，而是"处处 streaming"：ASR partials 让 final transcript 接近
免费；LLM token 流让 TTS 不用等整段；TTS chunk 流让播放不用等整句。

对真正慢的轮次（工具调用、长推理），修法不是消灭延迟而是不让它沉默——人类也这么干
（"我看看啊"）。acknowledgment 应该是编排策略而非性格脚本：
- 设阈值：固定间隔内没准备好就发短 cue，阈值是 orchestrator 的策略决定，不是 LLM 每轮自决；
- cue 要短且低承诺："我想想"能买几秒，长 filler 段落成本超过收益还招来 barge-in；
- cue 要诚实：绑定真实系统状态（如函数调用在途），空说"在查了"会训练用户不信任；
- 绝不在用户说话时发 cue——cue 填 agent 的沉默，不占用户的轮次；
- 结果落地立刻播答案，从第一个 token 开始播而不是等整段。

对本项目：派蒙默认短回复，慢轮次少；但"哼……让我想想"式 cue 恰好符合人设，
可作为 LLM TTFT 超阈值时的兜底行为，而不是沉默。
