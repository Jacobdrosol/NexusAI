from control_plane.chat.workspace_tools import _query_terms, search_workspace_snippets


def test_query_terms_filters_generic_stop_terms():
    query = (
        "Testing to ensure you have connection to the repo and can read files and context "
        "for authentication middleware hardening."
    )
    terms = _query_terms(query, max_terms=8)
    assert "testing" not in terms
    assert "repo" not in terms
    assert "read" not in terms
    assert "files" not in terms
    assert "context" not in terms
    assert "authentication" in terms
    assert "middleware" in terms
    assert "hardening" in terms


def test_search_workspace_snippets_prioritizes_code_paths(tmp_path):
    docs_path = tmp_path / "docs" / "timeline"
    docs_path.mkdir(parents=True, exist_ok=True)
    (docs_path / "status.md").write_text("AUTH_SIGNAL_TOKEN appears in documentation", encoding="utf-8")

    src_path = tmp_path / "src" / "services"
    src_path.mkdir(parents=True, exist_ok=True)
    (src_path / "auth_service.ts").write_text("export const AUTH_SIGNAL_TOKEN = true;", encoding="utf-8")

    hits = search_workspace_snippets(
        tmp_path,
        "Review AUTH_SIGNAL_TOKEN authentication hardening",
        limit=2,
        max_files=50,
    )
    assert hits
    assert hits[0]["path"].startswith("src/")

