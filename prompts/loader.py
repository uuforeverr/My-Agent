from pathlib import Path


class PromptLoader:
    def __init__(self, identity_path: Path):
        self.identity_path = identity_path

    def load_identity(self) -> str:
        if not self.identity_path.exists():
            raise FileNotFoundError(f"identity prompt not found: {self.identity_path}")

        content = self.identity_path.read_text(encoding="utf-8").strip()
        if not content:
            raise ValueError(f"identity prompt is empty: {self.identity_path}")

        return content
