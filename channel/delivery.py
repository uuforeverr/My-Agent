from dataclasses import dataclass


@dataclass(frozen=True)
class DeliveryReceipt:
    status: str
    provider_message_id: str = ""
    retryable: bool = False
    ambiguous: bool = False
    error: str = ""

    @property
    def delivered(self) -> bool:
        return self.status == "delivered"

    @property
    def accepted(self) -> bool:
        """Whether the channel accepted responsibility for the delivery.

        ``queued`` is intentionally distinct from ``delivered``: asynchronous
        channels can finish the Agent Turn without pretending the remote API
        has already acknowledged the message.
        """
        return self.status in {"queued", "delivered"}

    def __bool__(self) -> bool:
        return self.accepted

    @classmethod
    def success(cls, provider_message_id: str = "") -> "DeliveryReceipt":
        return cls("delivered", provider_message_id=provider_message_id)

    @classmethod
    def queued(cls, provider_message_id: str = "") -> "DeliveryReceipt":
        return cls("queued", provider_message_id=provider_message_id)

    @classmethod
    def failed(cls, error: str, *, retryable: bool = False) -> "DeliveryReceipt":
        return cls("failed", error=error, retryable=retryable)

    @classmethod
    def unknown(cls, error: str) -> "DeliveryReceipt":
        return cls("unknown", error=error, ambiguous=True)
