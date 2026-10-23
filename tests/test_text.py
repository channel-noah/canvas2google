from canvas_sync.text import html_to_text, strip_control_chars


def test_html_becomes_plain_text_with_paragraph_breaks():
    assert html_to_text("<p>First</p><p>Second</p>") == "First\n\nSecond"


def test_script_and_style_contents_are_dropped():
    html = "<p>Safe</p><script>alert('x')</script><style>p{color:red}</style>"
    assert html_to_text(html) == "Safe"


def test_html_entities_and_nonbreaking_spaces_are_decoded():
    assert html_to_text("Tom&nbsp;&amp;&nbsp;&nbsp;Jerry") == "Tom & Jerry"


def test_markup_is_never_returned():
    text = html_to_text('<a href="javascript:alert(1)" onclick="x()">link</a><img src=x onerror=y>')
    assert text == "link"
    assert "<" not in text and "javascript" not in text


def test_terminal_escape_sequences_are_neutralized():
    # ESC (\x1b) starts terminal control sequences; it must never reach the terminal.
    assert "\x1b" not in strip_control_chars("\x1b[31mred\x1b[0m")
    assert "\x1b" not in html_to_text("<p>\x1b]0;evil title\x07ok</p>")


def test_newlines_and_tabs_are_kept():
    assert strip_control_chars("a\nb\tc\x00d") == "a\nb\tcd"
