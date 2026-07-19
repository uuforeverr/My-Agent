# my-agent 测试报告

## 结论

本轮已为 my-agent 建立 pytest 测试体系，并完成 P0 + P1 范围验证。

当前测试结果：

```text
离线完整测试：113 passed, 2 skipped in 2.79s
源代码覆盖率：91%
真实 DeepSeek 冒烟：1 passed in 3.22s
真实 Agent + DeepSeek E2E：1 passed in 3.89s
```

整体效果：**当前系统的核心能力、工具安全边界、memory 分层设计、Agent Loop 编排已经具备基础回归保障**。后续改 memory、tools、context、agent loop 时，可以先跑 `python3 -m pytest -q` 判断是否破坏主链路。

## 执行命令

离线测试：

```bash
python3 -m pytest -q
```

覆盖率测试：

```bash
python3 -m pytest -q --cov=. --cov-report=term-missing --cov-report=html:htmlcov --cov-report=xml:coverage.xml
```

覆盖率报告产物：

```text
htmlcov/index.html
coverage.xml
```

真实 DeepSeek 冒烟测试：

```bash
RUN_LIVE_LLM=1 python3 -m pytest tests/test_llm_client.py::test_live_deepseek_smoke -q -s
```

真实 Agent + DeepSeek E2E：

```bash
RUN_LIVE_AGENT_E2E=1 python3 -m pytest tests/test_live_agent_e2e.py::test_live_deepseek_agent_reads_file_once -q -s
```

`live` 用例默认跳过，只有显式设置 `RUN_LIVE_LLM=1` 才会调用真实模型，避免无意消耗 API 用量。

## 测试覆盖范围

### 工具层

覆盖文件：

```text
tools/base.py
tools/file_tools.py
tools/bash_tool.py
```

覆盖内容：

- workspace 路径逃逸防护
- `BaseTool.run()` 成功/异常包装
- `glob/read_file/write_file/edit_file` 正常路径和边界条件
- `read_file` 行范围、负 offset、大文件截断
- `edit_file` 唯一替换、0 次匹配、多次匹配、append、camelCase 参数兼容
- `bash` 正常命令、危险命令拦截、超时上限、输出截断、环境变量白名单
- security-focused 测试：软链接逃逸、shell 命令拼接、命令替换读取敏感文件、系统级命令拦截

效果评价：工具层测试质量较高，尤其是路径安全和 bash 安全边界已经有明确回归用例。

### Memory 层

覆盖文件：

```text
memory/raw_store.py
memory/episodic_store.py
memory/semantic_store.py
memory/curator.py
memory/index.py
memory/manager.py
```

覆盖内容：

- raw JSONL 写入、读取、坏行跳过、session_id 消毒
- daily 情景记忆 append、normalize、reason title、read_recent、list_files
- `MEMORY.md` 初始化、手动语义记忆、去重、overwrite
- curator daily summary、dream output 解析、tool message 截断
- index chunk、search、增量 reindex、remove_source、FTS 降级
- manager 的语义全量注入、情景 Top-K、异步 flush、并发 flush 串行、deep_dream、close 等待
- 性能哨兵：100 个 daily 文件、约 5000 个 chunk，覆盖 rebuild/search 时间上限

效果评价：memory 是本轮测试收益最大的部分。当前 raw / episodic / semantic / index / curator / manager 都有单元测试，且覆盖了异步 flush 和增量索引这两个风险点。

### Context 与 Agent Loop

覆盖文件：

```text
agent/context_manager.py
agent/agent.py
```

覆盖内容：

- system prompt 组装
- memory 注入 metadata
- history 裁剪
- leading tool message 清理
- 异步 memory flush dispatch trace
- Agent 无工具调用一轮完成
- Agent tool_call 两轮完成
- max_steps 兜底
- raw 记录 user/assistant/tool 消息
- trace 关键事件
- 真实 Agent + DeepSeek E2E：通过真实模型触发一次文件读取工具调用

效果评价：主链路已有基础端到端保障。通过 FakeLLM 避免真实网络依赖，同时覆盖了工具调用循环。

### Observability / Skills / Prompts

覆盖文件：

```text
observability/tracing.py
prompts/loader.py
skills/loader.py
```

覆盖内容：

- Trace 事件追加、耗时、JSON 序列化
- JsonlTraceStore 追加 JSONL
- PromptLoader 成功读取、缺失文件、空文件异常
- SkillLoader frontmatter 解析、description fallback、XML 转义、空技能过滤、缺失目录

效果评价：此前 P2 的可观测性、技能、prompt 模块已补充细测，基础行为具备回归保障。

