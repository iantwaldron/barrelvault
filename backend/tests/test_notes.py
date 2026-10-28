from barrelvault.notes import append_note, close_open_fence, dropped_block, dropped_reason


def test_an_append_adds_a_blank_line_after_existing_notes():
    assert append_note(None, "first") == "first"
    assert append_note("", "first") == "first"
    assert append_note("first", "second") == "first\n\nsecond"


def test_the_drop_reason_is_the_last_dropped_block():
    notes = append_note("Some context.", dropped_block("Superseded by the Q3 plan."))
    assert notes == "Some context.\n\n```dropped\nSuperseded by the Q3 plan.\n```"
    assert dropped_reason(notes) == "Superseded by the Q3 plan."
    later = append_note(notes, dropped_block("Deal fell through."))
    assert dropped_reason(later) == "Deal fell through."


def test_notes_without_a_dropped_block_have_no_reason():
    assert dropped_reason(None) == ""
    assert dropped_reason("Plain notes.\n\n```python\nprint('hi')\n```") == ""
    assert dropped_reason("```dropped\nnever closed") == ""


def test_a_block_shaped_reason_is_returned_as_text():
    reason = "Copied from the old notes:\n```dropped\nfake\n```\nend"
    block = dropped_block(reason)
    assert block.startswith("````dropped\n")
    assert dropped_reason(append_note("context", block)) == reason


def test_a_dropped_fence_inside_another_block_is_its_text():
    notes = "````markdown\n```dropped\nnot a reason\n```\n````"
    assert dropped_reason(notes) == ""


def test_a_reason_is_trimmed_and_may_span_lines():
    assert dropped_reason(dropped_block("  line one\nline two  ")) == "line one\nline two"


def test_windows_and_old_mac_line_endings_are_read():
    for newline in ("\r\n", "\r"):
        notes = newline.join(["```py", "x", "```", "```dropped", "Deal fell through.", "```"])
        assert dropped_reason(notes) == "Deal fell through."


def test_a_fence_closes_with_indent_trailing_spaces_or_a_longer_run():
    assert dropped_reason("```dropped\nr\n```  ") == "r"
    assert dropped_reason("   ```dropped\nr\n   ```") == "r"
    assert dropped_reason("```dropped\nr\n`````") == "r"
    assert dropped_reason("````dropped\nr\n```\nstill r\n````") == "r\n```\nstill r"
    assert dropped_reason("    ```dropped\nindented four is code, not a fence\n```") == ""


def test_an_unclosed_fence_is_closed_before_a_drop_appends():
    notes = "x\n```\nnever closed"
    closed = close_open_fence(notes)
    assert closed == "x\n```\nnever closed\n```"
    assert dropped_reason(append_note(closed, dropped_block("superseded"))) == "superseded"
    assert close_open_fence("plain") == "plain"
    assert close_open_fence(None) == ""
