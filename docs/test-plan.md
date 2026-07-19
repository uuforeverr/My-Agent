# my-agent 测试方案

> 目标：为已搭建但未测试的 my-agent 建立一套可重复、可维护、以离线为主的测试体系。
> 框架：pytest + pytest-cov。范围：P0（核心正确性 + 安全）+ P1（流程编排）+ 指定 P2 缺口。
> LLM：默认全部用 FakeLLM 离线跑；真实 DeepSeek 冒烟和真实 Agent E2E 均默认跳过，只在显式开启环境变量时运行。

---

## 1. 测试原则

1. **离线优先**：除冒烟用例外，任何测试都不得发起真实网络请求。LLM 通过 `FakeLLM` 注入。
2. **隔离文件系统**：所有涉及读写的测试使用 pytest `tmp_path`，绝不触碰真实 `data/`、`workspace/`。
3. **确定性**：不依赖真实时间、随机、外部服务。断言必须稳定可复现。
4. **安全边界必测**：workspace 逃逸、bash 危险命令拦截是安全承诺，必须有正向和负向用例。
5. **成本可控**：真实 DeepSeek 用例默认跳过，仅在显式开启环境变量时运行，且每类只发一次最小请求。

---

## 2. 目录与运行方式

```
my-agent/
├── pytest.ini              # pytest 配置（testpaths、markers）
└── tests/
    ├── conftest.py         # 公共 fixture：FakeLLM、临时 workspace、settings
    ├── test_config.py
    ├── test_tools_base.py
    ├── test_file_tools.py
    ├── test_bash_tool.py
    ├── test_raw_store.py
    ├── test_semantic_store.py
    ├── test_episodic_store.py
    ├── test_memory_index.py
    ├── test_curator.py
    ├── test_memory_manager.py
    ├── test_context_manager.py
    ├── test_agent_loop.py
    ├── test_live_agent_e2e.py
    ├── test_llm_client.py
    ├── test_memory_performance.py
    ├── test_memory_tools.py
    ├── test_observability.py
    ├── test_prompts.py
    ├── test_registry.py
    ├── test_security_boundaries.py
    └── test_skills_loader.py
```

运行：

```bash
# 全部离线测试
python3 -m pytest

# 仅冒烟（需真实 DeepSeek，会消耗少量额度）
RUN_LIVE_LLM=1 python3 -m pytest -m live -s

# 覆盖率
python3 -m pytest -q --cov=. --cov-report=term-missing --cov-report=html:htmlcov --cov-report=xml:coverage.xml
```

---

## 3. 公共测试设施（conftest.py）

- `FakeLLM`：实现 `.chat(messages, tools=None)`，按“脚本队列”返回预设响应；记录收到的 messages 供断言。支持：
  - 固定返回一个 assistant 文本；
  - 按调用次数返回不同响应（用于 Agent Loop 的 tool_call → final 两轮）；
  - 返回带 `tool_calls` 的响应。
- `tmp_workspace`：基于 `tmp_path` 建一个隔离 workspace 目录。
- `tmp_memory_dir`：基于 `tmp_path` 建 memory 根目录。
- `settings_factory`：构造一个指向临时目录的 `Settings`，`openai_api_key` 用假值。

---

## 4. P0 用例清单（核心正确性 + 安全）

### 4.1 tools/base.py — `resolve_workspace_path`
- 正常相对路径 → 返回 workspace 内绝对路径。
- 空路径 → `ToolError`。
- `../` 逃逸 → `ToolError`。
- 绝对路径逃逸到 workspace 外 → `ToolError`。
- `BaseTool.run()` 捕获异常 → 返回 `ToolResult(ok=False)`。

### 4.2 tools/file_tools.py
- `WriteFileTool`：写入成功、自动建父目录、返回字节数；越界路径被拒。
- `ReadFileTool`：读取内容、offset/limit 选择行、负 offset 从末尾、超大内容截断、文件不存在报错。
- `EditFileTool`：唯一匹配替换成功；0 次匹配报错；多次匹配报错；空 old_text 追加；`oldText/newText` 别名兼容；无变化报错。
- `GlobTool`：命中文件、目录带 `/` 后缀、无匹配返回占位、`..`/绝对路径 pattern 被拒、max_results 截断。

### 4.3 tools/bash_tool.py
- 正常命令（`echo`）返回 exit_code=0 与输出。
- 危险命令全部被拦：含 `.env`/`api_key`、`sudo`、`rm -rf .`、绝对路径参数、`..`、`curl ... | sh`。
- 超时参数被夹到 `MAX_BASH_TIMEOUT` 上限。
- 输出超长时尾部截断。
- `_safe_env` 不泄漏非白名单环境变量。

### 4.4 memory/semantic_store.py
- 初始化写骨架（`# Semantic Memory` + 两个分组）。
- `remember` 追加到 `## Manual Notes`，带 source 注释。
- 重复内容第二次 `remember` 返回“already exists”，不重复写入。
- `overwrite` 整段替换；空内容兜底为骨架。

### 4.5 memory/episodic_store.py
- `append_summary` 写入分段，含 reason 标题与 session_id，返回条数。
- `_normalize_items` 处理 str/list、去 `- ` 前缀、过滤 `无/None/空`。
- 空摘要返回 0，不建空块。
- `read_recent(days)` 跨天拼接顺序正确。
- `list_files` 按日期升序。

### 4.6 memory/index.py
- `_chunk_markdown` 按 `##` 段切分，行号正确。
- `rebuild` + `search` 命中关键词。
- `reindex_file` 增量更新单文件不影响其他文件。
- `remove_source` 删除指定来源。
- `search` 空 query、min_score 过滤、Top-K 数量。
- FTS 不可用时降级：`_search_fts_ids` 返回空，仍能靠时间兜底出结果。

