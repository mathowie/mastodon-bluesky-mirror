"""Convert Mastodon HTML and media into Bluesky-compatible content."""
import re
from html.parser import HTMLParser
from typing import List, Tuple


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
    def split_into_threads(text: str) -> List[str]:
        """
        Splits text into chunks of MAX_BLUESKY_LENGTH.
        Tries to split at sentence boundaries or spaces to avoid cutting words.
        """
        if len(text) <= ContentTransformer.MAX_BLUESKY_LENGTH:
            return [text]

        threads = []
        while text:
            if len(text) <= ContentTransformer.MAX_BLUESKY_LENGTH:
                threads.append(text)
                break
            
            # Look for the best split point within the limit
            split_idx = ContentTransformer.MAX_BLUESKY_LENGTH
            
            # Try to find a sentence end (. ! ?)
            for char in ['.', '!', '?']:
                idx = text.rfind(char, 0, split_idx)
                if idx > split_idx * 0.7: # Only split if it's a reasonable chunk
                    split_idx = idx + 1
                    break
            
            # If no sentence end, try to find a space
            if split_idx == ContentTransformer.MAX_BLUESKY_LENGTH:
                space_idx = text.rfind(' ', 0, split_idx)
                if space_idx > split_idx * 0.7:
                    split_idx = space_idx
            
            threads.append(text[:split_idx].strip())
            text = text[split_idx:].strip()
            
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
