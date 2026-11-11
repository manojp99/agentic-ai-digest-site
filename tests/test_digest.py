from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import digest

NOW = datetime(2026, 11, 11, 17, tzinfo=timezone.utc)
SOURCE = {"name": "Example", "url": "https://example.com/feed", "domain": "example.com"}


def article(title="Agentic AI systems", **changes):
    item = {
        "title": title, "url": "https://example.com/post",
        "published": NOW.isoformat(), "excerpt": "A guide to reliable AI agents.",
        "source": "Example",
    }
    return dict(item, **changes)


class FeedTests(unittest.TestCase):
    def test_rss_dates_excerpt_and_unsafe_links(self):
        body = b"""<rss><channel>
        <item><title>Agentic &amp; safe</title><link>https://example.com/post?utm_source=feed#x</link>
        <pubDate>Wed, 11 Nov 2026 10:00:00 GMT</pubDate>
        <description>&lt;p&gt;AI agents&lt;/p&gt;&lt;script&gt;bad()&lt;/script&gt;</description></item>
        <item><title>Undated</title><link>https://example.com/x</link></item>
        <item><title>Wrong host</title><link>https://evil.test/post</link><pubDate>Wed, 11 Nov 2026 10:00:00 GMT</pubDate></item>
        </channel></rss>"""
        items, skipped = digest.parse_feed(body, SOURCE)
        self.assertEqual(skipped, 2)
        self.assertEqual(items[0]["url"], "https://example.com/post")
        self.assertEqual(items[0]["excerpt"], "AI agents")
        self.assertEqual(items[0]["title"], "Agentic & safe")

    def test_atom_published_not_updated_and_namespaced_content(self):
        body = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry>
        <title>AI agents</title><link rel="self" href="https://example.com/api"/>
        <link rel="alternate" href="https://example.com/post"/>
        <published>2026-11-10T12:00:00Z</published><updated>2026-11-11T12:00:00Z</updated>
        <summary type="html">&lt;b&gt;Tools&lt;/b&gt; for AI agents</summary></entry></feed>"""
        items, _ = digest.parse_feed(body, SOURCE)
        self.assertEqual(items[0]["published"], "2026-11-10T12:00:00+00:00")
        self.assertEqual(items[0]["url"], "https://example.com/post")
        updated_only = body.replace(b"<published>2026-11-10T12:00:00Z</published>", b"")
        self.assertEqual(digest.parse_feed(updated_only, SOURCE)[0], [])

    def test_literal_html_element_names_in_titles_are_preserved(self):
        body = b"""<rss><channel><item><title>Agent PRs behind &lt;details&gt;</title>
        <link>https://example.com/post</link><pubDate>Wed, 11 Nov 2026 10:00:00 GMT</pubDate>
        <description>Using AI agents</description></item></channel></rss>"""
        self.assertEqual(digest.parse_feed(body, SOURCE)[0][0]["title"], "Agent PRs behind <details>")

    def test_rejects_non_feed_and_entities(self):
        for body in (b"<html></html>", b'<!DOCTYPE rss [<!ENTITY x "x">]><rss/>'):
            with self.assertRaises(digest.DigestError):
                digest.parse_feed(body, SOURCE)

    def test_page_uses_publication_date_not_modification(self):
        body = b"""<html><script type="application/ld+json">{"@graph":[
        {"@type":"Article","headline":"Agent safety","description":"An evaluation guide.",
        "datePublished":"2026-11-09","dateModified":"2026-11-11T00:00:00Z"}]}</script></html>"""
        item = digest.parse_article(body, "https://example.com/post", SOURCE)
        self.assertEqual(item["published"], "2026-11-09T00:00:00+00:00")
        self.assertEqual(item["excerpt"], "An evaluation guide.")

    def test_publisher_index_paths_include_research_and_engineering(self):
        source = dict(SOURCE, kind="page", paths=["/engineering/", "/research/"])
        index = b'<a href="/research/agent-memory">Article</a><a href="https://evil.test/research/x">Outside</a>'
        page = b'<meta property="og:title" content="Agent memory"><meta property="article:published_time" content="2026-11-10T00:00:00Z"><meta name="description" content="AI agent memory design.">'
        with patch("digest.request_bytes", side_effect=[index, page]) as request:
            items, coverage = digest.fetch_source(source, NOW)
        self.assertEqual(len(items), 1)
        self.assertEqual(coverage["status"], "ok")
        self.assertEqual(request.call_count, 2)

    def test_huggingface_community_posts_are_not_first_party(self):
        body = b"""<rss><channel><item><title>Agentic</title><link>https://example.com/blog/user/post</link>
        <pubDate>Wed, 11 Nov 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
        self.assertEqual(digest.parse_feed(body, dict(SOURCE, first_party_only=True))[0], [])


