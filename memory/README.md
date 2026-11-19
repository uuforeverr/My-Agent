# Memory subsystem

My-Agent 使用 Raw、Episodic、Semantic 三层记忆。首版只考虑单用户、多渠道、多 Session。

## Raw

`RawConversationStore` 将主链路产生的事件放入内存队列，由单个后台 Writer 批量写入 `STATE_DB_PATH` 指向的 SQLite：

- `raw_turns`：`session_id + turn_seq` 唯一，保存 open/complete 状态。
- `raw_messages`：`turn_id + message_seq` 唯一，保存 user、assistant、tool 原文。
- SQLite 使用 WAL；Agent 回复不等待本轮落盘完成。
- 强杀可能丢失最后一个未提交批次；正常运行不静默丢弃队列数据。
- 首次启动会把旧 `data/memory/raw/**/*.jsonl` 导入 SQLite，但不会删除旧文件。

Session 恢复时先按 `turn_seq DESC LIMIT N` 选择最近 N 个 complete Turn，再按 `turn_seq ASC, message_seq ASC` 还原完整上下文。禁止直接限制 message 行数。

## SessionWindow 与 PendingBuffer

每个 Session 的精确短期窗口由 `SESSION_WINDOW_TURNS` 控制，默认保留 20 个已完成 Turn；正在进行的当前 Turn 不占用这 20 个历史名额。窗口向前滑动时，最老的完整 Turn 原样离开窗口并进入内存 PendingBuffer，该过程不调用总结 LLM，也不计为语义压缩。

模型请求超过 Token 预算时执行同步语义压缩：当前 Turn 之前的所有完整 Turn 与已有 Working Summary 合并成一份新的 Working Summary，当前 Turn 原文保持不变。摘要保存任务目标、用户约束、决策、完成进度、文件与命令、卡点、失败方案和下一步，并持久化到 SQLite `state_metadata`；重启后按 `summarized_through_turn_seq` 恢复摘要并避免重复注入已摘要的 Raw Turn。被摘要的完整 Turn 同时进入 PendingBuffer，后续异步提炼情景记忆。若摘要失败，精确历史保持不变。

Episodic Worker 满足任一条件时处理 Buffer：

- 达到 `EPISODIC_BATCH_TURNS`，默认 20 Turn；
- 最老 Turn 等待超过 `EPISODIC_MAX_WAIT_SECONDS`，默认 3600 秒。

Worker 失败时不弹出 Buffer。成功时，Episodic 写入和 `episodic_cursors` 推进位于同一 SQLite 事务；重启或 Session 恢复时，用游标从 Raw 重建位于游标之后、最近 N 个短期 Turn 之前的 Buffer。

## Episodic

`episodic_memories` 是情景记忆的事实表。每条记录包含：

- `body`：只写“发生了什么”的一条短句，默认不超过 60 字；时间不写入正文；
- `stable_facts`：稳定事实、长期决定或明确约束；
- `lessons`：对未来仍有复用价值的经验；
- 时间、Session、Raw Turn 范围等追溯字段。

Episodic 只用 `body` 写入 `MemoryIndex`，使用 SQLite FTS5 BM25 与 BGE 向量检索并通过 RRF 融合。时间、稳定事实和经验不参与检索。召回注入时才从 SQLite 补齐元数据，显示为“时间 / 事件 / 稳定事实 / 经验”；Episode ID、Session 和 Turn 范围不注入模型。

## Semantic

Semantic 只维护一份 `MEMORY.md`。新记忆显示时间型稳定编号，例如 `- [M-20261028T103522481-000] ...`。编号由“本地日期时间到毫秒 + 同毫秒序号”组成；ADD 或 SUPERSEDE 只在 INSERT 前执行正文去重和编号唯一性检查。UPDATE 必须引用已存在编号并保持编号不变，不重新生成编号，也不执行 INSERT 的正文查重。旧版 Hash ID 继续兼容读取和更新。模型不能自行指定新编号。`source`、写入时间等调试信息不写入正式 `MEMORY.md`。模型发现新增稳定事实时实时调用 `remember_memory(ADD)`；发现矛盾或纠正时调用 `remember_memory(UPDATE, memory_id=...)`。Daily Dream 按 `dream_cursors` 消费尚未处理的 Episodic，并通过 `apply_memory_operations` Function Call 提交最小 Operation：

- `ADD`
- `UPDATE`
- `SUPERSEDE`
- `NOOP`

Dream 不再按日期回看，也不整篇覆盖 `MEMORY.md`。工具参数或目标编号无效时返回具体校验路径、错误原因和可用编号，模型可据此重试；不会静默降级为 NOOP。

## 关键配置

参见根目录 `.env.example`：

- `SESSION_WINDOW_TURNS`
- `RAW_WRITE_BATCH_SIZE`
- `RAW_WRITE_FLUSH_INTERVAL_MS`
- `RAW_WRITE_QUEUE_CAPACITY`
- `EPISODIC_BATCH_TURNS`
- `EPISODIC_MAX_WAIT_SECONDS`
- `EPISODIC_WORKER_POLL_SECONDS`
- `EPISODIC_BODY_MAX_CHARS`
- `MEMORY_SEARCH_RESULTS`
- `MEMORY_EMBEDDING_*`
- `MEMORY_VECTOR_BACKEND`
- `MEMORY_CANDIDATE_LIMIT`
- `MEMORY_RRF_K`
