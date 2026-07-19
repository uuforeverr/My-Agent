# my-agent Memory 设计说明书

> 面向读者：需要独立理解 my-agent 的记忆系统，或需要把这份设计落成流程图的人。
> 阅读方式：先看第 1 节掌握骨架，再按 2–8 节展开细节。9 节列了未来演进方向。

---

## 1. 总览

### 1.1 设计目标

Memory 系统要同时满足四件事：

1. **可追溯**：任何回答都能追溯到当时的原始对话。
2. **可回忆**：长期偏好、决策、约束能被稳定注入模型上下文。
3. **可检索**：与当前问题相关的历史事件可以按需检索出来。
4. **不阻塞**：所有耗时工作（LLM 摘要、SQLite 写入）不应阻塞主循环。

### 1.2 三层记忆 + 一层索引

my-agent 把记忆划成三层，另加一层检索加速：

| 层 | 载体 | 是否进索引 | 是否注入模型 | 生成方式 |
| --- | --- | --- | --- | --- |
| 原始对话 raw | `data/memory/raw/{session}/YYYY-MM-DD.jsonl` | 否 | 否 | 每条消息追加 |
| 情景记忆 episodic | `data/memory/daily/YYYY-MM-DD.md` | 是 | 检索 Top-K | LLM 事件摘要 |
| 语义记忆 semantic | `data/memory/MEMORY.md` | 否 | 全量注入 | 手写 + 做梦重写 |
| 检索索引 index | `data/memory/index.db` (SQLite + FTS5) | 本身即索引 | 否 | 从情景记忆抽块 |

一句话记忆分层：**raw 是事实，episodic 是事件摘要，semantic 是长期结论，index 只服务情景检索。**

### 1.3 中心与卫星

Agent Loop 是记忆系统唯一的调用者。它只调 4 个入口：

- `memory.record_message()` — 落原文
- `memory.build_prompt(query)` — 组 memory 段落
- `memory.flush_from_messages(discarded)` — 上下文裁剪时的情景摘要
- `memory.deep_dream()` — 手动做梦

`MemoryManager` 是这四个入口的门面，内部把工作分派给 5 个子模块：`raw_store` / `episodic_store` / `semantic_store` / `curator` / `index`。

---

## 2. 存储物理布局

启动后 `data/memory/` 大致长这样（例子）：

```
data/memory/
├── MEMORY.md                          # 语义记忆本体（全量注入）
├── daily/
│   ├── 2026-07-05.md                  # 情景记忆本体（按天）
│   └── 2026-07-06.md
├── raw/
│   └── default/                       # 按 session_id 分目录
│       ├── 2026-07-05.jsonl           # 原始对话（按天）
│       └── 2026-07-06.jsonl
├── dreams/
│   └── 2026-07-06.md                  # 做梦日志（不是记忆本体）
└── index.db                           # SQLite + FTS5，只索引 daily/*.md
```

三个约定：

- **每天一个文件**：raw / daily / dreams 全部按 `YYYY-MM-DD` 分文件，方便按天回溯与做梦。
- **session 隔离原文**：raw 按 `session_id` 分目录，未来支持多 session；daily/semantic 目前共享。
- **索引只跟 daily**：`index.db` 里只有 daily md 的分块；`MEMORY.md` 和 raw jsonl 都不进。

---

## 3. 六个模块职责

### 3.1 agent/agent.py（Agent Loop，中心）

不是 memory 子模块，但它决定所有 memory 动作的触发时机。参见第 6 节触发表。

### 3.2 memory/manager.py（门面 + 异步）

对外唯一入口 `MemoryManager`。核心职责：

- 拉起 5 个子模块并保证目录存在。
- 维护一个 `ThreadPoolExecutor(max_workers=1)` + `Lock`，用于异步 flush。
- 冷启动时对情景记忆做一次全量索引重建。
- 语义/情景/原文分派，做梦编排。

关键函数：`__init__` / `remember` / `record_message` / `build_prompt` / `search` / `flush_from_messages` / `deep_dream` / `close`。

### 3.3 memory/raw_store.py（事实来源）

只追加原始 JSONL，不做压缩不做检索。文件路径规则：
`raw/{safe_session_id}/{YYYY-MM-DD}.jsonl`

每条 JSON：

```json
{
  "created_at": "2026-07-06T14:35:12",
  "session_id": "default",
  "role": "user | assistant | tool",
  "content": "...",
  "name": "tool_name or null",
  "metadata": {}
}
```

关键函数：`append_message()` / `read_day()` / `_safe_session_id()`。

