"""Convert Mastodon HTML and media into Bluesky-compatible content."""
import re
from html.parser import HTMLParser
from typing import List, Tuple


_CLOSERS = "\"'”’»›)]}"
_OPENERS = "“‘«‹([{"
_TRAILING_PUNCTUATION = _CLOSERS + ".!?…,:;"
_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")
_SENTENCE_END = re.compile(r"""[.!?…]+["'”’»›)\]}]*(?=\s|$)""")
_CLAUSE_END = re.compile(r"""[,;:—–]["'”’»›)\]}]*(?=\s|$)""")
_WHITESPACE = re.compile(r"\s+")


def _quote_is_open(text: str, index: int, quote: str) -> bool:
    """Track straight quotes across parts, ignoring apostrophes inside words."""
    prefix = text[:index]
    count = prefix.count('"') if quote == '"' else len(re.findall(r"(?<!\w)'|'(?!\w)", prefix))
    return count % 2 == 1


def _safe_thread_break(text: str, index: int, start: int) -> bool:
    """Keep opening delimiters with the next word and loose closers behind."""
    left = text[start:index].rstrip()
    if not left or left[-1] in _OPENERS:
        return False
    if left[-1] in "\"'" and (len(left) == 1 or left[-2].isspace() or left[-2] in _OPENERS):
        if _quote_is_open(text, index, left[-1]):
            return False
    right = text[index:].lstrip()
    if right:
        token = right.split(None, 1)[0]
        if all(char in _TRAILING_PUNCTUATION for char in token):
            # A straight quote can also open the next phrase.
            if token in {'"', "'"}:
                return not _quote_is_open(text, index, token)
            return False
    return True


def _thread_break_index(text: str, limit: int, start: int) -> int:
    # Prefer natural boundaries in the last 30% of the available space.
    # Sentence matches include closing quotes/brackets and require a word end,
    # so a decimal or a dot/question mark inside a URL is not a sentence break.
    minimum = start + max(1, int(limit * 0.7))
    end = start + limit
    for pattern, before in ((_PARAGRAPH_BREAK, True), (_SENTENCE_END, False), (_CLAUSE_END, False)):
        best = None
        for match in pattern.finditer(text, start):
            index = match.start() if before else match.end()
            if index > end:
                break
            if index >= minimum and _safe_thread_break(text, index, start):
                best = index
        if best is not None:
            return best

    # Even an early word boundary is better than cutting a word or URL that
    # would fit whole on the next post. Newlines and tabs are boundaries too.
    best = None
    for match in _WHITESPACE.finditer(text, start):
        index = match.start()
        if index > end:
            break
        if _safe_thread_break(text, index, start):
            best = index
    if best is not None:
        return best

    # An unbroken token longer than the limit has to be split. Leave at least
    # one character with nearby closing punctuation, but do not retreat through
    # a whole punctuation-only token and turn it into many tiny posts.
    index = end
    while index > minimum and text[index] in _TRAILING_PUNCTUATION:
        index -= 1
    return index if text[index] not in _TRAILING_PUNCTUATION else end


class _PostTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "br":
            self.parts.append("\n")
        elif tag in {"p", "div", "blockquote"} and self.parts:
            self.parts.append("\n\n")
        elif tag == "a":
            self.links.append((attrs.get("href", ""), len(self.parts), attrs.get("class", "")))

    def handle_endtag(self, tag):
        if tag in {"p", "div", "blockquote"}:
            self.parts.append("\n\n")
        elif tag == "a" and self.links:
            href, start, classes = self.links.pop()
            label = "".join(self.parts[start:])
            if (href.startswith(("https://", "http://")) and href != label
                    and "mention" not in classes and not label.startswith("#")):
                self.parts.append(f" ({href})")

    def handle_data(self, data):
        self.parts.append(data)


class ContentTransformer:
    MAX_BLUESKY_LENGTH = 300

    @staticmethod
    def clean_text(text: str) -> str:
        parser = _PostTextParser()
        parser.feed(text or "")
        parser.close()
        return re.sub(r"\n{3,}", "\n\n", "".join(parser.parts)).strip()

    @staticmethod
    def split_into_threads(text: str, max_length: int | None = None) -> List[str]:
        """Split within the limit, preferring paragraphs and complete sentences."""
        limit = ContentTransformer.MAX_BLUESKY_LENGTH if max_length is None else max_length
        if type(limit) is not int or limit < 1:
            raise ValueError("Thread length budget must be a positive integer")
        if len(text) <= limit:
            return [text]

        threads = []
        start = 0
        while start < len(text):
            if len(text) - start <= limit:
                threads.append(text[start:])
                break

            split_idx = _thread_break_index(text, limit, start)
            threads.append(text[start:split_idx].strip())
            # Keep the original text so quote pairing can see preceding parts.
            start = split_idx
            while start < len(text) and text[start].isspace():
                start += 1

        return threads

    @staticmethod
    def process_media(mastodon_status) -> List[Tuple[str, str]]:
        images = []
        for media in mastodon_status.get("media_attachments", []):
            if media.get("type") != "image":
                continue
            if not media.get("url"):
                raise ValueError("Image attachment has no downloadable URL")
            images.append((media["url"], media.get("description") or ""))
            if len(images) == 4:
                break
        return images
