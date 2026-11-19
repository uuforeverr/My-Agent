# Tool Governance Baseline and Target

Date: 2026-10-14

Status: baseline recorded; target design agreed in principle; implementation not started.

This document freezes the current tool-approval and execution behavior before the
next governance upgrade. After implementation, the same checks and scenarios must
be rerun and the final column completed so the change can be compared objectively.

## 1. Current execution path

```text
model tool call
  -> ToolRegistry resolves the tool
  -> ToolPolicy evaluates a static/dynamic L0-L3 risk level
  -> ApprovalGate may allow, deny, wait, or remember a session-level grant
  -> the tool executes in the My-Agent host process
  -> tool output is returned to the model and recorded in the trace
```

`TOOL_APPROVAL_MODE=off` is the code default. When it is `on`, L0 and L1 run
without approval, L2 and L3 require approval unless the tool explicitly sets
`requires_approval=False`. The repository-local `.env` currently enables approval.

The Bash tool performs an additional application-level check after approval:
`shell=False`, a short executable allowlist, no shell metacharacters, a reduced
environment, a 30-second maximum timeout, and no absolute or `..` path arguments.

## 2. Confirmed baseline problems

1. **Fail-open configuration default.** The code default is approval off, and in
   that mode all declared and unknown risk levels are allowed by `ToolPolicy`.
2. **Approval is not an operating-system boundary.** Once a command is approved,
   it runs with the same Windows account and host filesystem/network privileges as
   My-Agent.
3. **Executable-name allowlisting is not capability confinement.** `python`,
   `node`, `npm`, `npx`, `pip`, `git`, and `pytest` can load code, create child
   processes, access the network, or open files that were not visible in the
   original command string.
4. **Workspace confinement can be bypassed through an interpreter.** A harmless
   temporary-directory probe confirmed that an allowlisted `python -c` command can
   read a sentinel file outside the configured workspace. `BaseTool` path checks
   protect native file tools, not arbitrary code inside Bash.
5. **Session approval is too broad.** `/approve <id> session` records only
   `(session_id, tool_name)`. Approving one Bash command therefore pre-approves
   later Bash commands in the same session regardless of their arguments or
   targets.
6. **Risk is usually tool-level rather than effect-level.** The policy does not
   consistently describe exact resources, destinations, data sensitivity,
   reversibility, external communication, or the difference between draft and
   commit/send/delete operations.
7. **Workspace mutation is unapproved at L1.** `write_file`, `edit_file`, and
   `remember_memory` execute without approval even when approval mode is on. An
   injected instruction can therefore alter project files or create durable memory.
8. **Open-world content is not propagated as untrusted.** `web_search` is L2 but
   explicitly bypasses approval, and its result does not taint later tool calls.
   There is no rule that raises the risk of a write, send, or exec after untrusted
   web/file/message content has been read.
9. **No cross-tool data-flow or egress policy.** The system cannot detect flows
   such as reading private data and then sending it to an external destination.
10. **No sandboxed executor.** Bash and its descendants have no kernel/container
    filesystem, network, process, CPU, or memory boundary.
11. **No executable/operand binding across approval.** Approval does not bind the
    resolved executable identity, file hash, canonical cwd, or script operand and
    revalidate them immediately before launch.
12. **Approval durability is incomplete.** Interactive/session grants are kept in
    process memory, and pending scheduled approvals do not survive restart as a
    resumable authorization state.

## 3. Target governance model

The target separates four concerns that the baseline currently mixes together:

```text
tool availability
  -> effect-aware policy
  -> exact approval grant
  -> OS-constrained executor or typed capability broker
```

- **Tool availability:** expose only tools needed by the current context; deny wins.
- **Effect-aware policy:** evaluate operation, arguments, target resource,
  destination, trust provenance, sensitivity, reversibility, and cost.
- **Exact approval:** bind a one-time grant to canonical arguments, target, cwd,
  resolved executable/script identity, expiry, and user. Do not support a broad
  session grant for Bash or other open-ended interpreters.
- **Sandbox executor:** run arbitrary code and command tools in a separate
  OS/container boundary with only the required workspace view, no credentials,
  default-deny network, and resource limits.
- **Typed capability broker:** keep Feishu, mail, calendar, drive, and similar
  credentials in the trusted host process; expose narrow API operations instead of
  passing credentials or generic network access into the sandbox.
