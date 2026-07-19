# SOP 提效机制设计（滚雪球式增量归纳）

> 目标：帮用户发现工作中的重复劳动 / 可固化流程（SOP），并给出提效建议。
> 与做梦机制平级、同构：做梦是"旧 MEMORY.md + 近期 episodic → 新 MEMORY.md"，
> SOP 提效是"旧 SOP + 近期 episodic → 新 SOP"。同一个"增量蒸馏"范式，产出物不同。
> 本文档为落盘设计，已与用户对齐后进入实现。

---

## 1. 核心思想：滚雪球式增量归纳

重复工作 / SOP 无法从"一天"看出来——它可能横跨一周、一月。所以：

- **不是**一次扫描固定窗口就完事。
- **而是**一版接一版、越滚越厚的增量迭代：每次归纳都**带上上一版 SOP 成果**，加上本次窗口的新事件，重新精炼出更成熟的一版。

```
第1版：看最近 3 天事件                    → SOP_v1
   ↓（过一阵子）
第2版：[SOP_v1] + 最近 7 天事件           → SOP_v2（在 v1 基础上精炼/补充/修正）
   ↓
第3版：[SOP_v2] + 最近 15 天事件          → SOP_v3
   ↓
... 往复。窗口长短由用户用 /sop <天数> 灵活控制，SOP 像滚雪球一样不断沉淀。
```

关键：**每次归纳的输入 = 上一版 SOP 全文 + 本次窗口的近期情景记忆**。站在上一版肩膀上继续提炼，而不是每次从零重扫。

---

## 2. 为什么独立于做梦机制

| 维度 | 做梦 deep_dream | SOP 提效 distill_workflows |
|------|-----------------|----------------------------|
| 蒸馏目标 | 稳定事实 / 偏好 / 决策 | 重复流程 / 可固化 SOP / 提效点 |
| 产物 | 更新 MEMORY.md（会被检索注入对话） | SOP.md + 提效洞见（**不注入**对话） |
| 时间窗口 | 短（默认 1 天） | 可滚动拉长（3/7/15…） |
| 触发 | /dream | /sop [天数] |
| 频率/成本 | 高频、轻 | 低频、重 |

结论：产物、窗口、注入策略、频率都不同，硬塞进 deep_dream 会两边都做不好。独立成机制，但**复用同构范式和现成数据源**。

---

## 3. 数据源：复用现成的 episodic

不新增埋点。`episodic_store` 已按天存好 `data/memory/daily/YYYY-MM-DD.md`，
直接 `read_recent(days=N)` 就能拿到 N 天的情景记忆作为本次归纳的"新事件"。

---

## 4. 组件设计

### 4.1 新增 `memory/sop_store.py`（仿 SemanticMemoryStore）

```
data/memory/workflows/
├── SOP.md                       # 单一文件，像 MEMORY.md 一样被反复迭代 overwrite
└── log/
    └── YYYY-MM-DD-HHMMSS.md     # 每次迭代留痕，可回看每版怎么演进
```

方法：
- `read() -> str`：读当前 SOP 全文（首次为空/骨架）。
- `overwrite(content: str)`：用新一版 SOP 覆盖 SOP.md。
- `append_log(sop: str, insights: str, meta: dict)`：把本次迭代写进带时间戳的 log 文件。

**不注入对话**：SOP 不参与 `build_prompt` 的检索注入，避免每次聊天灌一堆流程分析。

### 4.2 curator 新增 `distill_workflows` + 提示词

新提示词 `WORKFLOW_SOP_SYSTEM_PROMPT`，产出两部分：

```
[SOP]
（在旧 SOP 基础上精炼/补充/修正后的标准流程，步骤化）

[INSIGHTS]
（哪些是重复劳动、可怎么自动化/批处理/模板化提效）
```

方法签名：
```python
def distill_workflows(self, existing_sop: str, episodic_recent: str) -> tuple[str, str]:
    # 输入：上一版 SOP 全文 + 本次窗口的近期情景记忆
    # 输出：(new_sop, insights)
    # 无 LLM 或无 episodic 时返回 ("", "")
```

解析函数 `parse_sop_output(text) -> (sop, insights)`，仿 `parse_dream_output` 切 `[SOP]/[INSIGHTS]`。

