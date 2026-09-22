# 记忆系统设计评估：Memsearch 接入与 Demo 假记忆方案

- Kind: evaluation（评估文档，非规范设计）
- Status: draft-for-decision —— 本阶段只出方案，不接真实记忆系统；接入时机见 §6
- Date: 2026-09-22
- 评估对象：`memsearch`（zilliztech/memsearch，MIT，Python >=3.10，PyPI 最新 0.4.x）
- 依据：memsearch 官方文档与源码（§7 来源）、本仓库代码与 LOOM 决策记录；
  未实测的数据均标注"待验证"

## TL;DR

Memsearch 是"markdown 文件为真相源 + Milvus 派生向量索引"的语义记忆库：
**写记忆 = 写 .md 文件，检索 = hybrid search（dense + BM25 + RRF），遗忘 = 删文件后 reindex**。
它解决的是"跨会话语义检索"这一个点，不提供记忆分型、写入时机、遗忘策略——
这些策略层恰好是本项目的核心资产逻辑，必须自研（与 Conversation Core 同构的权责划分）。

**对本项目的三个硬事实**：

1. **Milvus Lite 不支持 Windows**（官方明示 Linux/macOS only）——与 D-011
  "Windows 原生"冲突，接入要么 Docker Milvus Server、要么 WSL、要么 Zilliz Cloud，
  都引入新运维面；
2. **单人 Companion 的记忆量极小**（数百条量级），向量库在这个规模下收益有限，
  检索延迟反而进关键路径（ONNX bge-m3 CPU embed ~50–150ms/次，待实测）；
3. **Demo 不需要它**——假记忆用一个 JSON/md 文件 + prompt 注入即可，且这个 mock
  正好就是未来 `MemoryProvider` 的参考实现。

**推荐**：现在只做 §4 的 Demo 假记忆（方案 B：文件后端 + prompt 注入，同时充当
未来 MemoryProvider 的 mock）；真实接入留待 §6 的触发条件出现。届时 Memsearch 是
合格的后端候选，但不是唯一选项——小数据量下"本地 embedding + 暴力余弦"同样成立。

---

## 1. Memsearch 能力与限制

### 1.1 它是什么

面向 AI Agent（原版定位是 coding agent：Claude Code / Codex / OpenCode 等）的
跨会话语义记忆层。核心信条：**markdown 文件是唯一的真相源，向量索引是可重建的
派生缓存**（灵感来自 OpenClaw）。记忆文件人类可读、可 git 版本化、可手工编辑；
Milvus 丢了 `memsearch index` 一条命令全量重建。

### 1.2 能做什么

