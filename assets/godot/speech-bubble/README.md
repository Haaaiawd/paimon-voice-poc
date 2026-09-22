# Godot P0 — 角色说话气泡

这套资产为原创 SVG，供本项目的 Godot 实时角色使用。它只包含 P0：气泡面板、三种尾巴与“思考中”圆点；不包含角色贴纸、情绪图标或特效。

## 文件

| 文件 | 用途 |
| --- | --- |
| `paimon-speech-bubble.svg` | 气泡主体，作为 `NinePatchRect` 的 texture |
| `paimon-speech-tail-center.svg` | 居中尾巴 |
| `paimon-speech-tail-left.svg` | 左侧尾巴 |
| `paimon-speech-tail-right.svg` | 右侧尾巴 |
| `paimon-typing-dot.svg` | 思考态圆点；复制 3 个并错开 Tween |

## Godot 4 组装

```text
SpeechBubble (Control / CanvasLayer 子节点)
├─ Panel (NinePatchRect)                 # paimon-speech-bubble.svg
├─ Tail (TextureRect, show_behind_parent) # 三选一；位于 Panel 下沿之后
├─ Text (RichTextLabel)
└─ TypingDots (HBoxContainer)            # 三个 paimon-typing-dot.svg
```

- `Panel` 的 patch margin 设为 **30 px**（四边相同）；最小尺寸建议 `240 × 112 px`。
- 保持尾巴独立：居中尾巴跟随 Panel 中点，左右尾巴分别距左/右边 `48 px`。
- 在 `HeadAnchor` 投影到屏幕后，将容器锚点设为 `(0.5, 1.0)`，并向上偏移 `20–35 px`。
- 字体需由 Godot 项目单独引入。推荐 **Noto Sans SC**（OFL 1.1）；正文 26–32 px，行高 1.25–1.35，文本色 `#403760`。本仓库不复制字体二进制，避免不明授权来源。
- 思考态时显示三个圆点：每个做 `scale 0.78 → 1.0 → 0.78`、`alpha 0.45 → 1.0 → 0.45` 的 0.9 s 循环，依次延迟 0 / 0.15 / 0.30 s。

## 运行时对应

- 第一条 `reply.delta`：显示气泡并逐段更新 `Text`。
- `state = THINKING`：清空文本并显示 `TypingDots`。
- `reply.final`：停止流式光标，保留完整文字。
- `interrupted`：停止文字追加，淡出气泡（150 ms）。

颜色：描边 `#403760`，底色 `#FFFDF8`，阴影/辅色 `#B9A4DC`，点缀 `#F1C4D4`。