### 3.4 memory/episodic_store.py（情景记忆本体）

daily md 就是情景记忆的**唯一事实**。它不生产内容，只负责落盘和读。

- 一天一个 md 文件；文件不存在时写入 `# Daily Memory: YYYY-MM-DD` 骨架。
- 每次 flush 追加一段：

```markdown
## Trimmed Context (14:35)

session_id: `default`

- 用户希望把 flush 改成异步。
- 用户确认语义记忆走全量注入。
```

关键函数：`append_summary()` / `read_day()` / `read_recent(days)` / `list_files()` / `_reason_title()` / `_normalize_items()`。

### 3.5 memory/semantic_store.py（语义记忆本体）

`MEMORY.md` 每次调用模型前会**全量注入**到 system prompt，所以必须精炼，不能无限增长。

初始骨架：

```markdown
# Semantic Memory

## User Preferences

## Project Decisions
```

- 手写记忆自动归到 `## Manual Notes`，附 HTML 注释里写 `source` 和时间戳。
- 做梦机制可以整段覆盖。
- 用文本级“整条相等”去重，不做模糊匹配。

关键函数：`read()` / `remember()` / `overwrite()` / `_ensure_manual_section()` / `_contains_memory()`。

### 3.6 memory/curator.py（唯一 LLM 层）

其他模块都不调 LLM。curator 只干两件事：

1. `summarize_daily(messages, existing_daily, ...)`：把被裁剪的对话总结成 daily 事件 bullet。
2. `deep_dream(semantic, episodic)`：把当前语义 + 最近情景蒸馏为新的 `MEMORY.md` + dream diary。

两个 system prompt：

- `DAILY_SUMMARY_SYSTEM_PROMPT`：强调“按事件不按轮次、保留决策/文件路径/函数名、禁止编造、工具输出只留关键事实、无价值时输出‘无’”。
- `DREAM_SYSTEM_PROMPT`：要求输出 `[MEMORY]` + `[DREAM]` 两段，MEMORY 部分是精炼合并后的完整新语义，DREAM 是简短整理过程说明。

关键函数：`summarize_daily()` / `deep_dream()` / `format_messages()` / `_summarize_tool_message()` / `parse_bullets()` / `parse_dream_output()`。

### 3.7 memory/index.py（检索索引）

SQLite + FTS5，只覆盖情景记忆。核心并发保证：`threading.RLock` + `sqlite3.connect(check_same_thread=False)`，主线程 search 与后台 reindex 并发安全。

关键函数：

- `rebuild(files, root)`：冷启动一次性重建（当前只喂 daily md）。
- `reindex_file(path, source_type, root)`：flush 完成后**只重建单个 daily 文件**（增量）。
- `remove_source(rel_path)`：清理某个 daily。
- `search(query, limit, min_score)`：FTS 召回候选 → `_score` 排序 → Top-K。
- `_chunk_markdown()`：按 `##` 段切块。
- `_score()`：完整 query 命中 +0.6，token 命中率最多 +1.0，`source_type=episodic` +0.05。

---

## 4. 记忆如何产生

### 4.1 原文（raw）

- **谁产生**：Agent Loop。
- **何时产生**：任何一条消息进入 `ContextManager.history` 的同时。
- **写入规则**：只追加，永不修改。
- **失败策略**：追加失败不会中断主循环，会向上抛异常由 Agent 兜底。

具体触发点（`agent/agent.py`）：

- 用户输入进来 → 写一条 `role=user`。
- LLM 返回 assistant 消息 → 写一条 `role=assistant`（同时把 `tool_calls` 放到 metadata）。
- 每个工具执行完 → 写一条 `role=tool`，附 `tool_call_id/ok`。
- 达到 `max_steps` 兜底回答时 → 写一条 `role=assistant`。

### 4.2 情景记忆（episodic）

- **谁产生**：`MemoryCurator.summarize_daily()` 调用 LLM。
- **何时产生**：只有当 `ContextManager._trim_history()` 发生裁剪时。裁剪条件是 `len(history) > max_history_messages`。
- **不是每轮都产生**：只在“上下文即将超出”时才触发。也就是说，短对话根本不会有 daily。
- **不是攒够 N 次再产生**：每次裁剪立即触发一次；由 discarded 是一批完整 turns 天然带来了“攒批”效果。
- **异步产生**：`flush_from_messages` 把 discarded 提交给单线程池 `ThreadPoolExecutor(max_workers=1)`，主循环立即拿到 `future` 返回，不等 LLM 完成。
- **串行执行**：`_episodic_lock` 保证同一天 daily md 不会被并发写。