class CurationTests(unittest.TestCase):
    def test_deduplicates_filters_dates_and_recent_picks(self):
        items = [
            article(),
            article(url="https://www.example.com/post/?utm_campaign=x"),
            article(url="https://example.com/old", published=(NOW - timedelta(days=8)).isoformat()),
            article(url="https://example.com/future", published=(NOW + timedelta(seconds=1)).isoformat()),
            article(title="A recipe", excerpt="Cooking vegetables", url="https://example.com/recipe"),
            article(url="https://example.com/seen"),
        ]
        previous = [{"generated_at": (NOW - timedelta(days=1)).isoformat(), "articles": [article(url="https://example.com/seen")]}]
        picks, rejected = digest.select_articles(items, previous, NOW)
        self.assertEqual([item["url"] for item in picks], ["https://example.com/post"])
        self.assertEqual(rejected, {"duplicate": 1, "outside_window": 2, "recently_picked": 1, "irrelevant": 1})

    def test_ranks_relevance_then_freshness_with_source_variety(self):
        items = [article(url=f"https://example.com/{i}", published=(NOW - timedelta(hours=i)).isoformat()) for i in range(12)]
        items.append(article("AI agents and tool calling", source="Other", url="https://other.test/x"))
        picks, _ = digest.select_articles(items, [], NOW)
        self.assertEqual(len(picks), 10)
        self.assertEqual(picks[0]["source"], "Other")
        self.assertEqual(picks[1]["url"], "https://example.com/0")
        self.assertEqual(picks[2]["url"], "https://example.com/1")

    def test_no_padding_and_no_user_agent_false_positive(self):
        picks, _ = digest.select_articles([article("User-agent parsing", excerpt="Set the user agent header")], [], NOW)
        self.assertEqual(picks, [])
        picks, _ = digest.select_articles([article()], [], NOW)
        self.assertEqual(len(picks), 1)

    def test_collect_coverage_failure_and_same_day_rerun(self):
        def good(source, now):
            return [article()], {"name": source["name"], "status": "ok", "articles": 1, "notes": []}
        sources = [SOURCE, dict(SOURCE, name="Second")]
        first = digest.collect(sources, [], NOW, good)
        rerun = digest.collect(sources, [first], NOW + timedelta(hours=1), good)
        self.assertEqual(len(rerun["articles"]), 1)
        with self.assertRaises(digest.DigestError):
            digest.collect([SOURCE], [], NOW, good)

    def test_empty_first_edition_fails_but_later_no_news_is_explicit(self):
        def irrelevant(source, now):
            return [article("Cooking", excerpt="A recipe")], {"name": source["name"], "status": "ok", "articles": 1, "notes": []}
        sources = [SOURCE, dict(SOURCE, name="Second")]
        with self.assertRaises(digest.DigestError):
            digest.collect(sources, [], NOW, irrelevant)
        history = [{"date": "2026-11-10", "generated_at": (NOW - timedelta(days=1)).isoformat(), "articles": []}]
        self.assertEqual(digest.collect(sources, history, NOW, irrelevant)["articles"], [])


class RenderingTests(unittest.TestCase):
    def edition(self):
        picks, rejected = digest.select_articles([article(title='<img src=x onerror="bad()"> Agentic')], [], NOW)
        return {
            "date": "2026-11-11", "generated_at": NOW.isoformat(), "articles": picks,
            "rejected": rejected, "coverage": [{"name": "<script>bad</script>", "status": "error",
            "articles": 0, "error": "<img onerror=bad>", "notes": ["<b>untrusted</b>"]}],
        }

    def test_untrusted_text_is_escaped_and_links_are_checked(self):
        edition = self.edition()
        html = digest.render_digest(edition)
        self.assertNotIn("<img", html)
        self.assertNotIn("<script", html)
        self.assertIn("&lt;img", html)
        self.assertIn("Source excerpt", html)
        self.assertIn("rule-based", html)
        edition["articles"][0]["url"] = "javascript:alert(1)"
        with self.assertRaises(digest.DigestError):
            digest.render_digest(edition)

    def test_archive_links_and_older_pick_label(self):
        edition = self.edition()
        edition["articles"][0]["published"] = (NOW - timedelta(days=3)).isoformat()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            digest.build_site([edition], output)
            self.assertIn("Older pick", (output / "index.html").read_text())
            self.assertIn("../style.css", (output / "archive/2026-11-11.html").read_text())
            self.assertIn("archive/2026-11-11.html", (output / "archive.html").read_text())
            self.assertTrue((output / ".nojekyll").exists())

    def test_url_scheme_credentials_and_host_validation(self):
        for value in ("javascript:alert(1)", "https://me:password@example.com/x", "https://127.0.0.1/x", "https://localhost/x", "https://example.com:9000/x"):
            self.assertIsNone(digest.canonical_url(value))
        self.assertIsNone(digest.allowed_url("https://example.com.evil.test/x", "example.com"))

    def test_failed_refresh_leaves_last_successful_files_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            archived = data / "2026-11-11.json"
            content = json.dumps(self.edition())
            archived.write_text(content)
            output = root / "site"
            output.mkdir()
            page = output / "index.html"
            page.write_text("last successful deployment")
            with patch("sys.argv", ["digest.py", "--data", str(data), "--output", str(output)]), patch(
                "digest.collect", side_effect=digest.DigestError("All sources unavailable")
            ):
                self.assertEqual(digest.main(), 1)
            self.assertEqual(archived.read_text(), content)
            self.assertEqual(page.read_text(), "last successful deployment")


if __name__ == "__main__":
    unittest.main()
