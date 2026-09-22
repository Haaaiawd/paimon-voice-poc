## 目标
完成 `TASK-003`：LLM 赛马 benchmark（DeepSeek vs 通义千问）。

## 要求
1. 在项目目录内实现/校验 `scripts/bench_llm.py`。
2. 对每个已配置的 OpenAI-compatible endpoint 各跑 >=20 次，输出 TTFT 均值/p50/p95 与结构化输出成功率对比表。
3. 未配置 key 的 endpoint 优雅跳过并标注 MISSING_KEY。
4. 将结果写入 `data/llm_benchmark` 可落盘文件，并把验收证据写入 `.loom/tasks.json` 的 TASK-003 证据字段。

## 边界
- 不做选型结论，只做测量。
- 任一 key 未到位则该家标注 MISSING_KEY。
- 零 key 时停止并报告 blocked。