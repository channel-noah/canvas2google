"""Turn untrusted feed text into safe plain text. Nothing here renders or executes HTML."""

import re
import unicodedata
from html.parser import HTMLParser

_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "blockquote", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6",
}
_SKIP_TAGS = {"script", "style"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def strip_control_chars(text: str) -> str:
    """Remove control characters (except newline/tab).

    Feed text is printed to your terminal, and escape sequences hidden in a title
    could otherwise recolor, rewrite or hide terminal output.
    """
    return "".join(
        ch for ch in text if ch in "\n\t" or unicodedata.category(ch) != "Cc"
    )


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = strip_control_chars("".join(parser.parts))
    text = re.sub(r"[ \t ]+", " ", text)  #   is a non-breaking space (&nbsp;)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
