"""Dependency-free public-source retrieval and transparent digest curation."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import escape, unescape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
UTC = timezone.utc
PST = timezone(timedelta(hours=-8))
MAX_BYTES = 2_000_000
MAX_FEED_ITEMS = 150
MAX_ARTICLE_PAGES = 12
TERMS = (
    (r"\bagentic\b", 8, "Agentic systems"),
    (r"\bmulti[ -]agent\b", 8, "Multi-agent coordination"),
    (r"\b(?:ai|llm|coding|autonomous|computer.use|software) agents?\b", 7, "AI agents"),
    (r"\bagents?\b", 3, "Agent engineering"),
    (r"\b(?:tool[ -](?:use|calling)|function calling)\b", 6, "Tool use"),
    (r"\b(?:model context protocol|mcp)\b", 6, "Agent interoperability"),
    (r"\b(?:agent|agentic) (?:eval\w*|safety|security)\b", 8, "Agent reliability"),
)
CONTEXT = {
    "Agentic systems": "Relevant to designing systems that plan and act across multiple steps.",
    "Multi-agent coordination": "Relevant to coordinating responsibilities and handoffs between agents.",
    "AI agents": "Relevant to AI systems that take actions, including coding and computer-use workflows.",
    "Agent engineering": "Relevant to implementing agents; consult the original for capabilities and limitations.",
    "Tool use": "Relevant to connecting language models to tools and external actions.",
    "Agent interoperability": "Relevant to connecting agents with external tools and data through shared protocols.",
    "Agent reliability": "Relevant to evaluating and controlling agent behavior before deployment.",
}


class DigestError(Exception):
    pass


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "br", "div", "li"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        if tag in {"p", "div", "li"}:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value, limit=450):
    parser = TextExtractor()
    parser.feed(value or "")
    text = " ".join("".join(parser.parts).split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "..."


def title_text(value):
    return " ".join((value or "").split())[:220]


def canonical_url(value):
    try:
        parts = urlsplit(unescape(value).strip())
        if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
            return None
        if parts.port not in {None, 80, 443}:
            return None
        host = parts.hostname.lower().removeprefix("www.")
        if host == "localhost" or ":" in host or re.fullmatch(r"[\d.]+", host):
            return None
        query = urlencode(sorted(
            (key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in {"ref", "source", "fbclid", "gclid"}
        ))
        return urlunsplit(("https", host, parts.path.rstrip("/") or "/", query, ""))
    except ValueError:
        return None


def allowed_url(value, domain):
    url = canonical_url(value)
    if not url:
        return None
    actual = urlsplit(url).hostname
    return url if actual == domain or actual.endswith("." + domain) else None


def parse_date(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            stamp = parsedate_to_datetime(value.strip())
        except (ValueError, TypeError, OverflowError):
            return None
    # Date-only publisher metadata is explicitly interpreted as midnight UTC.
    if stamp.tzinfo is None and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC) if stamp.tzinfo else None


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def child_text(node, names):
    for child in node:
        if local_name(child.tag) in names:
            return "".join(child.itertext()).strip()
    return ""


def parse_feed(payload, source):
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)", payload, re.I):
        raise DigestError("Prohibited XML document type or entity")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise DigestError(f"Invalid XML feed: {exc}") from exc
    if local_name(root.tag) not in {"rss", "feed", "RDF"}:
        raise DigestError("Response is not an RSS or Atom feed")
    items = []
    skipped = 0
    entries = [node for node in root.iter() if local_name(node.tag) in {"item", "entry"}]
    for node in entries[:MAX_FEED_ITEMS]:
        title = title_text(child_text(node, {"title"}))
        title_node = next((child for child in node if local_name(child.tag) == "title"), None)
        if title_node is not None and title_node.get("type") == "html":
            title = plain_text(title, 220)
        link = child_text(node, {"link"})
        for child in node:
            if local_name(child.tag) == "link" and child.get("rel", "alternate") == "alternate" and child.get("href"):
                link = child.get("href")
                break
        url = allowed_url(urljoin(source["url"], link), source["domain"]) if link else None
        published = parse_date(child_text(node, {"published", "pubDate", "date"}))
        excerpt = plain_text(child_text(node, {"description", "summary", "encoded", "content"}))
        # Hugging Face's feed also includes unvetted community /blog/author/slug posts.
        first_party = not source.get("first_party_only") or (url and len(urlsplit(url).path.strip("/").split("/")) == 2)
        if not title or not url or not published or not first_party:
            skipped += 1
            continue
        items.append({"title": title, "url": url, "published": published.isoformat(),
                      "excerpt": excerpt, "source": source["name"]})
    return items, skipped


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.links = []
        self.structured = []
        self.ld = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            self.meta[attrs.get("property", attrs.get("name", ""))] = attrs.get("content", "")
        elif tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        elif tag == "script" and attrs.get("type") == "application/ld+json":
            self.ld = []

    def handle_data(self, data):
        if self.ld is not None:
            self.ld.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.ld is not None:
            try:
                self.structured.append(json.loads("".join(self.ld)))
            except json.JSONDecodeError:
                self.meta["metadata_warning"] = "Invalid JSON-LD ignored"
            self.ld = None


def structured_articles(value):
    if isinstance(value, list):
        for child in value:
            yield from structured_articles(child)
    elif isinstance(value, dict):
        types = value.get("@type", [])
        if isinstance(types, str):
            types = [types]
        if any(kind in types for kind in {"Article", "BlogPosting", "NewsArticle", "SocialMediaPosting", "TechArticle"}):
            yield value
        if "@graph" in value:
            yield from structured_articles(value["@graph"])


def parse_article(payload, url, source):
    page = PageParser()
    page.feed(payload.decode("utf-8", errors="replace"))
    metadata = next(structured_articles(page.structured), {})
    title = title_text(metadata.get("headline") or page.meta.get("og:title", ""))
    published = parse_date(metadata.get("datePublished") or page.meta.get("article:published_time"))
    excerpt = plain_text(metadata.get("description") or page.meta.get("og:description") or page.meta.get("description", ""))
    return {"title": title, "url": url, "published": published.isoformat() if published else None,
            "excerpt": excerpt, "source": source["name"]}


class SamePublisherRedirects(HTTPRedirectHandler):
    def __init__(self, domain):
        super().__init__()
        self.domain = domain

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://") or not allowed_url(newurl, self.domain):
            raise DigestError("Cross-publisher or non-HTTPS redirect refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def request_bytes(url, domain):
    if not url.startswith("https://") or not allowed_url(url, domain):
        raise DigestError("Request URL is outside the configured HTTPS publisher")
    request = Request(url, headers={
        "User-Agent": "AgenticAIDaily/1.0 (+https://github.com/manojp99/agentic-ai-digest-site)",
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/html;q=0.9",
    })
    with build_opener(SamePublisherRedirects(domain)).open(request, timeout=20) as response:
        payload = response.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise DigestError(f"Response exceeds {MAX_BYTES:,}-byte limit")
    return payload


def relevance(article):
    title = re.sub(r"\b(?:user[ -]agents?|real estate agents?|insurance agents?)\b", "", article["title"].lower())
    body = re.sub(r"\b(?:user[ -]agents?|real estate agents?|insurance agents?)\b", "", article["excerpt"].lower())
    matches = [
        (weight + (3 if re.search(pattern, title) else 0), topic)
        for pattern, weight, topic in TERMS if re.search(pattern, title + " " + body)
    ]
    return (sum(weight for weight, _ in matches), max(matches)[1]) if matches else (0, None)


def recent(article, now):
    published = parse_date(article["published"])
    return published is not None and now - timedelta(days=7) <= published <= now


def fetch_source(source, now):
    coverage = {"name": source["name"], "url": source["url"], "notes": []}
    try:
        payload = request_bytes(source["url"], source["domain"])
        if source["kind"] == "feed":
            items, skipped = parse_feed(payload, source)
            coverage["notes"].append(f"{skipped} entries skipped (missing metadata or outside first-party scope)")
            missing = [item for item in items if not item["excerpt"] and recent(item, now)]
            for item in missing[:MAX_ARTICLE_PAGES]:
                try:
                    detail = parse_article(request_bytes(item["url"], source["domain"]), item["url"], source)
                    item["excerpt"] = detail["excerpt"]
                except (HTTPError, URLError, TimeoutError, OSError, DigestError) as exc:
                    coverage["notes"].append(f"Article access failed: {item['url']}: {str(exc)[:180]}")
        else:
            page = PageParser()
            page.feed(payload.decode("utf-8", errors="replace"))
            links = []
            for link in page.links:
                url = allowed_url(urljoin(source["url"], link), source["domain"])
                if url and urlsplit(url).path.startswith(tuple(source["paths"])) and url not in links:
                    links.append(url)
            if not links:
                raise DigestError("No article links found on publisher index")
            items = []
            for url in links[:MAX_ARTICLE_PAGES]:
                try:
                    item = parse_article(request_bytes(url, source["domain"]), url, source)
                    if all(item.get(key) for key in ("title", "published", "excerpt")):
                        items.append(item)
                    else:
                        coverage["notes"].append(f"Skipped (no title, publication date, or excerpt): {url}")
                except (HTTPError, URLError, TimeoutError, OSError, DigestError) as exc:
                    coverage["notes"].append(f"Article access failed: {url}: {str(exc)[:180]}")
            coverage["notes"].append(f"Inspected {min(len(links), MAX_ARTICLE_PAGES)} index links in publisher order")
        usable = [item for item in items if item["excerpt"]]
        if not items:
            raise DigestError("No dated articles found; publisher metadata may be unavailable")
        if len(usable) < len(items):
            coverage["notes"].append(f"{len(items) - len(usable)} dated entries have no usable excerpt and cannot be selected")
        coverage.update(status="ok", articles=len(items), usable=len(usable))
        return usable, coverage
    except (HTTPError, URLError, TimeoutError, OSError, DigestError) as exc:
        coverage.update(status="error", articles=0, error=plain_text(str(exc), 240))
        return [], coverage


def select_articles(articles, previous, now):
    seen = set()
    for digest in previous:
        generated = parse_date(digest["generated_at"])
        if generated and now - timedelta(days=14) <= generated <= now:
            seen.update(item["url"] for item in digest["articles"])
    candidates = {}
    rejected = {"outside_window": 0, "recently_picked": 0, "irrelevant": 0, "duplicate": 0}
    for article in articles:
        if not recent(article, now):
            rejected["outside_window"] += 1
            continue
        url = canonical_url(article["url"])
        if not url:
            raise DigestError("Unsafe article URL")
        if url in seen:
            rejected["recently_picked"] += 1
            continue
        score, topic = relevance(article)
        if score < 6:
            rejected["irrelevant"] += 1
            continue
        candidate = dict(article, url=url, score=score, topic=topic, context=CONTEXT[topic])
        existing = candidates.get(url)
        if existing:
            rejected["duplicate"] += 1
        if not existing or (candidate["score"], candidate["published"]) > (existing["score"], existing["published"]):
            candidates[url] = candidate
    ranked = sorted(candidates.values(), key=lambda item: (-item["score"], -parse_date(item["published"]).timestamp(), item["url"]))
    picks, deferred, counts = [], [], {}
    for article in ranked:
        if counts.get(article["source"], 0) >= 2:
            deferred.append(article)
        else:
            picks.append(article)
            counts[article["source"]] = counts.get(article["source"], 0) + 1
    return (picks + deferred)[:10], rejected


def load_history(data_dir):
    history = []
    for path in sorted(data_dir.glob("????-??-??.json"), reverse=True):
        try:
            history.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise DigestError(f"Cannot read archived digest {path.name}: {exc}") from exc
    return history


def collect(sources, history, now, fetcher=fetch_source):
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda source: fetcher(source, now), sources))
    articles = [article for batch, _ in results for article in batch]
    coverage = [report for _, report in results]
    successful = sum(report["status"] == "ok" and bool(batch) for batch, report in results)
    if successful < 2 or not articles:
        raise DigestError(f"Insufficient source coverage ({successful}/{len(sources)} sources, {len(articles)} articles). "
                          + "; ".join(f"{r['name']}: {r.get('error', str(r['articles']) + ' parsed')}" for r in coverage))
    day = now.astimezone(PST).date().isoformat()
    previous = [entry for entry in history if entry["date"] != day]
    picks, rejected = select_articles(articles, previous, now)
    if not picks and not history:
        raise DigestError("No eligible articles in the past seven days; refusing to publish an empty initial digest")
    return {"date": day, "generated_at": now.isoformat(), "articles": picks,
            "coverage": coverage, "rejected": rejected}


def render_digest(digest, archive=False):
    cards = []
    generated = parse_date(digest["generated_at"])
    for index, article in enumerate(digest["articles"], 1):
        published = parse_date(article["published"])
        older = '<span class="older">Older pick &middot; over 48 hours</span>' if generated - published > timedelta(hours=48) else ""
        url = canonical_url(article["url"])
        if not url:
            raise DigestError("Unsafe article URL in digest")
        cards.append(f"""<li class="card">