- **Taint and egress checks:** mark web, external files, email, and third-party tool
  results as untrusted; require a fresh exact authorization before they can cause
  sensitive writes, external sends, host execution, or durable-memory changes.
- **Fail closed:** an unavailable sandbox, missing approval surface, unknown risk,
  invalid policy, or revalidation mismatch must deny rather than fall back to host.

## 4. Planned before/after comparison

| Scenario | Current baseline | Target acceptance criterion | Final result |
| --- | --- | --- | --- |
| Approval disabled/misconfigured | Policy allows every risk level | Unknown or invalid configuration denies | Pending |
| Non-allowlisted command | Denied by BashTool | Denied; approval cannot widen a hard deny | Pending |
| `python -c` reads outside workspace | Confirmed possible after Bash approval | OS sandbox blocks the read | Pending |
| Command opens arbitrary network host | Possible from an allowed interpreter | Default denied; only broker/proxy allowlist works | Pending |
| One Bash command approved for session | Later Bash calls pre-approved by tool name | Bash has exact one-time grants only | Pending |
| Executable changes after approval | Not revalidated | Launch denied on identity/hash mismatch | Pending |
| L1 file write after injected content | Executes without approval | Taint-aware policy blocks or asks with exact diff | Pending |
| External content writes durable memory | `remember_memory` can execute directly | Quarantined/validated memory write with provenance | Pending |
| Read sensitive data then send externally | No cross-tool rule | Egress policy requires exact user confirmation | Pending |
| Sandbox unavailable | Not applicable; host execution used | Fail closed unless user explicitly selects host exec | Pending |
| Phoenix audit | Tool/risk/decision recorded | Also records rule, trust, resource, grant, executor and sandbox | Pending |

## 5. Evidence to retain for the final comparison

- Unit tests for every policy decision and approval scope.
- Adversarial tests containing prompt injection in web, file, message, and memory
  content.
- Filesystem escape, symlink, interpreter, subprocess, network egress, executable
  replacement, and restart-recovery tests.
- Phoenix traces showing the selected rule, exact grant, sandbox identity, target
  resources, taint state, execution result, and denial reason.
- A real user flow for read-only work, workspace mutation, an external send, a
  scheduled tool call, and an explicitly elevated host command.

## 6. Related memory-retrieval decision

The next memory index uses a per-user filter before retrieval, real FTS5 BM25 and
embedding-vector candidate lists, and Reciprocal Rank Fusion (RRF) before selecting
the final Top 5. Time decay and importance are intentionally excluded from this
iteration. Vector generation, vector query execution, and optional `sqlite-vec`
acceleration must be observed separately so their latency is not conflated.

## 7. Confirmed scope decisions

- `sqlite-vec` remains optional. The vector-search implementation must retain an
  in-process cosine fallback and expose which backend served each query.
- A local embedding model is loaded once for the process, warmed once after
  startup, and reused. Document embeddings are produced at index/write time;
  one query embedding is produced per new user prompt and reused for that turn.
- The first sandbox iteration implements only the standard workspace sandbox.
  An isolated Git worktree or copy-on-write promotion flow is deferred.
- Because the standard sandbox sees the real workspace, command execution has
  two profiles: `read_only` by default and `workspace_write` only after an
  effect-aware policy decision. Network is denied by default in both profiles.

## 8. Tool execution and approval target matrix

Sandbox placement and human approval are independent decisions. A sandbox
limits what a running process can reach; approval decides whether the intended
effect is authorized.

The matrix below records effect sensitivity and the recommended balanced
behavior. The final interactive behavior is controlled by Section 9:
`always` prompts for every model-initiated tool call, while `never` converts
policy-permitted calls into audited automatic grants. Neither mode changes hard
denies or execution boundaries.

