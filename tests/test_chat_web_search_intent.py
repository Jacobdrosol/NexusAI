from control_plane.chat.web_search import _search_query, should_search_web


def test_product_research_prompts_trigger_web_search():
    assert should_search_web("best 3d scanner for 3d printing professionals on a budget") is True
    assert should_search_web("which budget scanner should I buy for tiny gears") is True
    assert should_search_web("compare Revopoint and Creality scanners") is True
    assert should_search_web("recommend a scanner under $800") is True


def test_private_messages_stay_off_the_search_engine():
    assert should_search_web("Help me word a private message to my colleague") is False
    assert should_search_web("best way to word a private message to my boss") is False
    assert should_search_web("draft a confidential email to the buyer") is False


def test_existing_lookup_prompts_still_trigger_web_search():
    assert should_search_web("What is the current market price for this part?") is True
    assert should_search_web("Look up this serial number for me") is True


def test_search_query_keeps_the_product_topic():
    query = _search_query("best 3d scanner for 3d printing professionals on a budget")
    assert "3d scanner" in query.lower()


def test_search_query_strips_meta_instructions_and_citation_requests():
    meta = _search_query(
        "Using only the supplied web results, name two budget 3D scanners for small parts "
        "and include their exact URLs."
    )
    assert "3d scanners" in meta.lower()
    assert "using only" not in meta.lower()
    assert "urls" not in meta.lower()

    citation = _search_query(
        "Best budget 3D scanner for small gears? Answer in one short sentence and include a source URL."
    )
    assert "3d scanner" in citation.lower()
    assert "answer in one short sentence" not in citation.lower()
    assert "source url" not in citation.lower()


def test_research_and_capability_questions_trigger_web_search():
    query = (
        "It should be doing research on 3d scanner cameras or how to test 3d scanning "
        "with an s26 ultra if it is even capable"
    )
    assert should_search_web(query) is True
    cleaned = _search_query(query)
    assert "3d scanner" in cleaned.lower()
    assert "research on" not in cleaned.lower()


def test_research_follow_up_pulls_in_the_prior_user_topic():
    from control_plane.api.chat import _needs_prior_user_research_context

    follow_up = (
        "It should be doing research on 3d scanner cameras or how to test 3d scanning "
        "with an s26 ultra if it is even capable"
    )
    assert _needs_prior_user_research_context(follow_up) is True
    assert (
        _needs_prior_user_research_context(
            "The previous answer was fine and I do not need any additional details "
            "about that topic right now thanks very much."
        )
        is False
    )
