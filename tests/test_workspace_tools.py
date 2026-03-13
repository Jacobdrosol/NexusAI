from control_plane.chat.workspace_tools import _query_terms, build_focus_query, search_workspace_snippets


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


def test_build_focus_query_prioritizes_domain_terms_from_long_prompt():
    query = (
        "Testing repo awareness and proper file searching. "
        "Can you look through everything related to my lesson builder system "
        "and lesson blocks and tell me what is done?"
    )
    focused = build_focus_query(query, max_terms=6)
    terms = focused.split()
    assert "lesson" in terms
    assert "builder" in terms
    assert "blocks" in terms
    assert "awareness" not in terms
    assert "proper" not in terms


def test_search_workspace_snippets_deprioritizes_migrations_and_temp_issue_files(tmp_path):
    migrations = tmp_path / "GlobeIQ.Server" / "Migrations"
    migrations.mkdir(parents=True, exist_ok=True)
    (migrations / "20250907194502_AddLessonContentModeAndRawCode.Designer.cs").write_text(
        "migration for lesson content mode",
        encoding="utf-8",
    )

    temp_issues = tmp_path / "TEMP ISSUE FILES"
    temp_issues.mkdir(parents=True, exist_ok=True)
    (temp_issues / "programming_code_blocks_family.csv").write_text(
        "lesson,blocks,builder",
        encoding="utf-8",
    )

    services = tmp_path / "GlobeIQ.Server" / "Services"
    services.mkdir(parents=True, exist_ok=True)
    (services / "LessonBuilderService.cs").write_text(
        "public class LessonBuilderService { /* lesson block assembly */ }",
        encoding="utf-8",
    )

    hits = search_workspace_snippets(
        tmp_path,
        "lesson builder blocks for course services",
        limit=3,
        max_files=200,
    )
    assert hits
    assert "Migrations" not in hits[0]["path"]
    assert "TEMP ISSUE FILES" not in hits[0]["path"]
    assert hits[0]["path"].endswith("LessonBuilderService.cs")