<div class="rank" aria-hidden="true">{index:02d}</div><article>
<div class="meta">{escape(article['source'])} <span aria-hidden="true">&middot;</span>
<time datetime="{escape(article['published'], quote=True)}">{published.strftime('%b %d, %Y')}</time> {older}</div>
<h2><a href="{escape(url, quote=True)}" rel="noopener noreferrer">{escape(article['title'])} <span aria-hidden="true">&#8599;</span></a></h2>
<p class="excerpt"><span class="label">Source excerpt</span>{escape(article['excerpt'])}</p>
<p class="context"><span class="label">Why this topic matters &middot; rule-based</span>{escape(article['context'])}</p>
<span class="topic">{escape(article['topic'])}</span></article></li>""")
    coverage_rows = []
    for report in digest["coverage"]:
        status = f"{report['articles']} dated entries parsed" if report["status"] == "ok" else "Unavailable: " + report["error"]
        notes = "".join(f"<p>{escape(note)}</p>" for note in report["notes"])
        coverage_rows.append(f"<li><strong>{escape(report['name'])}</strong><div>{escape(status)}{notes}</div></li>")
    available = sum(report["status"] == "ok" for report in digest["coverage"])
    prefix = "../" if archive else ""
    empty = '<div class="empty"><h2>No new picks today</h2><p>No unseen articles met the seven-day window and relevance rules. Nothing has been padded or invented.</p></div>' if not cards else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="A daily, source-linked reading list on AI agents from frontier labs and technical blogs.">
<title>Agentic AI Daily &mdash; {escape(digest['date'])}</title><link rel="stylesheet" href="{prefix}style.css"></head>
<body><a class="skip" href="#reading">Skip to articles</a><div class="shell">
<header><a class="brand" href="{prefix}index.html"><span class="mark" aria-hidden="true">A/</span> Agentic AI Daily</a>
<nav aria-label="Main"><a href="{prefix}archive.html">Daily archive</a><a href="https://github.com/manojp99/agentic-ai-digest-site">About &amp; source</a></nav></header>
<main><section class="intro"><p class="eyebrow">THE DAILY READING LIST</p><h1>Less noise.<br>More agency.</h1>
<p class="dek">Original perspectives on AI agents, tools, and the systems that make them useful. Up to ten worthwhile reads, collected in the cloud.</p>
<div class="edition"><strong>{escape(digest['date'])}</strong><span>{len(cards)} reads</span><span>Past 7 days &middot; no padding</span></div></section>
<aside class="freshness" aria-label="Publication status"><strong>{'Archived edition' if archive else 'Latest successful refresh'}</strong>
<time datetime="{escape(digest['generated_at'], quote=True)}">{generated.strftime('%b %d, %Y at %H:%M UTC')}</time>
<span>Daily at 9:00 AM fixed PST / 17:00 UTC. Cloud scheduling may be delayed. If this timestamp is old, check <a href="https://github.com/manojp99/agentic-ai-digest-site/actions">workflow status</a>.</span>
<span><strong>Source coverage: {available} of {len(digest['coverage'])} publishers available.</strong> See access and metadata notes below.</span></aside>
<section id="reading" aria-label="Ranked reading list"><ol class="reading">{''.join(cards)}</ol>{empty}</section>
<section class="method"><h2>A reading list, not an AI news summary.</h2><p>Excerpts are shortened publisher feed or page descriptions, not independently verified reporting or AI-written summaries.
Topic context is a rule-based explanation, not a claim about an article's findings.
Articles must have a publication date within seven days and match agent-related terms.
Relevance, freshness, and source variety determine the order. Picks from the previous 14 days are excluded.
Articles older than 48 hours are labeled. No older backfill is used.</p>
<details><summary>Source coverage &amp; access notes</summary><ul class="coverage">{''.join(coverage_rows)}</ul>
<p>At most 150 feed entries, 12 article pages per source, and 2 MB per response are inspected.
Articles missing publication dates or excerpts are skipped. Hugging Face community posts are excluded.
Date-only publication metadata is interpreted as midnight UTC.
Coverage is limited to the configured publishers, not the entire web. Failed sources are not treated as having no news.</p>
<p>Filtering this refresh: {escape(json.dumps(digest['rejected'], sort_keys=True))}</p></details></section></main>
<footer><span>Built for a calmer reading habit.</span><a href="{prefix}archive.html">Browse previous editions &#8594;</a></footer>
</div></body></html>"""


