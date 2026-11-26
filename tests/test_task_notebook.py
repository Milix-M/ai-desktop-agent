import pytest

from ai_desktop_agent.agent.notebook import SourceLibrary, TaskNotebook


def test_notes_update_without_replaying_every_body():
    notes = TaskNotebook()
    notes.save("decision", "preliminary")
    notes.save("deadline", "Thursday")
    notes.save("decision", "approved")
    assert "preliminary" not in notes.context()
    assert "Thursday" not in notes.context()
    notes.recall("deadline")
    assert "Thursday" in notes.context()
    assert list(notes.entries) == ["decision", "deadline"]


def test_passages_survive_app_switch_with_pagination_and_document_updates():
    sources = SourceLibrary()
    sources.remember("a", "Meeting", ["Topic " + "x" * 4000, "Other"], False)
    sources.remember("b", "Meeting", ["Different revision"], True)
    sources.focused = ""
    sources.extract(1, "topic", 0)
    assert "offset=3500" in sources.context()
    assert "Other" not in sources.context()
    sources.extract(1, "topic", 3500)
    assert "offset=" not in sources.selected
    sources.remember("a", "Meeting", ["Signed decision"], False)
    assert not sources.selected
    sources.extract(1, "signed", 0)
    assert "Signed decision" in sources.selected
    with pytest.raises(ValueError):
        sources.extract(3, "", 0)


def test_batch_preview_keeps_prior_sources_and_groups_repeated_passages():
    library = SourceLibrary()
    library.remember("file:first", "first", ["Shared", "Review by Tuesday"], False)
    library.remember("file:second", "second", ["Shared", "Review by Friday"], False)
    preview = library.file_preview({"second": "Shared\nReview by Friday"})
    assert preview.count("Shared") == 1
    assert "[second, first] Shared" in preview
    assert "Review by Tuesday" in preview
    library.extract(0, "Friday", 0)
    assert "second" in library.selected
    assert "Tuesday" not in library.selected
