import pytest

from prompts.loader import PromptLoader


def test_load_identity_success(tmp_path):
    path = tmp_path / "identity.md"
    path.write_text("  You are test.  ", encoding="utf-8")
    assert PromptLoader(path).load_identity() == "You are test."


def test_load_identity_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        PromptLoader(tmp_path / "missing.md").load_identity()


def test_load_identity_empty(tmp_path):
    path = tmp_path / "identity.md"
    path.write_text("   ", encoding="utf-8")
    with pytest.raises(ValueError):
        PromptLoader(path).load_identity()
