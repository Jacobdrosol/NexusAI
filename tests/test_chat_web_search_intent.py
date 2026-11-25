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
