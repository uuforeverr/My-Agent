import warnings

# lark_oapi (feishu channel) transitively imports urllib3 v2, which warns on
# macOS system Python built against LibreSSL. Harmless; silence the message so
# it does not clutter the CLI prompt.
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

from channel.manager import ChannelManager
from config import load_settings
from observability.otel import configure_observability, shutdown_observability


def main() -> None:  # pragma: no cover - process entrypoint
    settings = load_settings()
    configure_observability(settings)
    try:
        manager = ChannelManager(settings)
        manager.build_channels()
        print(f"channels: {', '.join(settings.channels)}")
        tool_names = sorted(manager.agent.tools.tools)
        print(f"tools: {', '.join(tool_names)}")
        if "web_search" not in tool_names:
            print("web_search: disabled (set a non-empty QIANFAN_API_KEY and restart)")
        manager.run()
    finally:
        shutdown_observability()


if __name__ == "__main__":
    main()
