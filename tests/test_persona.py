"""TASK-009 acceptance：结构化输出 schema/情绪标签合法性 + system prompt 槽位审查。

verify_by:
- pytest tests/test_persona.py 断言 schema 与标签合法性
  （MockTransport 覆盖解析路径；TestLiveQwen 在 DASHSCOPE_API_KEY 存在时
  打真实百炼 compatible-mode 端点验证"给定 context 稳定输出 schema JSON"）；
- system prompt 只含身份/语气/长度/行为限制——TestSystemPrompt 逐槽位断言，
  对照 doc 05 §10。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from dotenv import dotenv_values

from character import (
    EMOTION_TAG_ORDER,
    NOOP_REPLY,
    PAIMON,
    BehaviorConstraints,
    BehaviorPolicy,
    CharacterAgent,
    build_messages,
    build_system_prompt,
    is_noop,
    render_turn_input,
)
from providers.llm import (
    EMOTION_TAGS,
    AgentReply,
    LLMError,
    OpenAICompatibleLLM,
)

BASE_URL = "http://mock-llm/v1"

DOC05_TAGS = {
    "neutral",
    "happy",
    "excited",
    "teasing",
    "annoyed",
    "confused",
    "smug",
    "soft",
}

NORMAL_INPUT = {
    "character": "paimon",
    "state": "THINKING",
    "last_user_text": "你觉得今天吃什么？",
    "recent_heard_history": [],
    "interruption_context": None,
    "silence_duration_ms": 0,
    "initiative_reason": None,
    "behavior_constraints": {},
}


def sse_body(tokens: list[str]) -> bytes:
    lines = [
        f"data: {json.dumps({'choices': [{'delta': {'content': t}}]}, ensure_ascii=False)}"
        for t in tokens
    ]
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode()


def reply_sse(reply: dict) -> bytes:
    text = json.dumps(reply, ensure_ascii=False)
    third = max(1, len(text) // 3)
    return sse_body([text[:third], text[third : 2 * third], text[2 * third :]])


def make_agent(handler, **kwargs) -> CharacterAgent:
    llm = OpenAICompatibleLLM(
        base_url=BASE_URL,
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )
    return CharacterAgent(llm)


class TestEmotionTags:
    def test_tag_set_matches_doc05_section8(self):
        """doc 05 §8 的 8 个标签 = persona 集 = LLM 输出合法域。"""
        assert set(EMOTION_TAG_ORDER) == DOC05_TAGS
        assert PAIMON.emotion_tags == EMOTION_TAGS == frozenset(DOC05_TAGS)


class TestSystemPrompt:
    """acceptance 2：prompt 只含分层槽位（stable core/环境适配/本轮限制），
    不写角色小说。"""

    #: 允许出现的槽位行前缀（身份/语气/记忆/语音/长度/输出契约/行为限制）。
    SLOT_PREFIXES = (
        "你是",
        "语气：",
        "记忆：",
        "语音：",
        "长度：",
        "只输出",
        "你上一句被打断了",
        "这次是你主动开口",
        "用户要求你安静",
    )

    def test_only_allowed_slots(self):
        """每一行都属于六个槽位之一——没有规则行以外的叙事内容。"""
        constraints = BehaviorConstraints(
            max_sentences=2, was_interrupted=True, is_initiative=True
        )
        prompt = build_system_prompt(PAIMON, constraints)
        for line in prompt.splitlines():
            assert line.startswith(self.SLOT_PREFIXES), f"非槽位行: {line}"

    def test_compact_not_a_character_novel(self):
        """doc 05 §10：不写几千字角色小说——整体短、行数少、无叙事段落。"""
        prompt = build_system_prompt(
            PAIMON,
            BehaviorConstraints(
                max_sentences=2,
                was_interrupted=True,
                is_initiative=True,
                may_speak=False,
            ),
        )
        assert len(prompt) < 600
        assert len(prompt.splitlines()) <= 8
        for line in prompt.splitlines():
            if line.startswith("记忆："):
                continue  # 数据载荷行，长度由记忆内容决定
            assert len(line) < 200  # 无段落式描写

    def test_static_slots_content(self):
        """身份/语气/长度/输出契约四个静态槽位内容齐全。"""
        prompt = build_system_prompt(
            PAIMON, BehaviorConstraints(max_sentences=2)
        )
        assert "派蒙" in prompt and "伙伴" in prompt  # 身份
        for clause in PAIMON.tone:
            assert clause in prompt  # 语气
        assert "2 句" in prompt  # 长度
        for tag in EMOTION_TAG_ORDER:  # 输出契约含完整标签枚举
            assert tag in prompt
        assert '"speech"' in prompt and '"should_continue"' in prompt

    def test_dynamic_slots_only_when_triggered(self):
        """被打断/主动开口/静默三个行为限制按需渲染。"""
        default = build_system_prompt(PAIMON, BehaviorConstraints(max_sentences=2))
        assert "被打断" not in default
        assert "主动开口" not in default
        assert "安静" not in default

        interrupted = build_system_prompt(
            PAIMON, BehaviorConstraints(max_sentences=2, was_interrupted=True)
        )
        assert "被打断" in interrupted and "不要自动补完原句" in interrupted

        initiative = build_system_prompt(
            PAIMON, BehaviorConstraints(max_sentences=2, is_initiative=True)
        )
        assert "主动开口" in initiative and "speech 为空" in initiative

        silenced = build_system_prompt(
            PAIMON, BehaviorConstraints(max_sentences=2, may_speak=False)
        )
        assert "安静" in silenced

    def test_length_slot_delegates_judgment(self):
        """长度槽位把判断权交给模型：基线+上限都摆出来，不做关键词门。"""
        prompt = build_system_prompt(
            PAIMON, BehaviorConstraints(max_sentences=2)
        )
        assert "2 句" in prompt
        assert f"{PAIMON.long_max_sentences} 句" in prompt
        assert "判断归你" in prompt

    def test_memory_slot_conditional(self):
        """记忆槽位是 stable core 的数据层：有记忆渲染 记忆： 行，
        无记忆不占行；"怎么用"的规则在语气行常驻。"""
        default = build_system_prompt(PAIMON, BehaviorConstraints(max_sentences=2))
        assert "记忆：" not in default
        assert "别往旧事上拐" in default  # 使用规则始终在场
        with_mem = build_system_prompt(
            PAIMON, BehaviorConstraints(max_sentences=2), memory="共同记忆·测试"
        )
        assert "记忆：共同记忆·测试" in with_mem


class TestBehaviorPolicy:
    """doc 05 §3–§7 的规则面：长度/打断/静默/主动开口约束推导。"""

    def test_defaults_short_and_speakable(self):
        c = BehaviorPolicy().derive(NORMAL_INPUT)
        assert c.max_sentences == PAIMON.default_max_sentences == 2
        assert c.may_speak and not c.may_noop
        assert not c.was_interrupted and not c.is_initiative

    def test_interruption_context_sets_flag(self):
        c = BehaviorPolicy().derive(
            {**NORMAL_INPUT, "interruption_context": {"event": "x"}}
        )
        assert c.was_interrupted

    def test_silenced_forbids_speech(self):
        """doc 05 §6：SILENCED 状态下禁止主动发言（闸口依据）。"""
        c = BehaviorPolicy().derive({**NORMAL_INPUT, "state": "SILENCED"})
        assert not c.may_speak

    def test_initiative_allows_noop(self):
        """doc 03 §2.3：主动开口时 LLM 可返回 NOOP。"""
        c = BehaviorPolicy().derive(
            {**NORMAL_INPUT, "initiative_reason": "long_silence"}
        )
        assert c.is_initiative and c.may_noop

    def test_no_keyword_rules_for_length(self):
        """长度判断放权给模型：解释类请求不再触发关键词放宽规则。"""
        c = BehaviorPolicy().derive(
            {**NORMAL_INPUT, "last_user_text": "给我解释一下量子纠缠"}
        )
        assert c.max_sentences == PAIMON.default_max_sentences

    def test_explicit_overrides_win(self):
        """pipeline 显式 behavior_constraints 覆盖规则推导。"""
        c = BehaviorPolicy().derive(
            {
                **NORMAL_INPUT,
                "behavior_constraints": {"max_sentences": 1, "may_noop": True},
            }
        )
        assert c.max_sentences == 1 and c.may_noop


class TestBuildMessages:
    """doc 03 §5 输入 → messages：heard 历史进角色位，本轮输入键值块收尾。"""

    def test_history_roles_and_truncation_mark(self):
        agent_input = {
            **NORMAL_INPUT,
            "recent_heard_history": [
                {"role": "user", "text": "你今天发型不错"},
                {
                    "role": "assistant",
                    "text": "我觉得你今天这个发型特别像",
                    "interrupted": True,
                },
                {"role": "assistant", "text": ""},  # 空文本跳过
            ],
        }
        messages = make_agent(lambda r: None).build_messages(agent_input)
        assert messages[0]["role"] == "system"
        assert messages[1] == {"role": "user", "content": "你今天发型不错"}
        # heard 面被打断轮次补 —— 截断标记
        assert messages[2]["content"].endswith("——")
        assert messages[-1]["role"] == "user"

    def test_context_hygiene_folds_noise(self):
        """防退化卫生：末尾 user 与 turn-input 去重、旧打断碎片折叠成
        带话题前缀的标记、同角色连续条目合并保持交替。"""
        agent_input = {
            **NORMAL_INPUT,
            "last_user_text": "后面这句",
            "recent_heard_history": [
                {"role": "user", "text": "第一句"},
                {
                    "role": "assistant",
                    "text": "说到一半的回复片段一",
                    "interrupted": True,
                },
                {"role": "user", "text": "中间这句"},
                {"role": "user", "text": "又一句"},  # noop 堆出的连续 user
                {
                    "role": "assistant",
                    "text": "最新的半截回复",
                    "interrupted": True,
                },
                {"role": "user", "text": "后面这句"},  # 与 last_user_text 重复
            ],
        }
        messages = make_agent(lambda r: None).build_messages(agent_input)
        contents = [m["content"] for m in messages]
        # 旧碎片折叠成带话题前缀的标记；最新一条保留 ——
        assert "（'说到一半的回复片段一'说到一半被打断）" in contents
        assert "最新的半截回复——" in contents
        # 连续 user 合并成一条
        assert "中间这句；又一句" in contents
        # 与 last_user_text 重复的历史条目不重复渲染（只在 turn 块出现）
        assert sum(c == "后面这句" for c in contents) == 0

    def test_interruption_context_rendered_per_doc03(self):
        """doc 03 §3：四行结构原样呈现给下一轮 LLM。"""
        agent_input = {
            **NORMAL_INPUT,
            "last_user_text": "你敢说完试试。",
            "interruption_context": {
                "assistant_heard": "我觉得你今天这个发型特别像——",
                "assistant_generated_but_not_heard": "一只刚睡醒的史莱姆。",
                "user": "你敢说完试试。",
                "event": "assistant_was_interrupted",
            },
        }
        block = render_turn_input(agent_input)
        assert 'assistant_heard: "我觉得你今天这个发型特别像——"' in block
        assert 'assistant_generated_but_not_heard: "一只刚睡醒的史莱姆。' in block
        assert "event: assistant_was_interrupted" in block
        assert 'user: "你敢说完试试。"' in block

    def test_turn_input_meta_lines(self):
        block = render_turn_input(
            {
                **NORMAL_INPUT,
                "silence_duration_ms": 45000,
                "initiative_reason": "long_silence",
            }
        )
        assert "state: THINKING" in block
        assert "silence_duration_ms: 45000" in block
        assert 'initiative_reason: "long_silence"' in block


class TestStructuredReply:
    """acceptance 1：给定 context，LLM 输出稳定解析为 schema 合法 AgentReply。"""

    CONTEXTS = [
        pytest.param(NORMAL_INPUT, id="normal-turn"),
        pytest.param(
            {
                **NORMAL_INPUT,
                "last_user_text": "你敢说完试试。",
                "interruption_context": {
                    "assistant_heard": "我觉得你今天这个发型特别像——",
                    "assistant_generated_but_not_heard": "一只刚睡醒的史莱姆。",
                    "user": "你敢说完试试。",
                    "event": "assistant_was_interrupted",
                },
            },
            id="interrupted-turn",
        ),
        pytest.param(
            {**NORMAL_INPUT, "last_user_text": "", "initiative_reason": "long_silence"},
            id="initiative",
        ),
    ]

    @pytest.mark.parametrize("agent_input", CONTEXTS)
    async def test_schema_and_emotion_legality(self, agent_input):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=reply_sse(
                    {
                        "speech": "哈？你认真的？",
                        "emotion": "teasing",
                        "energy": 0.8,
                        "should_continue": False,
                    }
                ),
            )

        agent = make_agent(handler)
        reply = await agent.respond(agent_input)
        assert isinstance(reply, AgentReply)
        assert reply.emotion in EMOTION_TAGS
        assert 0.0 <= reply.energy <= 1.0
        assert isinstance(reply.should_continue, bool)
        assert not is_noop(reply)

    async def test_unknown_emotion_falls_back_to_neutral(self):
        """chinese-tts-eval C5：标签集外的 emotion 降级 neutral。"""
        agent = make_agent(
            lambda r: httpx.Response(
                200,
                content=reply_sse({"speech": "哦。", "emotion": "curious"}),
            )
        )
        reply = await agent.respond(NORMAL_INPUT)
        assert reply.emotion == "neutral"

    async def test_fenced_json_tolerated(self):
        agent = make_agent(
            lambda r: httpx.Response(
                200,
                content=sse_body(['```json\n{"speech": "嗯。", "emotion": "soft"}\n```']),
            )
        )
        reply = await agent.respond(NORMAL_INPUT)
        assert reply.speech == "嗯。" and reply.emotion == "soft"

    async def test_empty_speech_is_noop(self):
        """主动开口场景模型选择不说：speech 空 = NOOP。"""
        agent = make_agent(
            lambda r: httpx.Response(
                200, content=reply_sse({"speech": "", "emotion": "neutral"})
            )
        )
        reply = await agent.respond(
            {**NORMAL_INPUT, "initiative_reason": "long_silence"}
        )
        assert is_noop(reply)

    async def test_silenced_short_circuits_without_llm_call(self):
        """doc 05 §6：SILENCED 不打 LLM，直接 NOOP。"""
        calls = []
        agent = make_agent(
            lambda r: (calls.append(r), httpx.Response(200, content=b""))[1]
        )
        reply = await agent.respond({**NORMAL_INPUT, "state": "SILENCED"})
        assert reply == NOOP_REPLY and is_noop(reply)
        assert calls == []

    async def test_request_uses_json_object_mode(self):
        """结构化输出走 response_format=json_object（线上消费口径）。"""
        seen = []
        agent = make_agent(
            lambda r: (
                seen.append(json.loads(r.content)),
                httpx.Response(
                    200, content=reply_sse({"speech": "行。", "emotion": "happy"})
                ),
            )[1]
        )
        await agent.respond(NORMAL_INPUT)
        assert seen[0]["response_format"] == {"type": "json_object"}


class TestLiveQwen:
    """真实端点佐证：.env/环境里有 chat 可用 key 时，用 persona prompt
    打 OpenAI-compatible 端点，断言每种 context 都返回 schema 合法输出。
    无 key 或 key 对 chat/completions 不可用（如 workspace realtime 专用
    sk-ws-* key）时 skip；可用 -k "not Live" 剔除。"""

    CONTEXTS = [
        {**NORMAL_INPUT, "last_user_text": "你觉得今天吃什么？"},
        {
            **NORMAL_INPUT,
            "last_user_text": "你敢说完试试。",
            "interruption_context": {
                "assistant_heard": "我觉得你今天这个发型特别像——",
                "assistant_generated_but_not_heard": "一只刚睡醒的史莱姆。",
                "user": "你敢说完试试。",
                "event": "assistant_was_interrupted",
            },
        },
        {**NORMAL_INPUT, "last_user_text": "派蒙，给我讲个冷笑话。"},
    ]

    @staticmethod
    def _endpoint(env: dict) -> tuple[str, str, str] | None:
        """(base_url, model, api_key)；无 chat 可用配置返回 None。"""
        if key := (env.get("DEEPSEEK_API_KEY") or "").strip():
            return (
                "https://api.deepseek.com/v1",
                (env.get("DEEPSEEK_MODEL") or "deepseek-chat").strip(),
                key,
            )
        key = (env.get("DASHSCOPE_API_KEY") or "").strip()
        if not key or key.startswith("sk-ws-"):
            # sk-ws-* 是 workspace realtime 专用 key，chat/completions 拒收
            return None
        base = (
            env.get("OPENAI_COMPATIBLE_URL")
            or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ).strip()
        return (base, (env.get("QWEN_MODEL") or "qwen-flash").strip(), key)

    async def test_schema_stability_across_contexts(self):
        env = {
            **{
                k: v
                for k, v in dotenv_values(
                    Path(__file__).resolve().parents[1] / ".env"
                ).items()
                if v is not None
            },
            **os.environ,
        }
        spec = self._endpoint(env)
        if spec is None:
            pytest.skip("无 chat/completions 可用 key（DASHSCOPE/DEEPSEEK）")
        base_url, model, key = spec
        llm = OpenAICompatibleLLM(
            base_url=base_url,
            api_key=key,
            model=model,
            timeout=30.0,
            trust_env=False,  # 绕本机 Clash（.env.example）
        )
        agent = CharacterAgent(llm)
        try:
            try:
                replies = [
                    await agent.respond(agent_input, max_tokens=96)
                    for agent_input in self.CONTEXTS
                ]
            except LLMError as e:
                if "401" in str(e) or "invalid_api_key" in str(e):
                    pytest.skip(f"配置的 key 对 chat/completions 不可用: {e}")
                raise
            for reply in replies:
                assert isinstance(reply, AgentReply)
                assert reply.emotion in EMOTION_TAGS
                assert 0.0 <= reply.energy <= 1.0
                assert isinstance(reply.should_continue, bool)
                assert reply.speech.strip()  # 普通轮次不许 NOOP
        finally:
            await llm.close()