摘要规则（curator 的 system prompt）：

- 按事件维度合并同一件事的多轮讨论；
- 保留用户明确的需求、纠正、偏好、约束；
- 保留决策、结论、待办、路径、函数名、错误处理；
- 忽略寒暄、重复确认、无价值中间话；
- 工具输出只保留“做了什么/结果如何”，长文本自动截头尾；
- 没有可记内容时输出“无”，`episodic_store` 会自动跳过；
- 每条一行 `- xxx`，一句话；不输出 JSON/表格/标题。

### 4.3 语义记忆（semantic）

- **谁产生**：两条路径。
- **路径一：手写记忆**。`remember_memory` 工具（或 `MemoryManager.remember()`）触发 `semantic_store.remember()`，把一条 bullet 追加到 `## Manual Notes`。
  - 会做整条文本去重。
  - 每条附 HTML 注释：`<!-- source: xxx, at: ISO 时间 -->`。
- **路径二：做梦蒸馏**。`deep_dream()` 读语义 + 最近情景，调 `curator.deep_dream()`，用返回的 `[MEMORY]` 段整段覆盖 `MEMORY.md`。
- **没有第三条路径**：情景记忆本身不会自动进语义层。要沉淀成语义，必须靠做梦或人工手写。

---

## 5. 记忆如何更新

### 5.1 原文（raw）

不更新。永久追加。要“修正”只能重新写一条新消息。原文的价值在于忠实还原历史。

### 5.2 情景记忆（episodic）

不修改历史 daily 文件。每次 flush 只往当天文件 append 一段 `##`。这样做的好处：

- 保留时间线：一天中的多次事件按发生顺序留痕。
- 无需事务：追加天然不会破坏已有内容。
- 索引增量：只需 `reindex_file(today_daily)`，不需要重建全部。

如果日后要清理，直接删对应 daily md，然后 `index.remove_source(rel_path)`；或者调 `deep_dream()` 让做梦把它蒸发到语义层后手工删除。

### 5.3 语义记忆（semantic）

三种更新方式，按主动性从低到高：

1. **手写累加**：`remember_memory` 工具或 API 调用 `MemoryManager.remember()`。只在 `## Manual Notes` 追加，不会重排现有分组。
2. **做梦重写**：`deep_dream()` 拿到 `[MEMORY]` 段后 `semantic_store.overwrite()` 整段替换整个 `MEMORY.md`。做梦机制负责合并重复、解决冲突、删除过期。
3. **直接编辑**：`MEMORY.md` 就是本体，你打开编辑器改也算数，下次注入模型立即生效。

`MEMORY.md` 不进索引也不做增量，是因为它**每次都全量注入模型**。让它自身保持精炼即可，反而不需要索引加速。

### 5.4 检索索引（index）

- 启动时：`MemoryManager.__init__` 调 `_rebuild_episodic_index()`，走 `index.rebuild(daily 文件列表)`，一次性全量。
- flush 完成后：`_flush_worker` 内部调 `_reindex_today_episodic()`，只对今天的 daily md 走 `index.reindex_file()`，增量。
- 手动清理：`index.remove_source(rel_path)`，配合 daily 文件删除。
- 并发：写读全部走 `RLock` 串行，`check_same_thread=False` 允许后台线程与主线程共用同一个连接。

---

## 6. 记忆如何被使用

### 6.1 语义记忆 — 每次调模型都全量注入

`MemoryManager.build_prompt(query)`：

- 读 `semantic_store.read()`（`MEMORY.md` 全量文本）。
- 如果有内容，直接嵌进 system prompt 的 `### Semantic memory` 段。
- 不做检索，不做截断（除非你在 `MEMORY.md` 里自己压缩）。

### 6.2 情景记忆 — 检索 Top-K

同一个 `build_prompt(query)`：

- 用当前用户最近一条 `user` 消息作为 query。
- 调 `MemoryIndex.search(query, limit, min_score)`。
- FTS 召回候选 → 打分 → 排序 → 拿 Top-K。
- 每条注入格式：`- 内容 (source=daily/2026-07-06.md:12-24)`，带来源，方便模型引用。

### 6.3 原文 — 不注入，只作追溯

模型永远看不到原文。除非你手写工具去 `raw_store.read_day()`，那属于扩展用途。

### 6.4 模型可否主动调用

- 模型可以调用 `memory_search` 工具，走 `MemoryIndex.search()` 主动“回忆”。
- 模型不能主动触发做梦。做梦目前只有 CLI `/dream` 入口。

