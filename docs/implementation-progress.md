# Scheduler and observability implementation progress

This log tracks the implementation approved on 2026-10-14. It is deliberately
kept separate from the eventual user-facing design document.

## Baseline

- Repository constraints checked: no `AGENTS.md` is present.
- Existing uncommitted README, requirements, runtime database, memory and task
  data changes were identified and left intact.
- Test environment initially lacked `pytest`; `pytest 8.4.2` was installed in
  the Python 3.11 environment.
- Baseline suite: 404 passed, 3 skipped, 5 failed before implementation.
  The failures are pre-existing/environmental: `.env` changes the expected
  approval-mode default, two Windows path-normalization assertions, and two
  symlink tests require a Windows privilege that is unavailable in this shell.

## Phase status

- [x] 1. Shared state SQLite foundation and task migration
- [x] 2. APScheduler deadline-driven coordinator
- [x] 3. Durable run queue and decoupled execution
- [x] 4. `notify_at`, `start_at`, and `deliver_by`
- [x] 5. Validation runs and adaptive lead time
- [x] 6. Same-origin delivery receipts and outbox
- [x] 7. Local Phoenix/OpenTelemetry/OpenInference
- [x] 8. Episodic-memory state consolidation and full regression

## Problems and decisions

### 2026-10-14 - Preserve the dirty worktree

The repository already contains user changes and generated runtime data. New
work is being added incrementally; unrelated files will not be reset or
rewritten.

### 2026-10-14 - SQLite journal mode

The installed Python 3.10 and 3.11 runtimes bundle SQLite 3.37.2 and 3.39.4.
Until the runtime is upgraded, the state store uses short transactions, a
process-wide writer lock, foreign keys, and `busy_timeout`, but does not force
WAL mode. Phoenix owns a separate database, so telemetry writes cannot block
the state store.

### 2026-10-14 - Durable scheduling without polling

The 30-second due-task scan was replaced by APScheduler date jobs. SQLite is
the source of truth; APScheduler only holds the next wake-up timers, which are
rebuilt at startup. The scheduling clock, execution pool and delivery pool are
separate so long Agent runs cannot delay timer handling.

Each occurrence has a unique `(task_id, run_kind, scheduled_for)` identity.
Results are recorded before delivery, and a durable outbox tracks confirmed,
retryable, ambiguous and failed sends. Feishu request exceptions are marked
`unknown` rather than retried blindly because the provider send API has no
application idempotency key.

### 2026-10-14 - Time semantics and validation

The tool now supports `notify_at`, `start_at` and `deliver_by`. `deliver_by`
uses successful duration history, a conservative percentile/safety margin,
and per-task min/max bounds. If work is not ready at the deadline, a progress
message is released and the eventual result is delivered afterwards.

New user tasks are saved disabled, immediately executed once, and sent to the
captured originating channel. The formal schedule is enabled only after both
execution and delivery are confirmed. The validation result reports elapsed
time, next formal run and the current lead-time reference.

### 2026-10-14 - Phoenix endpoint and dependency split

The first real OTLP smoke request was sent to the Phoenix root URL and returned
HTTP 405. The adapter now normalizes a base URL to `/v1/traces`. A second smoke
run succeeded and Phoenix's own SQLite contained parent/child Agent and LLM
spans with OpenInference kinds, inputs, outputs, token counts, session id, CPU
time and RSS fields.

The full Phoenix server is intentionally in `requirements-observability.txt`;
the normal runtime only carries the lightweight OTLP/OpenInference client.
Phoenix's SQLite working directory is separate from `data/state.db`. The local
server started for the smoke test was stopped afterwards.

### 2026-10-14 - State consolidation and compatibility

`data/state.db` now contains schedules, occurrences, outbox rows, conversation
selection, episodic watermarks and the memory FTS index. Raw transcripts,
daily summaries and curated memory remain files. Legacy task, conversation and
watermark JSON files are imported non-destructively; standalone MemoryManager
tests retain their file-backed fallback.

The legacy JSON TaskStore remains supported only for explicitly injected test
or extension callers. Production Agent construction always uses the SQLite
repository.

### 2026-10-14 - Verification

- Scheduler/state/channel targeted regression: 86 passed.
- Phoenix/Agent/observability targeted regression: 60 passed.
- Memory/conversation/state targeted regression: 48 passed.
- Final complete suite: 418 passed, 3 skipped, 3 failed.
- Final suite excluding the three confirmed environment-dependent cases:
  418 passed, 3 skipped, 3 deselected.

