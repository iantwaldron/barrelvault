from barrelvault.tags import read_tags, stored_tags


def test_the_stored_form_is_lowercase_unique_tokens_in_first_order():
    assert stored_tags("  Deal:Chad   deal:chad  home\tHOME ") == "deal:chad home"
    assert stored_tags("") == ""
    assert stored_tags("   ") == ""
    assert stored_tags(None) == ""


def test_a_read_returns_the_tokens():
    assert read_tags("deal:chad home") == ["deal:chad", "home"]
    assert read_tags("") == []
    assert read_tags(None) == []
