from dataclasses import dataclass


@dataclass
class Reply:
    content: str
    type: str = "text"
