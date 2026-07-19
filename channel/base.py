from channel.message import IncomingMessage
from channel.reply import Reply


class Channel:
    channel_type: str = ""

    def startup(self) -> None:
        raise NotImplementedError  # pragma: no cover

    def send(self, reply: Reply, message: IncomingMessage) -> None:
        raise NotImplementedError  # pragma: no cover

    def stop(self) -> None:
        pass