### 4.7 memory/manager.py
- `build_prompt`：语义全量注入 + 情景 Top-K；无内容返回空。
- `remember` 只写语义、不进索引（语义不参与 search）。
- `flush_from_messages`：返回带 `future` 的异步结果；`future.result()` 后 daily 落盘且情景可检索。
- `_episodic_lock`：并发提交多次 flush 不损坏 daily 文件（串行）。
- `deep_dream`：覆盖 MEMORY.md，写 dream diary；无情景时跳过。
- `close(wait=True)`：等待后台任务完成、可重复调用不报错。

---

## 5. P1 用例清单（流程编排）

### 5.1 agent/context_manager.py
- `append_*` 正确入队。
- `build_model_messages`：system 在首、history 顺序、trace 事件写入。
- `_trim_history`：超阈值裁剪、leading tool 消息被丢、`memory_flush_dispatched` 事件、future 不进 trace。
- `_latest_user_message`：取最近 user。

### 5.2 agent/agent.py（用 FakeLLM）
- 无 tool_call：一轮返回 final answer，raw 落 user+assistant。
- 有 tool_call：assistant→tool→assistant 两轮，工具被执行，raw 落 tool 消息。
- `max_steps` 兜底返回提示并落 raw。
- trace 关键事件齐全（user_message/llm_call/tool_call/final_answer）。

### 5.3 memory/curator.py（用 FakeLLM）
- `summarize_daily`：无 LLM 返回空；正常解析 bullets。
- `parse_bullets`：去前缀、过滤 sentinel。
- `parse_dream_output`：正确切 `[MEMORY]/[DREAM]`；缺段兼容。
- `format_messages`：user/assistant/tool 差异化；tool_calls 记录工具名。
- `_summarize_tool_message`：长输出截头尾。

### 5.4 config.py
- `_load_dotenv`：解析 KEY=VALUE、去引号、跳过注释与空行；不覆盖已存在环境变量。
- `load_settings`：默认值与类型转换正确。

### 5.5 memory/raw_store.py
- `append_message` 追加 JSONL、字段完整。
- `read_day` 逐行解析、坏行跳过。
- `_safe_session_id` 消毒非法字符。

### 5.6 llm/openai_client.py（mock urlopen）
- 无 api_key → `LLMError`。
- HTTP 错误 → `LLMError` 带状态码。
- 正常响应解析出 message。
- 异常响应结构 → `LLMError`。

---

## 6. 真实 DeepSeek 冒烟（受控）

- 标记 `@pytest.mark.live`，默认 `skip`，仅当环境变量 `RUN_LIVE_LLM=1` 时运行。
- 只有 1 个用例：用 `.env` 里的真实 key，构造最小 messages（一句“ping，请回复 pong”），断言返回结构合法（含非空 content 或 tool_calls）。
- 严格控制：`max_tokens` 尽量小；只调用一次；不做多轮；不跑 Agent Loop。
- 目的：验证 `OpenAICompatibleClient` 与 DeepSeek 端点、鉴权、响应解析在真实环境可用。

---

## 7. 真实 Agent + DeepSeek E2E（受控）

- 标记 `@pytest.mark.live`，默认 `skip`，仅当 `RUN_LIVE_AGENT_E2E=1` 时运行。
- 用临时 project root 和 workspace，不污染真实项目。
- 只执行一次低成本任务：让真实 DeepSeek 读取 `a.txt` 并总结一句话。
- 断言真实模型完成一次工具调用，raw 里存在 tool 消息，trace 里存在 `tool_call`。
- 目的：验证真实模型、工具 schema、Agent Loop、工具执行和 raw 记录在真实场景可闭环。

---

## 8. 指定 P2 补充范围

### 8.1 coverage

- 引入 `pytest-cov`。
- 产出 `htmlcov/index.html` 和 `coverage.xml`。
- 报告 source-only 覆盖率，不把 `tests/` 纳入覆盖率主体。

### 8.2 observability / prompts / skills

- `Trace`：事件追加、总耗时、JSON 序列化。
- `JsonlTraceStore`：JSONL 落盘。
- `PromptLoader`：成功、缺失文件、空文件。
- `SkillLoader`：frontmatter、description fallback、XML 转义、空技能过滤、缺失目录。

### 8.3 memory 性能哨兵

- 构造 100 个 daily md。
- 每个 daily 50 个 `##` chunk，总计约 5000 chunks。
- 验证 rebuild 小于 8 秒，search 小于 0.5 秒。
- 这是退化哨兵，不是严格性能基准。

### 8.4 security-focused

- 软链接逃逸读写。
- bash 分号拼接路径逃逸。
- bash 命令替换读取 `.env`。
- launchctl 等系统级命令拦截。

---

## 9. 通过标准

- 离线用例（P0+P1）全部通过：`python3 -m pytest` 绿。
- 冒烟用例在 `RUN_LIVE_LLM=1` 下单独通过一次。
- Agent E2E 用例在 `RUN_LIVE_AGENT_E2E=1` 下单独通过一次。
- 覆盖率报告成功生成。
- 不产生对真实 `data/`、`workspace/` 的写入残留。
- 无遗留临时文件、无 `__pycache__` 混入提交范围。

---

## 10. 不在本轮范围

- 多进程真实并发压测（当前只测单进程线程并发正确性）。
- 真实复杂多轮 Agent 任务压测（当前只做一次文件读取 E2E）。
- 严格性能基准（当前是宽松退化哨兵）。