| 能力 | 事实 |
|------|------|
| 写入 | 没有 `add()` API——**直接写 .md 文件**，`index()`/`index_file()`/`watch()` 把它索引进向量库 |
| 检索 | `search(query, top_k)` → chunk dicts（content/source/heading/score）；hybrid = dense 向量 + BM25 sparse（Milvus 自动生成稀疏向量）+ RRF(k=60) 融合排序 |
| 去重/增量 | SHA-256 内容哈希，未变 chunk 跳过；删掉的文件/段落下次 `index()` 自动清出索引 |
| 实时同步 | `watch()` 后台线程 file watcher，debounce 后自动增量索引 |
| 压缩/巩固 | `compact()` —— 用 LLM（openai/anthropic/gemini，可换 base_url 指向兼容端点）把 chunk 压缩成摘要写回 `memory/YYYY-MM-DD.md` 并自动索引。这是"consolidate"的对应物 |
| 分层召回 | L1 search（chunk 摘要）→ L2 expand（完整小节）→ L3 transcript（原始对话），成本递进 |
| Embedding | provider 可选 openai/google/voyage/jina/mistral/ollama/local/**onnx**；onnx = 本地 CPU 跑 bge-m3，**不需要 API key**（插件默认项，中英双语 benchmark 选型结果） |
| 多用户隔离 | `paths`/`collection`/`milvus_uri` 三维隔离；本项目单用户用不上 |

### 1.3 不能做什么（对本项目重要的缺口）

- **没有记忆类型系统**：不区分事实/经验/偏好，只有"markdown 里的 chunk"。
  记忆分型（§2.2）是我们自己在 .md 文件组织方式上建的约定。
- **没有 `forget()`**：遗忘 = 编辑/删除 .md 文件 + reindex。没有 TTL、没有
  重要性衰减、没有"用户说忘掉 X"的语义接口——遗忘策略（§2.5）要自研。
- **没有写入时机判断**：不会自己决定"这句话该不该记"。插件体系里是各平台
  hook（session stop 时抓 transcript）；我们要自己定义语音场景的写入点（§2.3）。
- **不是为语音场景设计的**：面向 coding agent 的 transcript 索引。单人
  Companion 的记忆形态（事实+关系状态）比"会话记录搜索"小得多也结构化得多。
- **不支持结构化/关系查询**：纯语义检索，没有"取出所有事实型记忆"以外的
  强类型保证（只能靠文件组织 + BM25 关键词兜底）。

### 1.4 与 Milvus 的关系——不必须，但有平台坑

`milvus_uri` 三档：

| 形态 | URI | 依赖 | 本项目适配 |
|------|-----|------|-----------|
| Milvus Lite | 本地 `*.db` 路径 | `pymilvus` 内置，无服务进程 | ❌ **官方明示 Linux/macOS only**；D-011 定死 Windows 原生，WSL 已被否决 → 主路径不可用 |
| Milvus Server | `http://host:port` | Docker Compose 起 standalone（etcd+minio+milvus 三容器） | ⚠️ 可用但重：为一个几百条的记忆库起三个容器 |
| Zilliz Cloud | `https://*.zillizcloud.com` | SaaS，免费层存在 | ⚠️ 本地 Companion 引入外部网络依赖 + 隐私面（用户语音记忆出本机）+ Clash 代理坑（F-030 同款） |

**诚实结论**：在 Windows 原生约束下，"用 Memsearch"实际等于"用 Memsearch +
Milvus Server（Docker）或 Zilliz Cloud"。这不是不能接受的方案，但使它的对手
变成"自存 JSON/md + 本地 ONNX embedding + numpy 暴力余弦"——后者在数百条规模下
检索质量差距很小、零新增服务、Windows 原生可跑，且 embedding 还能复用
memsearch 同款 bge-m3。选型应在接入时点按当时记忆规模实测裁决（§6）。

### 1.5 安装/运行成本

- `pip install memsearch`：拉 `pymilvus` + embedding 客户端；`memsearch[onnx]`
  额外拉 onnxruntime + 首次运行下载 bge-m3 模型（数百 MB–~2GB，视量化版本，待实测）。
- 常驻内存：ONNX bge-m3 常驻 ~500MB 量级（待实测）；Milvus Lite 嵌入式零进程；
  Milvus Server 三容器 ~1GB+。
- 检索延迟构成 = embed query（ONNX CPU 估 50–150ms）+ Milvus 查询（小库 <10ms）。
  **串行放关键路径会吃掉 SEFA 预算的 10–30%**——必须走投机并行（§2.4）。
- 部署：Python 库内嵌即可，无独立服务（Lite 形态）；CLI/`uv tool` 形态对本项目无意义。

### 1.6 API 概览（对应任务的 add/search/consolidate/forget）

```python
mem = MemSearch(paths=["./memory"], embedding_provider="onnx",
                milvus_uri="./data/memory.db")   # Lite；Server/Cloud 换 URI
await mem.index()            # 全量/增量索引（add 的真身：先写 .md 再 index）
await mem.index_file(p)      # 单文件索引
await mem.search(q, top_k=5) # → [{content, source, heading, score, ...}]
await mem.compact(source=…)  # LLM 压缩巩固 → 写回当日 .md（consolidate）
watcher = mem.watch()        # 后台 file watcher 自动索引
# forget = 删/改 .md 文件 → index() 自动清 stale chunk
```

### 1.7 与 Provider 抽象层的关系

**不塞进 LLMProvider**。LLM 的职责边界是"轮到派蒙时说什么"
（`src/providers/llm/base.py` docstring），记忆是独立的存储/检索域。
与现有三接口同构的做法是新增第四类抽象：

```python
class MemoryProvider(ABC):
    async def recall(self, query: str, *, top_k: int = 5) -> list[MemoryItem] ...
    async def remember(self, entry: MemoryEntry) -> None ...   # 内部=写文件+index
    async def forget(self, memory_id: str) -> None ...          # 删文件/段+reindex
    async def close(self) -> None ...
```

放 `src/providers/memory/`（与 D-004"供应商赛马可替换"同思想：memsearch /
json-mock / 暴力余弦自研 三实现可换）。**何时 recall、何时 remember、注入什么进
prompt 是策略层**——归 `src/conversation/`（核心资产侧），不归 provider。
这与"ASR 只产信号、轮次裁决归 Core"（streaming-asr-zh C3）是同一权责形状。

---

## 2. 记忆系统设计

### 2.1 记忆分层

| 层 | 范围 | 载体 | 现状 |
|----|------|------|------|
| 短期（对话内） | 当前 session 的轮次 | ContextManager 双历史（heard/logical，`history_limit=20`） | **已建成**（TASK-006），不需要新系统 |
| 中期（会话间） | 上一次/近几次会话 | 会话结束时的 session summary（一段 markdown） | 缺口——Demo 价值最高的一层 |
| 长期（跨会话） | 持久事实、偏好、关系状态 | 结构化记忆条目（未来：memsearch 索引的 .md 库） | 缺口——接入时再做 |

分层与 memsearch 对齐：中期/长期都是"写在 memory/ 下的 .md"，区别只是文件组织
（`sessions/2026-09-22.md` vs `facts/user.md`）与检索优先级。短期层永不出
ContextManager——**不要把 in-conversation 历史塞进向量库**，那会同时污染
heard-history 精度（turn-taking C5）和检索质量。

### 2.2 记忆类型

| 类型 | 内容 | 例子 | 建议载体 |
|------|------|------|---------|
| 事实型 | 用户偏好、身份、项目背景等稳定事实 | "用户在调 paimon-voice-poc"、"用户不喜欢被叫'您'" | `memory/facts/*.md`，一条一节 |
| 经验型 | 上次会话结论、未完成的梗、失败教训 | "昨天争论 SEFA 目标没谈拢"、"上次那个史莱姆梗用户接了" | `memory/sessions/YYYY-MM-DD.md`，会话摘要 |
| 人格型 | 派蒙侧的行为/情绪状态：记仇计数、关系热度、最近被怼没赢回来 | "grudge=2（上轮被说'烦'还没找回场子）" | `memory/paimon_state.md`，小 JSON/YAML frontmatter |

人格型是这个项目最有差异化的一层：它不进语义检索主路，而是**每轮必注入**的
小型状态块（见 §2.4）——"记仇几轮"（doc 05 §4）天然需要跨轮状态，这正是
纯 prompt 人设做不到的事。

### 2.3 写入时机

按实现成本递增，第一版只取前两条：

1. **会话结束批量写（推荐 v1）**：session 结束时用现有 LLMProvider 跑一次
   摘要（"把这次对话浓缩成经验型记忆"），写 `memory/sessions/当日.md`。
   零关键路径延迟，确定性可测。对应 memsearch 的 `compact()` 思路。
2. **显式指令写**：用户说"记住 XXX"/"别记这个"——复用 D-013 同款规则分类器
   （ASR_FINAL 关键词匹配，不走 LLM），命中即写/删 facts。演示效果好
   （"派蒙，记住我明天要答辩"），实现便宜。
3. **AI 自动判断重要性（v2，暂缓）**：每轮后台跑"该轮是否含可记事实"的抽取
   调用。成本高（每轮多一次 LLM）、误记污染检索——等记忆规模上来再做，
   届时可用便宜模型分级。

### 2.4 检索时机

| 时机 | 机制 | 延迟影响 | 建议 |
|------|------|---------|------|
| 会话开始 | 注入上次 session summary + 全部 facts（小数据量下全量注入，不检索） | 零（启动时） | **v1 必做**——"一开口就记得"是记忆体验的最强信号 |
| 话题相关时 | ASR partial 到达 → 并行 embed+search（投机），turn_complete 时结果已就绪进 prompt | 投机路径内 ≈0；未命中可弃 | v2 做；挂在 turn-taking C6 的投机点，与 prompt 预构造同缝 |
| 人格判断需要时 | `paimon_state` 小状态块每轮进 system prompt/行为约束槽位 | 零（纯本地读） | v1 做——成本只是一段文本 |

关键纪律：**记忆检索永不串行插在 turn_complete→LLM 请求之间**。要么启动时
全量注入，要么 partial 阶段投机并行——与投机 prompt 预构造（TASK-010 验收第 3 条）
共享同一时机与丢弃语义。

### 2.5 遗忘机制（TBD，先立边界）

memsearch 层只有"删文件+reindex"原语，策略要自定：

- **显式遗忘**："忘掉 XXX"→ 规则分类器命中 → 删对应 .md 节 → reindex。v1 做。
- **经验型过期**：session 摘要可带 `expires:` frontmatter，超过 N 天/被新摘要
  取代即清。v2。
- **人格型衰减**：grudge/energy 等状态自带半衰期（如每 session 衰减），
  写入时按规则衰减——这是"记仇几轮就翻篇"（doc 05 §4）的机制化。
- **不做**：自动遗忘模型、重要性打分淘汰——过度设计，等真实记忆规模说话。
- 兜底事实：markdown 真相源意味着**手工编辑 memory/ 目录永远合法**——
  这是 memsearch 选型附赠的调试红利。

---

## 3. 与现有架构的关系

### 3.1 记忆系统属于哪个模块

按现有权责形状拆两半：

- **策略层（核心资产侧）**：`src/conversation/` 内。记忆写入时机、检索时机、
  注入 agent_input 的位置、人格状态——与 TurnManager/ContextManager 同层，
  理由同"轮次裁决归 Core"：这是定义产品行为的逻辑，供应商可换它不能换。
- **存储/检索层（provider 侧）**：`src/providers/memory/`。Memsearch adapter /
  JSON mock / 未来暴力余弦实现，全部实现同一 `MemoryProvider` 接口。
  STRUCTURE.md 的"业务层不直接 import 供应商 SDK"规则原样适用
  （`import memsearch` 只允许出现在 adapter 内）。

`agent_input`（doc 03 §5）加两个可选字段即可承载全部注入：

```json
{
  "memory_context": ["上次会话：争论 SEFA 没谈拢", "用户不喜欢被称'您'"],
  "paimon_state": {"grudge": 2, "last_session": "2026-09-21"}
}
```

`build_messages`/`build_system_prompt`（`src/character/prompt.py`）渲染时
加一个"记忆"槽位——与现有六槽位（身份/语气/长度/输出契约/打断/主动许可）
同一机制，prompt 原则（doc 05 §10）不破。

### 3.2 对 TASK-009 / 010 / 011 / 012 的影响

| Task | 影响 | 结论 |
|------|------|------|
| TASK-009（persona，active） | `memory_context`/`paimon_state` 只是 prompt 多一个槽位；验收两条（schema 稳定、prompt 六槽位原则）均不含记忆 | **零阻塞，照常做**。若想把"记忆槽位"写进 prompt 模板可顺手加，但不是验收项 |
| TASK-010（pipeline 组装） | 投机记忆检索挂在 ASR partial→prompt 预构造的同一缝（验收第 3 条已定义该时机）；metrics 可加 `t_memory_recall` 但非必须 | **零返工**。现在不做，未来插入点已天然存在 |
| TASK-011（InitiativePolicy） | 记忆可喂养 `interesting_context` 评分（"记得一个相关的梗"），属可选增强 | **零阻塞**。主动性第一版规则不依赖记忆 |
| TASK-012（MVP 验收） | 用例 A–G 与 ≥10min 连续对话均无记忆要求；doc 01 §2.2 明示"记住几天前的聊天"第一阶段暂不要求 | **无影响** |

### 3.3 现在不改代码是否可行——可行，且是正确顺序

doc 01 §3 的问题优先级把 Memory 排在第 6 位（Turn-taking > Barge-in > Latency >
Persona > Initiative > Memory），§2.2 明确"记住几天前的聊天"第一阶段暂不要求。
**当前路线图原样成立**：记忆唯一的预埋件是"将来 agent_input 加两个可选字段、
prompt 加一槽"——向后兼容的纯增量，不存在"现在不做以后要大改"的结构性欠债。

唯一建议的路线图动作（**不需要现在执行**，记录备查）：未来新增
`TASK-013: MemoryProvider 抽象 + JSON mock adapter + 注入缝`，
`TASK-014: 会话摘要写入 + 显式记住/忘掉`，`TASK-015: memsearch adapter 实测选型`。
Demo 假记忆若做，可作为 TASK-013 的提前量或独立小任务。

---

## 4. Demo 方案（假记忆）

目标：观众看到"派蒙记得上次的事"，后台没有向量库。三方案对比：

| 方案 | 机制 | 能演示 | 不能演示 | 成本 |
|------|------|--------|---------|------|
| A 硬编码 | 代码里写死几条记忆字符串，固定触发 | 剧本化 demo | 写入、重启延续、自由发挥 | 最低 |
| B 文件后端 | `data/demo_memory.json`（或 .md），启动读入注入 prompt，"记住 X"写文件 | **读+写全链路**：说"记住"→落盘→重启后派蒙开口就提 | 语义检索（只能关键词/全量注入） | 低（一个文件 + 一个注入点） |
| C prompt 注入 | system prompt 里塞一段伪记忆文本 | 最便宜的"看起来记得" | 写入路径、跨重启 | 最低 |

**推荐方案 B**，理由三条：

1. Demo 的杀伤力在**写入侧**——"派蒙，记住我明天要给老板演示"→（重启程序）→
   "欸，老板演示准备得怎么样了？"这一下比任何检索精度都有说服力。只有 B 能演。
2. B 的实现形状就是 §3.1 的 `MemoryProvider` mock：今天写的 demo_memory.json
   读写代码，未来就是 `JsonMemoryProvider` 参考实现，**Demo 代码不浪费**。
3. B 机械上就是 C（把文件内容注入 prompt）+ 一个写入路径——成本只差一个
   几十行的文件读写函数和一条"记住"关键词规则（D-013 分类器同款）。

A 只推荐作为 B 的兜底（demo 前夜发现写入路径有 bug 时降级为纯剧本）。
C 单独用没有存在理由——它是 B 的注入步骤。

### Demo 交互设计（方案 B 话术脚本）

```text
[启动] 派蒙（soft）：……哦，是你。上次你说的那个语音 pipeline，调通了没？

用户：调通了，你看我们现在不是在聊吗。
派蒙（smug）：哼，勉强算你厉害。对了——你上次说要少喝咖啡，做到了吗？

用户：派蒙，记住我周五要给老板演示这个。
派蒙（excited）：记住了记住了！周五，老板，演示——搞砸了可别说是我做的。

[Ctrl+C，重启]

[启动] 派蒙（neutral）：周五了。给老板演示是吧？……别紧张，有我罩你。
```

实现要点（设计层面，非实现指令）：
- `demo_memory.json` 结构：`{"facts": [...], "sessions": [{"date": "...", "summary": "..."}], "paimon_state": {"grudge": 0}}`；
- 注入位置 = `agent_input["memory_context"]`（§3.1 同一字段，未来无缝换真）；
- "记住 X" 触发 = ASR_FINAL 规则匹配（"记住/别忘了"），写文件 + 让派蒙口头确认；
- 防穿帮纪律：假记忆条目**写成派蒙语气的引用**（"用户上次说在调 pipeline"），
  不要写成百科式陈述——观众听到的是"派蒙记得"，不是"系统查到了"。

---

## 5. 实际接入路线图（未来）

### 5.1 需要改/新增的文件

| 文件 | 动作 |
|------|------|
| `src/providers/memory/base.py` | 新增：`MemoryProvider` ABC + `MemoryItem`/`MemoryEntry` 类型 |
| `src/providers/memory/json_memory.py` | 新增：JSON 文件实现（Demo mock 转正） |
| `src/providers/memory/memsearch_adapter.py` | 新增：memsearch 实现（写 .md + index/search/compact） |
| `src/conversation/context.py` | 改：`build_agent_input` 加 `memory_context`/`paimon_state` 字段 |
| `src/conversation/memory_policy.py` | 新增：写入时机（session-end summary、显式指令）、注入组装 |
| `src/character/prompt.py` | 改：system prompt 加"记忆"槽位渲染 |
| `src/runtime/main.py` | 改：session start 加载注入、session end 触发摘要写 |
| `pyproject.toml` / `.env.example` | 改：`memsearch` 依赖（仅走该后端时）+ embedding/Milvus 配置项 |
| `tests/` | 新增：mock 驱动的写入/召回/遗忘测试（镜像现有 provider 测试形状） |

### 5.2 新增接口

`MemoryProvider`（§1.6 已给形状）；域层加两个事件可选：`SESSION_SUMMARIZED`、
`MEMORY_WRITTEN`（挂进现有 EventBus 事件面，doc 03 §4 风格延续）。

### 5.3 从 mock 迁移到真实记忆

markdown 真相源让迁移接近零成本：`demo_memory.json` → 一个转换脚本（或直接
把 mock 实现改成写 .md）→ `memsearch.index()` 全量重建。结构化字段
（type/expires）用 frontmatter 承载，memsearch chunking 原样兼容。
**真正要迁移的不是数据，是检索调用从"全量注入"换成"语义 search"**——
小数据量下甚至可保留全量注入，先吃"写入+持久化"的价值。

### 5.4 预估工程量（Devin 单任务口径）

| 项 | 量 |
|----|----|
| MemoryProvider 抽象 + JSON 实现 + agent_input/prompt 注入缝 | ≈1 个 Task（TASK-002 级） |
| Session 摘要写入 + 显式记住/忘掉规则分类器 | ≈0.5–1 个 Task |
| Memsearch adapter + Milvus Server（Docker）联调 + 与暴力余弦基线赛马 | ≈1–1.5 个 Task（含 Windows 部署坑的不可预见量） |
| 遗忘策略（TTL/衰减） | ≈0.5 个 Task，可缓 |
| **合计** | **≈3–4 个 Task**；其中前两项（JSON 后端全链路）独立可用、不依赖 memsearch |

---

## 6. 决策建议

### 现在（本阶段）

**不做真实记忆，只做 §4 方案 B 的 Demo 假记忆。**依据：doc 01 §2.2/§3 把记忆
排在末位且明示第一阶段不要求；当前阻塞项是 SEFA 与轮次质量（TASK-003/009–012），
记忆不改变这些验收；memsearch 在 Windows 原生约束下有真实部署成本，不值得为
Demo 承担。

### 什么时候该接真实记忆系统

按优先级排序的触发条件（命中任一即启动 §5 前两档，命中第 3 条才评估 memsearch）：

1. **演示/体验反馈说"记忆"是卖点**：demo 后"它居然记得"被证明是留存/惊叹的
   主驱动 → 先做 JSON 后端的全链路（持久化+写入+注入），此时还不需要向量检索；
2. **多会话成为真实使用形态**：开发者本人开始每天跟它聊，`history_limit=20`
   之外的 continuity 诉求真实出现 → 同上，JSON/md 全量注入仍够用；
3. **记忆规模或检索需求真实超过全量注入**：条目数百+、注入 token 成本可感、
   或出现"这个话题我们以前聊过吗"式语义召回需求 → 此时才进入后端选型：
   memsearch（+Milvus Server/Zilliz Cloud）vs 本地 bge-m3 + 暴力余弦，赛马裁决；
4. **进入 Phase 2+（手机/直播）**：记忆成为跨端产品承诺 → 直接评估带服务端的
   方案（Milvus Server / Zilliz Cloud），本地文件方案出局。

反向触发（不要接）：仅因为"memsearch 批准备选了"就提前接入——备选批准的是
**方向**，不是排期。

---

## 7. 来源

- memsearch 仓库与文档：`github.com/zilliztech/memsearch`（README、core.py）、
  `zilliztech.github.io/memsearch/`（Python API：构造参数/milvus_uri 三形态/
  index/index_file/search/compact/watch/close、per-user isolation；
  Design Philosophy：markdown 真相源、hybrid search、L1–L3 渐进召回、why Milvus；
  Claude Code plugin README：ONNX bge-m3 默认、无需 key）
- PyPI：`memsearch` 0.4.17，MIT，Python >=3.10
- 本仓库：`01_PRODUCT_SCOPE.md` §2.2/§3（记忆第一阶段暂不要求、优先级第6）、
  `02/03/05/06` 规范文档、`.loom/DECISIONS.md`（D-011 Windows 原生、D-013 规则分类器、
  D-004 provider 赛马思想）、`.loom/tasks.json` TASK-009–012、
  `src/providers/{asr,llm,tts}/base.py`、`src/conversation/context.py`、
  `src/character/prompt.py`、`.loom/design/REALTIME_PLUS_EVALUATION.md`（评估文档格式先例）

## 8. 诚实记录（与直觉/备选预期的出入）

- **"Memsearch = 记忆系统"不准确**：它是"markdown 记忆库的索引/检索引擎"。
  记忆分型、写入时机、遗忘策略、人格状态——本项目需要的记忆设计大头全部要自研；
  memsearch 只覆盖其中"检索"一个环节。选型权重应给策略层设计，后端可换。
- **"本地文件后端可用"有条件**：Milvus Lite 这个本地形态在 Windows 不可用
  （官方 Linux/macOS only）。Windows 下"本地"只剩 Docker Milvus Server 或
  云端——"本地文件"四个字掩盖了一个平台坑。
- **"需要向量检索"在本项目规模下是假设而非事实**：单人 Companion 的持久记忆
  大概率长期停在数百条，全量注入/关键词匹配的体感差距很小；接入时点用真实
  记忆规模裁决，不要为想象中的规模预付 Milvus 运维成本。
- **Demo 假记忆不是技术债**：方案 B 的 JSON 文件读写按 MemoryProvider 形状写，
  就是未来接入时的 mock 实现与迁移数据源——本条已尽量让 Demo 投入沉淀为资产。
