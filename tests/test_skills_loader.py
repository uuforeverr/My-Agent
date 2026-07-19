from skills.loader import SkillLoader, _escape_xml, _first_paragraph, _split_frontmatter


def test_split_frontmatter_with_metadata():
    metadata, body = _split_frontmatter("---\nname: demo\ndescription: 'desc'\n---\nBody")
    assert metadata == {"name": "demo", "description": "desc"}
    assert body == "Body"


def test_split_frontmatter_without_metadata():
    metadata, body = _split_frontmatter("Body only")
    assert metadata == {}
    assert body == "Body only"


def test_first_paragraph_skips_headings_and_stops_on_blank():
    text = "# Title\n\nFirst line\nsecond line\n\nignored"
    assert _first_paragraph(text) == "First line second line"


def test_escape_xml():
    assert _escape_xml("<a&b>\"'") == "&lt;a&amp;b&gt;&quot;&apos;"


def test_skill_loader_loads_frontmatter_and_builds_prompt(tmp_path):
    skill_dir = tmp_path / "demo"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: demo-skill\ndescription: Demo <skill>\n---\n# Body\n",
        encoding="utf-8",
    )

    loader = SkillLoader(tmp_path)
    skills = loader.load()
    assert len(skills) == 1
    assert skills[0].name == "demo-skill"
    assert skills[0].description == "Demo <skill>"

    prompt, count = loader.build_prompt()
    assert count == 1
    assert "<available_skills>" in prompt
    assert "Demo &lt;skill&gt;" in prompt


def test_skill_loader_fallback_description_and_skip_empty(tmp_path):
    good = tmp_path / "good"
    good.mkdir()
    (good / "SKILL.md").write_text("# Title\n\nUse this skill well.", encoding="utf-8")

    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("# Only title", encoding="utf-8")

    loader = SkillLoader(tmp_path)
    skills = loader.load()
    assert [s.name for s in skills] == ["good"]
    assert skills[0].description == "Use this skill well."


def test_skill_loader_missing_dir():
    loader = SkillLoader(__import__("pathlib").Path("/definitely/not/here"))
    assert loader.load() == []
    assert loader.build_prompt() == ("", 0)
