from channel.message import IncomingMessage
from channel.reply import Reply
from channel.delivery import DeliveryReceipt


class Channel:
    channel_type: str = ""

    def startup(self) -> None:
        raise NotImplementedError  # pragma: no cover

    def send(self, reply: Reply, message: IncomingMessage) -> DeliveryReceipt:
        raise NotImplementedError  # pragma: no cover

    def stop(self) -> None:
        pass