| Tool or operation | Execution boundary | Default approval | Important conditions |
| --- | --- | --- | --- |
| `glob`, ordinary `read_file` | Host typed broker | No | Workspace only; deny secret/protected paths |
| `memory_search`, `read_memory` | Host memory broker | No | Filter by `user_id`; read-only |
| Automatic prompt-time memory recall | Host memory broker | No | Once per user prompt; bounded semantic memory plus RRF Top 5 |
| `web_search` | Host network broker | No for ordinary public queries | Fixed provider; sensitive query content is denied or requires approval; output is marked untrusted |
| `write_file`, `edit_file` on ordinary workspace files | Host typed broker | No | Exact path and diff recorded; protected files require approval |
| `remember_memory` explicitly requested by the user | Host memory broker | No | Store provenance and owning `user_id` |
| Model-inferred durable memory | Host memory broker | Yes or stage as a candidate | Never let untrusted content silently become durable policy |
| `bash` diagnostics/tests/read-only interpreters | Docker sandbox, read-only workspace | No | No secrets, no host home, no network, resource limits |
| `bash` that can mutate the real workspace | Docker sandbox, read-write workspace | Yes, exact and one-time | Bind argv, cwd, target paths and executable identity |
| Dependency installation or networked command | Docker sandbox | Yes | Enable only required destination/network capability |
| Host command outside the sandbox | Host elevated executor | Always; exact and one-time | Disabled by default; no session-wide Bash grant |
| Scheduler `list` and `get` | Host scheduler broker | No | Scope by `user_id` |
| Same-channel fixed reminder explicitly requested by user | Host scheduler broker | No extra prompt | User request is the authorization; echo exact schedule and target |
| Scheduled agent task or scheduled tool call | Host scheduler broker, then normal tool policy at run time | Yes at creation | Creation approval does not bypass future tool policy |
| Scheduler pause/resume/delete | Host scheduler broker | Yes unless it directly answers an explicit current user command | Bind exact task id and owner |
| Read-only office APIs | Host typed capability broker | No | Minimum OAuth scope and per-user resource scope |
| Draft creation | Host typed capability broker | No | Draft only; no external side effect yet |
| Send/publish/invite/share/delete/payment | Host typed capability broker | Yes | Exact destination, content/resource and one-time grant |

Protected workspace targets include credentials and environment files, agent
instructions, skills/plugins, CI/workflow definitions, startup hooks, security
policy, and approval configuration. Writes to those targets are never covered by
the ordinary workspace-write allowance.

The target effect levels are:

- **R0 Observe:** scoped reads and searches; no approval.
- **R1 Reversible local change:** ordinary workspace edits and drafts; no approval
  when they are within the active user request, but fully audited.
- **R2 Durable, external, or capability-widening change:** schedules, durable
  inferred memory, network enablement, dependency installation, sends, shares,
  and protected-file writes; exact approval.
- **R3 Privileged or irrecoverable action:** host execution, credential access,
  privilege changes, broad deletion, payments, or sandbox escape; hard deny by
  default and an explicit elevated path when the product intentionally supports
  the operation.

An active-request allowance may be derived only from the current direct user
message or an explicit approval grant. Retrieved memory, web pages, files, tool
results, and model-generated plans cannot create or widen that authority. If a
frequent command needs write access without repeated prompts, it should become a
narrow typed tool (for example, a formatter over declared files) instead of
receiving a broad persistent Bash grant.

For scheduled work, a creation-time grant authorizes only the stored schedule
and action envelope. When the run later invokes tools, each tool is evaluated
again under its current effect policy. This prevents a harmless scheduled agent
prompt from becoming a durable grant for future arbitrary commands.

## 9. User-selectable approval strategy

The implementation plan adds one explicit configuration value:

```text
TOOL_APPROVAL_MODE=always | never
```

- **`always`:** every model-initiated tool call asks for an exact, one-time user
  approval, including read-only tools. Automatic prompt-time memory recall,
  tracing, health checks, and other internal runtime steps are not tool calls and
  do not prompt. A fixed scheduled reminder is approved when its exact schedule,
  content, and destination are created; an agent/tool action launched by a
  schedule still parks for approval at execution time.
- **`never`:** no interactive approval prompt is emitted. Every operation that
  passes the effect policy is recorded as an automatic grant and proceeds.

`never` disables human prompts only. It does not disable Docker isolation,
`user_id` ownership checks, workspace/path constraints, secret masking, network
defaults, resource limits, taint/egress checks, or hard-deny rules. R3 operations
and unsupported host elevation remain denied unless a separate explicit elevated
capability is enabled. An invalid or missing value fails closed; first-run setup
should default to `always` and require an explicit user choice before switching
to `never`.

For auditability every policy span records the configured mode, the effect level,
the matched rule, whether the grant was `human` or `automatic`, the sandbox
profile, and the final result. This makes the convenience mode observable rather
than silently equivalent to turning policy off.