def build_site(history, output):
    if not history:
        raise DigestError("No successful digests to render")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / "archive"
    archive.mkdir(exist_ok=True)
    ordered = sorted(history, key=lambda digest: digest["date"], reverse=True)
    (output / "index.html").write_text(render_digest(ordered[0]), encoding="utf-8")
    links = []
    for digest in ordered:
        (archive / f"{digest['date']}.html").write_text(render_digest(digest, archive=True), encoding="utf-8")
        links.append(f'<li><a href="archive/{digest["date"]}.html">{digest["date"]}</a><span>{len(digest["articles"])} reads</span></li>')
    (output / "archive.html").write_text(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Daily archive &mdash; Agentic AI Daily</title>
<link rel="stylesheet" href="style.css"></head><body><div class="shell"><header><a class="brand" href="index.html">A/ Agentic AI Daily</a></header>
<main><section class="intro"><p class="eyebrow">THE READING ROOM</p><h1>Daily archive.</h1><p class="dek">Every successful edition, preserved.</p></section>
<ul class="archive">{''.join(links)}</ul></main><footer><a href="index.html">Back to the latest edition</a></footer></div></body></html>""", encoding="utf-8")
    (output / "style.css").write_text((ROOT / "style.css").read_text(encoding="utf-8"), encoding="utf-8")
    (output / ".nojekyll").touch()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=ROOT / "data")
    parser.add_argument("--output", type=Path, default=ROOT / "_site")
    parser.add_argument("--build-only", action="store_true")
    args = parser.parse_args()
    try:
        history = load_history(args.data)
        if args.build_only:
            build_site(history, args.output)
        else:
            sources = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
            digest = collect(sources, history, datetime.now(UTC))
            history = [entry for entry in history if entry["date"] != digest["date"]] + [digest]
            build_site(history, args.output)
            args.data.mkdir(parents=True, exist_ok=True)
            target = args.data / f"{digest['date']}.json"
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(digest, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
            temporary.replace(target)
            print(f"Generated {len(digest['articles'])} real picks for {digest['date']}")
            for report in digest["coverage"]:
                print(f"{report['name']}: {report.get('error', str(report['articles']) + ' dated entries')}")
    except (DigestError, OSError, ValueError) as exc:
        print(f"Digest refresh failed; do not deploy: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
