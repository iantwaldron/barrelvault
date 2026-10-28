from barrelvault.tags import add_tags, has_tags, read_tags, remove_tags, stored_tags


def test_the_stored_form_is_lowercase_unique_tokens_in_first_order():
    assert stored_tags("  Deal:Chad   deal:chad  home\tHOME ") == "deal:chad home"
    assert stored_tags("") == ""
    assert stored_tags("   ") == ""
    assert stored_tags(None) == ""


def test_a_read_returns_the_tokens():
    assert read_tags("deal:chad home") == ["deal:chad", "home"]
    assert read_tags("") == []
    assert read_tags(None) == []


def test_an_add_goes_after_the_stored_tokens_and_skips_one_already_there():
    assert add_tags("deal:chad", "Home") == "deal:chad home"
    assert add_tags("deal:chad home", "HOME") == "deal:chad home"
    assert add_tags(None, "home") == "home"


def test_a_remove_compares_in_stored_form_and_skips_one_not_there():
    assert remove_tags("deal:chad home", "HOME") == "deal:chad"
    assert remove_tags("deal:chad", "away") == "deal:chad"
    assert remove_tags(None, "home") == ""


def test_a_filter_needs_every_tag_as_a_whole_token():
    assert has_tags("deal:chad home", "Home deal:chad")
    assert not has_tags("deal:chad home", "home away")
    assert not has_tags("deal:chad", "deal")
    assert has_tags("deal:chad", "")