The remaining failures are not introduced by this work: the repository-local
`.env` sets `TOOL_APPROVAL_MODE=on` while one test expects the unset default,
and two symlink security tests cannot create their fixture because this Windows
account lacks the symbolic-link privilege. A pre-existing Windows path
separator failure in Glob and the memory-index path assertion were fixed.

### 2026-10-14 - Real-model scheduler end-to-end smoke test

A reusable `scripts/e2e_scheduler_smoke.py` test now exercises the complete
production path with the model endpoint configured in `.env`: natural-language
request, LLM tool selection, scheduler task creation, immediate validation,
APScheduler wake-up, SQLite run/outbox persistence and same-origin delivery.
The script isolates its state, memory and workspace beneath `data/e2e/` so the
normal `data/state.db` and user memory are not modified.

The first script invocation failed before any API request because the `agent`
package does not re-export `Agent` and `RunContext`. Direct module imports fixed
the harness; no scheduler change was needed.

The successful run used the real configured `deepseek-v4-flash` endpoint and
asked it to create a `notify_at` task for 30 seconds later. The model called the
`scheduler` tool with the requested fixed message. Validation execution and
validation delivery both succeeded, followed by one formal delivery. The
deadline was `2026-10-14T12:50:56.923246`; the CLI channel observed the formal
message at `2026-10-14T12:50:56.981`, a measured drift of +0.058 seconds. The
scheduled occurrence took 16 ms and both persisted outbox rows were confirmed
as `delivered` on their first attempt.

Evidence is retained in
`data/e2e/20261014-125011-e87995/result.json` and its sibling `state.db`.
Phoenix project `my-agent-e2e` received 16 spans across task creation,
validation and formal execution, including `agent.invoke`, `llm.chat`,
`tool.execute`, `scheduler.execute` and `delivery.send`.

### 2026-10-14 - On-demand Phoenix task correlation

Scheduler lifecycle traces remain independent request/execution traces. They
now share flat Phoenix attributes rather than a long-lived parent trace:
`my_agent.task.id`, `my_agent.run.id`, `my_agent.delivery.id`,
`my_agent.operation`, run kind/timestamps, delivery kind/status, timing
semantics and the originating session id. The Agent root span also exposes the
task ids created during that invocation. Conversation `session.id` is not
assigned to background scheduler traces, so Phoenix Sessions remains focused
on user conversations.

`ToolResult` now carries internal trace metadata, preserved through large
output spilling. Scheduler execution receives the durable occurrence fields
from SQLite, and delivery resolves its run and task before starting the span.
Delivery traces are now also appended to the existing local JSONL/SQLite trace
store instead of being Phoenix-only.

The expanded complete suite produced 421 passes and 3 skips when the three
known environment-dependent tests were deselected. The full unfiltered run had
the same three unrelated failures: repository `.env` approval mode and two
Windows symlink privilege fixtures.

A second real-model run used project `my-agent-correlation-e2e`, task
`41df9648`, and a 30-second `notify_at` reminder. It delivered exactly once at
`2026-10-14T14:10:41.501` for a `2026-10-14T14:10:41.440709` deadline (+0.060
seconds). Filtering Phoenix storage by `my_agent.task.id=41df9648` returned six
correlated spans: Agent root, scheduler-create tool, validation execution and
delivery, then scheduled execution and delivery. Both run ids and both
delivery ids were independently available. Evidence is retained under
`data/e2e/20261014-140955-192b1e/`.

### 2026-10-14 - Personal implementation guide delivered

A user-facing Feishu document now records the completed scheduler and Phoenix
design: `My-Agent 定时任务与观测平台：个人版实现详解`
(`Fhm4dYXEFoYwByxv1ITcK1iKnle`). It explains the creation, SQLite persistence,
immediate validation, APScheduler wake-up, durable run, executor, outbox and
same-origin delivery chain; the Phoenix instrumentation and on-demand
task/run/delivery correlation model; real-test evidence; local operation; and
the relevant code map.

Two embedded whiteboards show the scheduler lifecycle and the observability
relationship. The scheduler board was changed from a wide left-to-right graph
to a compact top-to-bottom graph after preview review; the final server-exported
image has readable text with no clipping or overlap. A final full document
fetch preserved one title, all nine chapters and both embedded whiteboards.