### Config 与 LLM Client

覆盖文件：

```text
config.py
llm/openai_client.py
```

覆盖内容：

- `.env` 解析、引号剥离、注释跳过、不覆盖已有环境变量
- `Settings` 默认值与类型转换
- LLM 无 key 报错
- mock urlopen 成功响应
- mock HTTPError 分支
- 异常响应结构报错
- 真实 DeepSeek 最小冒烟

效果评价：LLM 客户端的关键错误分支和真实服务连通性都已覆盖。真实测试只有一个最小请求，成本可控。

## 本轮发现并修正的问题

### 1. curator 截断测试数据不足

原测试用 50 行 `lineN`，总字符数未超过 `_summarize_tool_message()` 的 500 字符阈值，因此不会触发截断。

处理：

- 将测试数据改为 `line0000-padding-content` 形式；
- 显式断言 `len(long_text) > 500`；
- 再断言输出包含 `...`、头部和尾部内容。

价值：

- 测试现在真正覆盖了长工具输出截断逻辑。

### 2. MemoryIndex 搜索兜底行为被误解

原断言认为删除某关键词后 `search("banana")` 应该返回空。但实现中有“候选不足时按更新时间兜底”的设计，低分结果仍可能返回。

处理：

- 改为断言旧关键词不会出现高分命中；
- 同时断言新关键词和未受影响关键词仍可高分命中。

价值：

- 测试更贴合真实设计：搜索有召回兜底，但相关结果应分数更高。

### 3. HTTPError mock 不完整

最初构造 `HTTPError(fp=None)`，导致业务代码调用 `e.read()` 时触发 mock 自身错误，而非真实 HTTPError 分支。

处理：

- 用 `io.BytesIO` 提供错误响应 body；
- 断言 `LLMError` 中包含 `LLM HTTP 401`。

价值：

- 真实覆盖了 `openai_client.py` 的 HTTP 错误包装逻辑。

## 测试效果评估

### 已经能保障什么

1. **文件工具不会轻易越权**  
   `../`、绝对路径、glob 越界、bash 危险命令都已测。

2. **memory 分层主设计可回归**  
   raw 原文、daily 情景、MEMORY.md 语义、index 检索、curator 总结、manager 异步编排都已有测试。

3. **Agent Loop 基本闭环稳定**  
   用户输入、LLM 响应、tool_call、tool result、final answer、max_steps 都有用例。

4. **真实模型接口可用**  
   DeepSeek 冒烟已通过，说明 `.env` 配置、鉴权、接口格式、响应解析在真实环境可通。

### 还不能完全保障什么

1. **没有真实多进程并发测试**
   当前只测了同进程线程并发。多进程同时写同一个 daily/index 还没有保证。

2. **真实 Agent E2E 仍是低频小样本**
   当前只做了一次受控文件读取工具调用，能验证真实模型会调用工具，但不代表复杂多轮任务全部可靠。

3. **性能测试只是退化哨兵**
   100 daily / 5000 chunk 用例能发现明显退化，但不是严谨 benchmark，没有统计多轮均值、P95 或硬件归一化。

4. **覆盖率仍有结构性空洞**
   `main.py` 没进入覆盖率，因为 CLI 交互不适合单元测试；部分异常兜底分支仍未覆盖。

## 质量判断

当前测试体系可以作为 my-agent 继续开发的安全网。

如果后续改动以下模块，建议必须先跑完整离线测试：

```text
memory/*
tools/*
agent/context_manager.py
agent/agent.py
llm/openai_client.py
```

如果改动模型供应商、`.env` 配置、OpenAI-compatible payload，再额外跑一次 live 冒烟：

```bash
RUN_LIVE_LLM=1 python3 -m pytest tests/test_llm_client.py::test_live_deepseek_smoke -q -s
```

如果改动 Agent Loop、工具 schema 或模型 prompt，再额外跑一次真实 Agent E2E：

```bash
RUN_LIVE_AGENT_E2E=1 python3 -m pytest tests/test_live_agent_e2e.py::test_live_deepseek_agent_reads_file_once -q -s
```

## 后续建议

1. 增加 CI 命令：默认跑 `python3 -m pytest -q`，不跑 live。
2. 增加覆盖率阈值，例如 `--cov-fail-under=85`。
3. 对 `main.py` 做 CLI 命令级测试，或在覆盖率中显式 omit 交互入口。
4. 增加 memory 性能测试的多轮统计，记录 rebuild/search 平均值和 P95。
5. 后续安全加固后继续扩展 bash 绕过用例，例如引号拆分、变量拼接、子 shell、更复杂的重定向。
