# My-Agent 记忆系统架构

当前实现采用 Raw、Episodic、Semantic 三层记忆，并以 SQLite 作为 Raw、Episodic、游标和检索索引的持久化底座。

## 主链路

1. 用户消息开始一个新的 `turn_seq`。
2. user、assistant、tool 消息只进入 `RawWriteQueue`，主链路不等待 SQLite。
3. 单 Writer 按批量条数或时间间隔提交 `raw_turns/raw_messages`。
4. `ContextManager` 只保留最近 `SESSION_WINDOW_TURNS` 个完整 Turn。
5. 轮数或 Token 预算触发移出时，完整 Turn 进入 PendingBuffer，不同步总结。

## Raw 顺序与恢复

- `UNIQUE(session_id, turn_seq)` 保证 Session 内 Turn 唯一。
- `UNIQUE(turn_id, message_seq)` 保证 Turn 内消息唯一。
- 只有 `status='complete'` 的 Turn 可以用于重启恢复。
- 查询先倒序选择最近 N 个 Turn，再正序返回其全部消息；不能直接对 message 做 `LIMIT N`。

旧 JSONL 会在首次启动时只读导入 SQLite，原文件保留。

## Episodic 异步提炼

PendingBuffer 的触发条件为：

- 数量达到 `EPISODIC_BATCH_TURNS`；或
- 最老 Turn 等待超过 `EPISODIC_MAX_WAIT_SECONDS`。

Worker 调用 LLM 输出 JSON 数组，每个事件只有 `body`、`stable_facts`、`lessons`。`body` 只用一条短句说明“发生了什么”，默认上限 60 字，不包含时间；`occurred_at` 由对应 Raw Turn 的时间写入 SQLite 元数据。

成功时，在同一事务中写入 `episodic_memories` 并推进 `episodic_cursors`，之后才从 Buffer 弹出。失败时原批次保留。Session 淘汰和服务退出都不会强制触发提炼。

## 检索

Episodic 写入 `MemoryIndex`：

- SQLite FTS5 提供 BM25 候选；
- `BAAI/bge-small-zh-v1.5` 提供 512 维向量候选；
- RRF 合并两个排名。

BM25 与向量都只检索 `body`，时间、稳定事实和经验不参与匹配。召回后按 ID 从 `episodic_memories` 补齐元数据，并渲染为“时间 / 事件 / 稳定事实 / 经验”。Episode ID、Session 与 Raw Turn 范围仅留作内部追溯，不注入模型。

Raw 不参与语义检索，只用于短期恢复、Buffer 重建和历史追溯。

## Semantic 与 Dream

Semantic 使用单一 `MEMORY.md`。新条目使用可见时间型编号，例如 `[M-20261028T103522481-000]`；同一毫秒内递增尾部序号。ADD 或 SUPERSEDE 作为 INSERT，在写入前执行正文去重和编号唯一性检查，新编号只能由存储层生成。UPDATE 必须先验证完整目标编号已经存在，然后原位修改并沿用该编号；UPDATE 不生成编号，也不执行 INSERT 的正文查重。旧版 `[M-3964DC78AB]` Hash 编号继续兼容读取和 UPDATE。`source` 和写入时间仅属于运行时调试信息，不写入正式文件。实时记忆工具支持 `ADD` 与 `UPDATE`；Dream 按 `dream_cursors` 获取未处理的 Episodic，并必须通过 `apply_memory_operations` Function Call 提交 `ADD`、`UPDATE`、`SUPERSEDE` 或 `NOOP`，由 `SemanticMemoryStore.apply_operations()` 做原子化局部修改。

Schema 不合法、字段缺失或编号不存在时，工具返回具体字段路径、失败原因及当前可用编号，供模型修正 Function Call 后重试；校验失败不推进 Dream 游标，也不会默认改成 NOOP。

## 关闭语义

优雅退出会尽量清空 Raw 写队列，但不会刷新 PendingBuffer。强杀可能丢失 Raw 最后一个未提交批次，这是明确接受的尾部风险。
