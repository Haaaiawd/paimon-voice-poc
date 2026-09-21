# Smart Turn 中文准确率短板

Source: pipecat-ai/smart-turn 官方 benchmark（repo docs / HuggingFace model card，v3.2）

Smart Turn 官方 benchmark 中中文表现垫底：准确率 85.79%、FNR（漏报率）9.26%，
在支持的 23 种语言中倒数第三。

对本项目的直接含义：
- 约 9% 的中文轮次会判 incomplete 后落入 SmartTurnParams.stop_secs（默认 3s）静音兜底——
  SEFA 的 p95 将由兜底主导而非模型判定速度，长尾体感是"偶尔反应慢一拍"；
- 调试时无法区分"模型对中文的短板"与"接线错误"——除非 metrics 单独记录
  每次 complete 的来源（model vs fallback）；
- 对策空间：fallback 率是内置观测点；若中文误判集中且影响体验，可收紧兜底、
  叠加 FilterIncomplete（LLM 语义判完整）做第二层、或后续用中文语料微调。
  第一版先观测 fallback 率再决定，不预防性堆复杂度。
