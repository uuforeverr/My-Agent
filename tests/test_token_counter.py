from agent.token_counter import (
    TokenCounter,
    encoding_for,
    resolve_context_window,
    resolve_max_input_tokens,
)


def test_unknown_model_does_not_raise_and_uses_fallback_window():
    counter = TokenCounter(model="definitely-not-a-real-model")
    encoding = encoding_for("definitely-not-a-real-model")
    assert encoding.name == "cl100k_base"
    assert counter.context_window == 32768
    assert counter.max_input_tokens == 26214
    assert counter.count_text("hello") > 0


def test_gpt4o_mini_window_is_80_percent_of_128k():
    counter = TokenCounter(model="gpt-4o-mini")
    assert resolve_context_window("gpt-4o-mini") == 128000
    assert counter.max_input_tokens == 102400


def test_o3_prefix_uses_200k_window():
    assert resolve_context_window("o3-mini") == 200000
    assert resolve_max_input_tokens("o3-mini") == 160000


def test_qwen_and_deepseek_windows():
    assert resolve_context_window("qwen-plus") == 131072
    assert resolve_context_window("deepseek-chat") == 128000
    assert resolve_max_input_tokens("qwen-plus") == 104857


def test_window_override_beats_builtin_table():
    counter = TokenCounter(model="gpt-4o-mini", window_override=1000)
    assert counter.context_window == 1000
    assert counter.max_input_tokens == 800
    assert resolve_context_window("gpt-4o-mini", window_override=1000) == 1000


def test_count_messages_includes_tool_calls_and_overhead():
    counter = TokenCounter(model="fake-model")
    plain = counter.count_messages([{"role": "user", "content": "hi"}])
    with_tools = counter.count_messages([
        {
            "role": "assistant",
            "content": "hi",
            "tool_calls": [{"id": "c1", "function": {"name": "glob", "arguments": "{}"}}],
        }
    ])
    assert plain > 0
    assert with_tools > plain


def test_count_tools_and_payload():
    counter = TokenCounter(model="fake-model")
    schemas = [{"type": "function", "function": {"name": "glob", "parameters": {}}}]
    tool_tokens = counter.count_tools(schemas)
    assert tool_tokens > 0
    messages = [{"role": "user", "content": "hi"}]
    assert counter.count_payload(messages, schemas) == counter.count_messages(messages) + tool_tokens
    assert counter.count_tools([]) == 0
