"""Readable thread boundaries without losing text, links, or punctuation."""
import html

import pytest

from app.core.transformer import ContentTransformer
from test_sync_regressions import bluesky_client, db, status, syncer


CONTEXT = ("Useful context " * 15).strip()
FOLLOWUP = ("The next paragraph continues the discussion. " * 5).strip()


@pytest.mark.parametrize("quote", [
    '"A complete quoted question?"',
    '“A complete quoted question?”',
    '‘A complete quoted question?’',
    '«A complete quoted question?»',
    '(A complete parenthetical sentence.)',
    '[A complete aside!]',
    '(“A complete nested quotation!”)',
])
def test_closing_punctuation_stays_with_its_sentence(quote):
    first = CONTEXT + "\n\n" + quote
    parts = ContentTransformer.split_into_threads(first + "\n\n" + FOLLOWUP)
    assert parts == [first, FOLLOWUP]


def test_latest_sentence_boundary_wins_regardless_of_punctuation_type():
    first = "a " * 105 + "done. Another clear sentence ends!"
    following = ("There is more to say here " * 8).strip()
    assert ContentTransformer.split_into_threads(first + " " + following) == [first, following]


def test_a_nearby_paragraph_break_is_preferred_to_a_later_sentence():
    paragraph = ("A paragraph with useful context. " * 8).strip()
    following = "The next paragraph starts here. " + "more words " * 20
    parts = ContentTransformer.split_into_threads(paragraph + "\n\n" + following.strip())
    assert parts == [paragraph, following.strip()]


@pytest.mark.parametrize("token", [
    "https://long.subdomain.example/path/to/story?topic=threads&from=reader",
    "12345.6789",
])
def test_periods_inside_a_token_are_not_sentence_boundaries(token):
    text = "word " * 45 + token + " " + "following words " * 15
    parts = ContentTransformer.split_into_threads(text.strip())
    assert any(token in part for part in parts)
    assert " ".join(parts).split() == text.split()
    assert all(len(part) <= 300 for part in parts)


def test_a_long_token_that_fits_a_post_is_moved_whole():
    token = "x" * 295
    assert ContentTransformer.split_into_threads("Brief introduction. " + token) == [
        "Brief introduction.", token,
    ]


def test_newlines_can_separate_words_without_cutting_them():
    text = ("example\n" * 45).strip()
    parts = ContentTransformer.split_into_threads(text)
    assert " ".join(parts).split() == text.split()
    assert all(0 < len(part) <= 300 for part in parts)


@pytest.mark.parametrize("opener", ['"', "“", "(", "["])
def test_an_opening_delimiter_is_not_left_on_the_previous_post(opener):
    first = ("word " * 58).strip()
    following = opener + " " + "continuation" * 2
    assert ContentTransformer.split_into_threads(first + " " + following) == [first, following]


def test_a_closing_quote_just_over_the_limit_moves_with_its_word():
    first = ("word " * 56).strip()
    quoted = '"abcdefghijklmnopqrs?"'
    text = first + " " + quoted + "\n\n" + FOLLOWUP
    parts = ContentTransformer.split_into_threads(text)
    assert all(0 < len(part) <= 300 for part in parts)
    assert any(quoted in part for part in parts)
    assert not any(part.startswith('"\n') for part in parts)


@pytest.mark.parametrize("text", ["", "Short text.", "a" * 300])
def test_text_within_the_limit_is_unchanged(text):
    assert ContentTransformer.split_into_threads(text) == [text]


def test_an_overlong_unbroken_token_makes_progress_without_losing_characters():
    text = "x" * 901
    parts = ContentTransformer.split_into_threads(text)
    assert "".join(parts) == text
    assert all(0 < len(part) <= 300 for part in parts)


@pytest.mark.parametrize("quote", ['"', "'"])
def test_quote_context_is_retained_across_more_than_two_posts(quote):
    text = quote + "word " * 120 + quote + " tail"
    parts = ContentTransformer.split_into_threads(text)
    assert len(parts) == 3
    assert parts[-1] == "word " + quote + " tail"
    assert " ".join(parts).split() == text.split()
    assert all(0 < len(part) <= 300 for part in parts)


@pytest.mark.parametrize("quote", ['"', "'"])
def test_a_spaced_closing_quote_does_not_force_a_word_split(quote):
    token = "x" * 295
    parts = ContentTransformer.split_into_threads(quote + "Intro " + token + " " + quote + " tail")
    assert parts == [quote + "Intro", token + " " + quote, "tail"]


@pytest.mark.parametrize("punctuation", [".", '"', ")"])
def test_a_long_punctuation_token_does_not_create_tiny_posts(punctuation):
    text = punctuation * 500
    parts = ContentTransformer.split_into_threads(text)
    assert len(parts) == 2
    assert "".join(parts) == text
    assert all(0 < len(part) <= 300 for part in parts)


def test_published_thread_keeps_quotes_and_reply_relationships(db, monkeypatch):
    first = CONTEXT + '\n\n"A complete quoted question?"'
    source = status(content="<p>" + html.escape(first).replace("\n\n", "</p><p>")
                    + "</p><p>" + html.escape(FOLLOWUP) + "</p>")
    bsky, records = bluesky_client(monkeypatch)
    runner = syncer(db, bsky, [source])
    runner.run_sync_cycle()
    assert [record.text for record in records] == [first, FOLLOWUP]
    assert records[1].reply.root.uri == records.uris[0]
    assert records[1].reply.parent.uri == records.uris[0]
    runner.run_sync_cycle()
    assert len(records) == 2
