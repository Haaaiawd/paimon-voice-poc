# Paimon Voice PoC Docs

这是 `Paimon Voice PoC` 的项目理解与交接文档集。

请从：

**`00_READ_ME_FIRST.md`**

开始阅读。

当前阶段目标：

> 在 PC 上完成一个低延迟、可打断、具有自然轮次和主动性的“派蒙实时语音对话”原型。

当前不包含：

- 3D
- X4 Air
- 手机端
- 直播

文档版本：2026-09-21

## 开发环境

```bash
git submodule update --init --depth 1   # 拉 reference/ 参考源码
pip install -e ".[dev]"                 # Python >=3.12，Windows 原生
cp .env.example .env                    # 填入 API keys
```

`.env` 不入库；换机器时手动拷贝。本机有代理的话记得给 `*.aliyuncs.com` /
`api.fish.audio` 配 DIRECT 规则（详见 `.env.example` 尾部注释）。

项目状态与任务图见 `.loom/`（loom CLI）。
