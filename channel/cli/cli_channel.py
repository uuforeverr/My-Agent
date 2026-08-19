import json
import uuid

from channel.chat_channel import ChatChannel
from channel.message import IncomingMessage
from channel.reply import Reply
from observability import TraceQuery, format_trace_tree, summarize_trace_record
from tools.approval import handle_approval_command

STOP = "stop"
CONTINUE = "continue"
PRODUCED = "produced"


class CLIChannel(ChatChannel):
    channel_type = "cli"
    user_id = "local"

    def startup(self) -> None:  # pragma: no cover - interactive stdin loop, excluded from offline tests
        self.start_consuming()
        print("my-agent CLI")
        print(f"workspace: {self.settings.workspace}")
        print("Type 'exit' to quit. '/trace' shows latest trace. '/traces' lists persisted traces.")
        while True:
            try:
                line = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if self._dispatch(line) == STOP:
                break

    def _dispatch(self, line: str) -> str:
        if not line:
            return CONTINUE
        if line.lower() in {"exit", "quit"}:
            return STOP
        if line == "/trace":
            if self.last_trace is None:
                print("No trace yet.")
            else:
                print(self.last_trace.to_json(pretty=True))
            return CONTINUE
        if line.startswith("/trace "):
            trace_id = line.split(maxsplit=1)[1].strip()
            self._print_persisted_trace(trace_id)
            return CONTINUE
        if line == "/traces" or line.startswith("/traces "):
            query = self._parse_traces_args(line)
            self._print_trace_list(query=query)
            return CONTINUE
        if line == "/dream":
            result = self.agent.memory.deep_dream(lookback_days=1)
            if result.get("ok"):
                print(f"dream done. diary: {result.get('dream_path') or '(no diary)'}")
            else:
                print(f"dream skipped: {result.get('reason', 'unknown')}")
            return CONTINUE
        if line == "/new":
            cid = self.start_new_conversation(self.user_id)
            print(f"🌱 new conversation: {cid}")
            return CONTINUE
        if line == "/summarize":
            summarize = getattr(self.agent, "summarize_session", None)
            if callable(summarize):
                cid = self.current_conversation(self.user_id)
                print(summarize(f"{self.channel_type}:{self.user_id}:{cid}"))
            else:
                print("无需压缩")
            return CONTINUE
        handled = handle_approval_command(self.agent, line, self.user_id)
        if handled is not None:
            print(handled)
            return CONTINUE
        if line == "/sop" or line.startswith("/sop "):
            days = self._parse_sop_days(line)
            result = self.agent.memory.distill_workflows(lookback_days=days)
            if result.get("ok"):
                print(f"sop distilled (last {days}d). file: {result.get('sop_path')}")
                insights = (result.get("insights") or "").strip()
                if insights:
                    print(f"\ninsights:\n{insights}")
            else:
                print(f"sop skipped: {result.get('reason', 'unknown')}")
            return CONTINUE

        self.produce(IncomingMessage(
            channel_type=self.channel_type,
            user_id=self.user_id,
            content=line,
            msg_id=uuid.uuid4().hex,
            conversation_id=self.current_conversation(self.user_id),
        ))
        return PRODUCED

    def send(self, reply: Reply, message: IncomingMessage) -> None:
        print(f"\n{reply.content}")

    def _print_persisted_trace(self, trace_id: str) -> None:
        record = self.trace_store.get(trace_id)
        if record is None:
            print(f"Trace not found: {trace_id}")
            return
        summary = summarize_trace_record(record)
        print(
            f"trace {summary['trace_id']} "
            f"status={summary['status']} total_ms={summary['total_ms']} "
            f"spans={summary['span_count']}"
        )
        print("\nspan tree:")
        print(format_trace_tree(record))
        print("\nraw:")
        print(json.dumps(record, ensure_ascii=False, indent=2))

    def _print_trace_list(self, query: TraceQuery = None) -> None:
        query = query or TraceQuery()
        records = self.trace_store.query_records(query)
        if not records:
            print("No persisted traces yet.")
            return
        print("created_at           status   total_ms   spans  trace_id                              root")
        for record in records:
            summary = summarize_trace_record(record)
            trace_id = summary["trace_id"]
            short_id = trace_id[:36]
            total_ms = summary["total_ms"]
            total_text = "-" if total_ms is None else str(total_ms)
            print(
                f"{summary['created_at'][:19]:19} "
                f"{summary['status'][:7]:7} "
                f"{total_text:>8} "
                f"{str(summary['span_count']):>5}  "
                f"{short_id:36} "
                f"{summary['root_name']}"
            )

    @staticmethod
    def _parse_traces_args(line: str) -> TraceQuery:
        limit = 10
        filters = {
            "before": None,
            "status": None,
            "session_id": None,
            "entrypoint": None,
            "start": None,
            "end": None,
        }
        for part in line.split()[1:]:
            if "=" in part:
                key, value = part.split("=", 1)
                if key in filters:
                    filters[key] = value.strip() or None
                continue
            try:
                parsed = int(part)
            except ValueError:
                continue
            if parsed > 0:
                limit = parsed
        return TraceQuery(limit=limit, **filters)

    @staticmethod
    def _parse_sop_days(line: str, default: int = 3) -> int:
        parts = line.split()
        if len(parts) < 2:
            return default
        try:
            days = int(parts[1])
        except ValueError:
            return default
        return days if days > 0 else default
