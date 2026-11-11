# Agentic AI Daily

A calm, public reading page for original articles on AI agents:
**https://manojp99.github.io/agentic-ai-digest-site/**

GitHub Actions collects, curates, archives, and publishes the page entirely in
the cloud. Your laptop does not need to be on. No paid API, model credentials,
third-party summarization service, or local production server is used.

## Schedule and operation

The [daily workflow](../../actions/workflows/digest.yml) runs at `0 17 * * *`:
**09:00 fixed PST (UTC-8), year-round**. This is 10:00 in Pacific daylight time,
not 09:00 America/Los_Angeles year-round. GitHub-hosted scheduling is approximate
and may be delayed or, under heavy load, dropped. Run it manually through
Actions > Daily Agentic AI digest > Run workflow.

GitHub Pages must use **GitHub Actions** as its deployment source. The workflow
runs only on `main`, is serialized, and has no push trigger (archive commits
cannot create loops). Collection has read-only repository access; a separate
job has `contents: write` solely to commit daily JSON archives; deployment has
only `pages: write` and `id-token: write`. The `github-pages` environment is used.
Actions are pinned to immutable commits. If `main` advances during collection,
the archive push fails safely rather than overwriting concurrent changes; start
a fresh manual run.
Do not require PRs for the workflow's archive commits without first configuring
an appropriate approved bypass or a different persistence mechanism.

If retrieval, tests, archive persistence, or deployment fails, the previously
deployed page remains online. The visible timestamp is the **last successful
collection**, not a promise that today's schedule ran. Access failures appear in
source coverage; other failures appear in the Actions run log. Standard GitHub
Actions failure notifications can be enabled in your account notification settings.
GitHub may disable scheduled workflows in public repositories after 60 days of
inactivity; monitor Actions and re-enable the schedule if necessary.

## Sources and curation

Edit [`sources.json`](sources.json) to configure the original publishers:
OpenAI, Anthropic engineering, Google DeepMind, Microsoft Research,
Hugging Face (first-party blog posts only), LangChain, LlamaIndex,
and Simon Willison. RSS/Atom feeds are preferred; publisher index pages and
structured article metadata are used where a working official feed is unavailable.
No access restriction is bypassed. A publisher may block GitHub runners or
change its markup; that failure is reported rather than silently treated as
an empty source. At least two sources with dated excerpts must succeed.

The digest selects **up to ten**, not necessarily ten:

- Publication must be within the past seven days, never in the future.
- Missing publication dates or descriptions are skipped; modification dates
  are not substituted. Date-only publication metadata means midnight UTC.
- Agent-related keyword relevance ranks first, publication recency second.
  Up to two picks per source are placed before additional picks from that source.
- Canonical URLs strip tracking parameters, fragments, and trailing slashes.
  Picks in the preceding 14 days are excluded. Same-day reruns replace that
  edition without suppressing its own picks.
- Picks older than 48 hours are labeled; there is no older backfill or padding.
  The first edition fails if no real picks are available. Later editions may
  explicitly report no unseen eligible articles.

**These are publisher excerpts, not AI-written summaries.** Excerpts are
shortened feed text or publisher page descriptions. “Why this topic matters”
is labeled rule-based context, not a claim about an article's findings.
This intentionally avoids unverified model access, invented summaries, and
paid credentials. Keyword curation cannot guarantee editorial quality, and
coverage is not exhaustive; read the linked originals.

Retrieval uses four source workers, a 20-second socket timeout, 2 MB per
response (also after gzip decompression), at most 150 entries per feed, and at most 12 article pages per
source. Page sources inspect links in publisher order; this is a bounded
sample, not a complete crawl. The collection job has a 15-minute timeout.
Redirects and article links must stay on the configured publisher's HTTPS
domain. All external text is rendered escaped; there is no client-side script
or remote font dependency. Daily JSON in `data/` preserves the browsable archive
and repeat history in the repository.

## Local development

Python 3.12+ with its standard library is sufficient; no install step is needed.

```sh
python3 -m unittest discover -s tests -v
python3 digest.py               # fetch live sources, save data, build _site/
python3 digest.py --build-only  # rebuild from the saved archive, no network
python3 -m http.server 8000 --directory _site
```

On macOS, if your Python installation does not find a trusted CA bundle,
configure its certificate store or point `SSL_CERT_FILE` to your system CA
bundle (for example `/etc/ssl/cert.pem`). Never disable TLS verification.
