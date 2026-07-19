import warnings

# lark_oapi (feishu channel) transitively imports urllib3 v2, which warns on
# macOS system Python built against LibreSSL. Harmless; silence the message so
# it does not clutter the CLI prompt.
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

from channel.manager import ChannelManager
from config import load_settings


def main() -> None:  # pragma: no cover - process entrypoint
    settings = load_settings()
    manager = ChannelManager(settings)
    manager.build_channels()
    print(f"channels: {', '.join(settings.channels)}")
    manager.run()


if __name__ == "__main__":
    main()