提示词要点：
- 从近期情景记忆中识别**重复出现的操作序列 / 例行任务**。
- 与旧 SOP 合并：相同流程精炼、新流程补充、过时流程修正或删除。
- SOP 部分：步骤化、可执行、可复用。
- INSIGHTS 部分：明确指出"你在重复做 X，可以这样提效"。
- 严禁编造材料中不存在的流程。
- 无可固化内容时输出"无"。

### 4.3 manager 新增 `distill_workflows(lookback_days=3)`

```python
def distill_workflows(self, lookback_days: int = 3) -> dict:
    existing_sop = self.sop_store.read()
    episodic = self.episodic_store.read_recent(days=lookback_days)
    new_sop, insights = self.curator.distill_workflows(existing_sop, episodic)
    if not new_sop:
        return {"ok": False, "skipped": True, "reason": "no_sop_output"}
    self.sop_store.overwrite(new_sop)
    log_path = self.sop_store.append_log(new_sop, insights, {...})
    return {"ok": True, "sop_path": ..., "log_path": ..., "insights": insights}
```

- 初始化时 `self.sop_store = SopStore(memory_dir)`。
- 复用现有 `_episodic_lock` 保证与 flush/reindex 的线程安全。

### 4.4 CLI 新增 `/sop [天数]`

在 `cli_channel._dispatch` 加分支，和 `/dream` 平级：
```
/sop        → distill_workflows(lookback_days=3)
/sop 7      → distill_workflows(lookback_days=7)
```
打印结果路径与 insights 摘要。窗口参数可选，默认 3，支持用户滚动拉长。

---

## 5. 改动范围（blast radius）

| 文件 | 改动 | 风险 |
|------|------|------|
| `memory/sop_store.py` | 新增 | 无（新文件） |
| `memory/curator.py` | 加 `WORKFLOW_SOP_SYSTEM_PROMPT` + `distill_workflows` + `parse_sop_output` | 低（纯新增） |
| `memory/manager.py` | 初始化 sop_store + `distill_workflows` 方法 | 低（纯新增，不改现有做梦/检索） |
| `channel/cli/cli_channel.py` | `_dispatch` 加 `/sop` 分支 | 低 |
| `memory/__init__.py` | 如需导出 SopStore | 低 |
| 现有做梦 / 记忆检索 / channel 调度核心 | **不动** | 无 |

---

## 6. 测试矩阵（离线，mock LLM）

| ID | 用例 | 组件 | 级别 |
|----|------|------|------|
| SOP-STORE-1 | 首次 read 空/骨架；overwrite 后 read 得到新内容 | sop_store | P0 |
| SOP-STORE-2 | append_log 生成带时间戳文件，含 sop + insights | sop_store | P0 |
| SOP-CUR-1 | distill_workflows 无 LLM / 无 episodic 返回 ("","") | curator | P0 |
| SOP-CUR-2 | 正常解析 [SOP]/[INSIGHTS] 两段 | curator | P0 |
| SOP-CUR-3 | parse_sop_output 缺段兼容（只有[SOP]、或都缺） | curator | P0 |
| SOP-CUR-4 | 输入带上 existing_sop（增量：prompt 里含旧 SOP） | curator | P0 |
| SOP-MGR-1 | distill_workflows 读旧SOP+episodic→overwrite+写log；无输出时skip | manager | P0 |
| SOP-MGR-2 | 增量迭代：第二次调用把第一次的 SOP 作为输入 | manager | P0 |
| SOP-CLI-1 | /sop 触发 distill_workflows(3)；/sop 7 传 7；打印结果 | cli | P1 |
| SOP-REG-1 | 既有 165 passed 不破 | 全量 | P0 |

---

## 7. 执行顺序

1. `memory/sop_store.py`（SOP-STORE-1/2）
2. `memory/curator.py` 加提示词 + distill + parse（SOP-CUR-1~4）
3. `memory/manager.py` 接线 + distill_workflows（SOP-MGR-1/2）
4. `channel/cli/cli_channel.py` /sop（SOP-CLI-1）
5. 写测试 + 跑全量回归（SOP-REG-1）

---

## 8. 一句话总结

> SOP 提效 = 做梦机制的孪生兄弟：同样"旧成果 + 近期事件 → 新成果"的增量蒸馏，
> 只是产出可固化流程和提效建议，滚雪球式越滚越厚，独立存储不注入对话，手动 /sop 触发、窗口可滚动拉长。