---

## 7. 触发时机总表

| 动作 | 触发者 | 触发时机 | 同步/异步 | 是否调 LLM |
| --- | --- | --- | --- | --- |
| 追加 raw jsonl | Agent Loop | 每条 user/assistant/tool 消息 | 同步 | 否 |
| 组 memory prompt | ContextManager | 每次调模型前 | 同步 | 否 |
| 裁剪 history | ContextManager | `len(history) > max_history_messages` | 同步 | 否 |
| daily 情景摘要 | MemoryManager._flush_worker | 裁剪发生后立即 dispatch，后台执行 | 异步 | 是 |
| 情景索引增量 | MemoryIndex.reindex_file | daily 追加成功后 | 异步（跟随 flush 线程） | 否 |
| 情景索引全量 | MemoryIndex.rebuild | 启动时一次 | 同步 | 否 |
| 手写语义记忆 | remember_memory 工具或 CLI | 模型/用户显式调用 | 同步 | 否 |
| 做梦蒸馏 | MemoryManager.deep_dream | CLI `/dream` 手动触发 | 同步（CLI 阻塞） | 是 |
| 关闭 memory | main.py finally | CLI 退出前 | 同步 wait=True | 否 |

---

## 8. 并发与失败保证

### 8.1 并发

- 主线程：Agent Loop 里 `record_message` / `build_prompt` / `search` / `_trim_history` 都在主线程执行。
- 后台线程：`ThreadPoolExecutor(max_workers=1)` 里跑 `_flush_worker`，串行处理裁剪任务。
- 锁：
  - `MemoryManager._episodic_lock`：保护 daily md 写入 + `reindex_file` 的完整过程。
  - `MemoryIndex._lock` (`RLock`)：保护 SQLite 连接的读写。
- 单 worker 的作用：让 daily md 追加与索引增量按 FIFO 串行，不会互相踩踏。

### 8.2 失败

- LLM 失败：`_flush_worker` 捕获异常，返回 `{"ok": False, "error": ...}`。主循环不会挂。
- daily 写入失败：捕获后同样返回错误统计。原文 jsonl 仍然存在，可以下次重来。
- 索引失败：捕获，情景记忆本体依然可读，只是暂时搜不到。
- SQLite 无 FTS5：`_init_db` 尝试建 FTS 表失败会把 `fts_available=False`，索引降级为“按更新时间兜底”。
- 进程崩溃：由于 raw + daily 都是文件系统追加，最坏情况丢的是内存里未 flush 的一批裁剪对话。原文一定在。

### 8.3 退出

- CLI `main.py` finally 里调 `agent.memory.close(wait=True)`。
- `close()` 会 `executor.shutdown(wait=True)`，等所有后台 flush 完成再关索引连接。
- 保证退出时不会丢正在进行的 daily flush。

---

## 9. 未来可能的演进

### 9.1 定时做梦

`/dream` 是第一步。之后可以加：

- 独立进程 cron / 系统定时器每天凌晨调一次 `agent.memory.deep_dream(lookback_days=1)`。
- 或者在 my-agent 里加一个轻量 scheduler 线程；启动时按“上次做梦日期”判断是否需要跑。

### 9.2 embedding 检索

`MemoryIndex._score()` 已经预留 `source_type` 加分位。之后若接入 embedding：

- 在 `search()` 里增加向量召回，与 FTS 结果做 rank fusion。
- 需要一个 embedding 缓存表，规避重复 embed。

### 9.3 raw 索引 / 精细回溯

目前 raw 不进索引，若日后需要“昨天下午我们讨论过什么代码路径”这种能力，可以：

- 给 `raw_store` 加一个只读检索层（不用 SQLite，可以直接 grep + tokenize）。
- 或者在做梦时把 raw 作为 fallback 输入，弥补 daily 覆盖不全的情况。

### 9.4 多用户 / 多 session

- 目前只 raw 分了 session；daily/semantic 是共享的。
- 若要多用户隔离，daily 和 MEMORY.md 都需要按 `user_id` 拆目录，检索按用户过滤。

---

## 10. 一句话总结

> 原文永远保留在 raw；情景在裁剪时异步摘要成 daily md 并增量索引；语义靠手写和做梦精炼进 MEMORY.md 全量注入；做梦目前手动 `/dream`，之后再接定时器。

这份说明书按“分层 → 模块 → 生成 → 更新 → 使用 → 触发 → 保证 → 演进”组织，你在其他平台画流程图时可以直接拿每一节的标题作为节点组。
