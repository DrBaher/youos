#!/usr/bin/env python3
"""Render YouOS markdown docs as a public docs subsite.

Run during the GitHub Pages workflow after the landing page is assembled.
Produces:

  _site/docs/index.html                — index of all docs
  _site/docs/<NAME>.html               — rendered HTML, styled to match landing
  _site/docs/<NAME>.md                 — raw markdown (LLM agents can fetch this)
  _site/robots.txt                     — keep /docs/ crawlable
  _site/llms.txt                       — convention for LLM agents (top-level
                                          discovery; points at AGENT_OPERATIONS.md)

The two main audiences:

  * **Humans** load the .html and read with the landing-page palette.
  * **LLM-driven agents** (Hermes, OpenClaw, anything calling YouOS via REST)
    fetch the .md to seed their tool-use context. The .md is the canonical
    form — links inside use relative paths that work in both modes.

Why a build script instead of Jekyll: Jekyll would work but the docs are
short, the rendering needs are simple, and an inline build script keeps the
publish step explicit + dependency-light (just ``python-markdown``).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import markdown
except ImportError:
    print("error: python-markdown not installed. `pip install markdown`", file=sys.stderr)
    sys.exit(1)


# Order = rendered order on the index page. Each entry: (filename, title, blurb).
DOCS: list[tuple[str, str, str]] = [
    (
        "THE_WIRE.md",
        "The Wire — newsletter digest setup",
        "Turn the day's newsletters across your accounts into one Gmail-safe HTML "
        "email — every story extracted, deduped across sources, grouped by theme — "
        "then archive the originals. Preview, configure, gate, and schedule it.",
    ),
    (
        "AGENT_OPERATIONS.md",
        "Agent operations playbook",
        "Runtime contract for LLM-driven orchestrators operating YouOS — decision "
        "tree, idempotency, error handling, paraphrasing, trust boundaries, "
        "worked end-to-end conversation. Start here if you're driving YouOS "
        "from Hermes/OpenClaw/Claude/a chat bot.",
    ),
    (
        "INTEGRATIONS.md",
        "Integrations — orchestrator backend",
        "Architecture diagram, setup recipe (Tailscale + token-create), endpoint "
        "reference table, security model, ~30-line Telegram bot example. The "
        "wiring side of the orchestrator vision.",
    ),
    (
        "REMOTE_ACCESS.md",
        "Remote access — phone / Tailscale / multi-account",
        "Reach YouOS from your phone via Tailscale + PIN, daily digest email, "
        "Gmail-label remote dismissal with categorical reasons (`YouOS/skip-*`), "
        "multi-account setup.",
    ),
    (
        "USAGE.md",
        "CLI command reference",
        "Every `youos <command>` with usage and a one-line description.",
    ),
    (
        "ARCHITECTURE.md",
        "Architecture",
        "Internal module map — ingestion, retrieval, generation, agent loop, "
        "autoresearch, schema. For contributors and curious operators.",
    ),
]


TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — YouOS</title>
<meta name="description" content="{description}">
<meta property="og:title" content="{title} — YouOS">
<meta property="og:description" content="{description}">
<meta property="og:type" content="article">
<link rel="canonical" href="https://youos.drbaher.com/docs/{href}">
{alt_md_link}<link rel="icon" type="image/svg+xml" href="/youos-mark.svg">
<style>
  :root {{
    --teal: #00c4a7; --teal-dim: #0a8f7c;
    --bg: #0d1117; --surface: #161b22; --surface-2: #1c2330;
    --border: #30363d; --text: #e6edf3; --muted: #8b949e; --bad: #f85149;
  }}
  @media (prefers-color-scheme: light) {{
    :root {{
      --teal: #0a8f7c;
      --bg: #ffffff; --surface: #f5f7fa; --surface-2: #eceff3;
      --border: #d8dee6; --text: #1a2230; --muted: #5b6675; --bad: #d1242f;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", system-ui, sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.65;
    margin: 0; padding: 0;
  }}
  .container {{ max-width: 860px; margin: 0 auto; padding: 0 20px 64px; }}
  header.site-nav {{
    border-bottom: 1px solid var(--border);
    padding: 16px 20px;
    display: flex; gap: 24px; align-items: center;
    font-size: 0.92rem;
  }}
  header.site-nav a {{ color: var(--muted); text-decoration: none; }}
  header.site-nav a:hover {{ color: var(--teal); }}
  header.site-nav .brand {{ font-weight: 700; color: var(--teal); }}
  header.site-nav .spacer {{ flex: 1; }}
  header.site-nav .raw {{ color: var(--teal); }}
  main h1 {{ font-size: 2rem; margin: 32px 0 8px; color: var(--teal); }}
  main h2 {{ font-size: 1.4rem; margin: 36px 0 12px; border-bottom: 1px solid var(--border); padding-bottom: 6px; }}
  main h3 {{ font-size: 1.1rem; margin: 24px 0 8px; }}
  main p, main ul, main ol {{ margin: 0 0 14px; }}
  main code {{
    background: var(--surface-2); padding: 2px 6px; border-radius: 4px;
    font-family: ui-monospace, "SF Mono", "Menlo", monospace; font-size: 0.9em;
  }}
  main pre {{
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 14px 16px; overflow-x: auto;
    font-size: 0.88rem; line-height: 1.5;
  }}
  main pre code {{ background: none; padding: 0; }}
  main blockquote {{
    border-left: 4px solid var(--teal); margin: 14px 0; padding: 4px 16px;
    color: var(--muted); background: var(--surface);
  }}
  main table {{
    border-collapse: collapse; margin: 14px 0; width: 100%;
    display: block; overflow-x: auto;
  }}
  main th, main td {{ border: 1px solid var(--border); padding: 8px 12px; text-align: left; }}
  main th {{ background: var(--surface); font-weight: 600; }}
  main a {{ color: var(--teal); }}
  main a:hover {{ text-decoration: underline; }}
  main hr {{ border: none; border-top: 1px solid var(--border); margin: 32px 0; }}
  footer.site-foot {{
    border-top: 1px solid var(--border); padding: 24px 20px; text-align: center;
    color: var(--muted); font-size: 0.85rem; margin-top: 64px;
  }}
  footer.site-foot a {{ color: var(--muted); }}
</style>
</head>
<body>
<header class="site-nav">
  <a class="brand" href="/">YouOS</a>
  <a href="/docs/">All docs</a>
  <a href="https://github.com/DrBaher/youos">GitHub</a>
  <span class="spacer"></span>
  {raw_md_chip}
</header>
<main>
<div class="container">
{content}
</div>
</main>
<footer class="site-foot">
  <p>YouOS — local-first personal email copilot · <a href="https://github.com/DrBaher/youos">source</a> · <a href="/">home</a></p>
  {mirror_note}
</footer>
</body>
</html>
"""


def _strip_first_h1(html: str) -> tuple[str, str]:
    """Pull the first <h1> out of the rendered body so the page heading
    isn't duplicated when the .md starts with ``# Title``. Returns
    (title_text, html_with_first_h1_removed). Falls back gracefully if
    the doc doesn't start with an H1."""
    m = re.search(r"<h1[^>]*>(.+?)</h1>", html, re.DOTALL)
    if not m:
        return "", html
    inner = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    return inner, html[: m.start()] + html[m.end() :]


def render() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    docs_src = repo_root / "docs"
    out_root = repo_root / "_site"
    out_dir = out_root / "docs"
    out_dir.mkdir(parents=True, exist_ok=True)

    md = markdown.Markdown(
        extensions=["fenced_code", "tables", "toc"],
        extension_configs={"toc": {"permalink": False}},
    )

    index_items: list[tuple[str, str, str]] = []
    for src_name, fallback_title, blurb in DOCS:
        src = docs_src / src_name
        if not src.exists():
            print(f"  skip (missing): {src}", file=sys.stderr)
            continue
        text = src.read_text(encoding="utf-8")
        html_body = md.convert(text)
        md.reset()
        page_title, html_body = _strip_first_h1(html_body)
        title = page_title or fallback_title
        href = src_name.replace(".md", ".html")
        page = TEMPLATE.format(
            title=title,
            description=blurb,
            href=href,
            alt_md_link=(
                f'<link rel="alternate" type="text/markdown" '
                f'href="/docs/{src_name}" title="Raw markdown">\n'
            ),
            raw_md_chip=(
                f'<a class="raw" href="/docs/{src_name}">View raw markdown ↓</a>'
            ),
            mirror_note=(
                f'<p>This page mirrors <code>docs/{src_name}</code> in the repo. '
                f'The raw markdown above is the canonical form for LLM agents.</p>'
            ),
            content=f"<h1>{title}</h1>\n{html_body}",
        )
        (out_dir / href).write_text(page, encoding="utf-8")
        (out_dir / src_name).write_text(text, encoding="utf-8")
        index_items.append((title, blurb, href))
        print(f"  rendered: {href}")

    # Index page — links + blurbs so a first-time visitor knows which to open.
    items_html = "\n".join(
        f'<li><h3><a href="./{href}">{title}</a> '
        f'<small style="color:var(--muted);font-weight:normal">'
        f'· <a href="./{href.replace(".html", ".md")}">raw .md</a></small></h3>'
        f'<p>{blurb}</p></li>'
        for title, blurb, href in index_items
    )
    index_body = (
        '<h1>YouOS — documentation</h1>'
        '<p>The canonical contract is the OpenAPI spec served by every YouOS '
        'instance at <code>GET /openapi.json</code>. These docs explain the '
        'concepts, recipes, and runtime conventions that aren\'t captured by '
        'the schema alone.</p>'
        '<p style="color:var(--muted)">For LLM-driven agents: each doc is '
        'available as <strong>rendered HTML</strong> (for humans) and '
        '<strong>raw markdown</strong> (for tool-use context). Start with '
        '<a href="./AGENT_OPERATIONS.html">Agent operations playbook</a>.</p>'
        f'<ul style="list-style:none;padding:0;margin:24px 0">{items_html}</ul>'
        '<hr><p><small>See also: <code>GET /openapi.json</code> · '
        '<a href="https://github.com/DrBaher/youos">repo</a></small></p>'
    )
    (out_dir / "index.html").write_text(
        TEMPLATE.format(
            title="Documentation",
            description="YouOS documentation index — agent operations, integrations, remote access, CLI, architecture.",
            href="",
            alt_md_link="",   # index has no .md counterpart
            raw_md_chip="",   # nothing to link to
            mirror_note="",
            content=index_body,
        ),
        encoding="utf-8",
    )

    # llms.txt — emerging convention (https://llmstxt.org) for letting LLM
    # crawlers/agents find the right entry-point docs. Top-level so it's the
    # first file an agent would probe after / and /robots.txt.
    llms_txt = (
        "# YouOS\n\n"
        "> Local-first personal email copilot. Background agent sweeps unread inbox, "
        "drafts replies, queues for review; exposes REST + OpenAPI for orchestrators "
        "(Hermes / OpenClaw / Telegram bot).\n\n"
        "## For LLM agents operating YouOS\n\n"
        "- [Agent operations playbook](https://youos.drbaher.com/docs/AGENT_OPERATIONS.md): "
        "decision tree, idempotency, error handling, paraphrasing, trust boundaries, "
        "worked conversation. Start here.\n"
        "- [Integrations](https://youos.drbaher.com/docs/INTEGRATIONS.md): "
        "architecture, setup, endpoint reference, security model, reference Telegram bot.\n"
        "- OpenAPI spec: every YouOS instance serves it at `GET /openapi.json`.\n\n"
        "## For humans setting up\n\n"
        "- [Remote access (Tailscale, phone, multi-account)](https://youos.drbaher.com/docs/REMOTE_ACCESS.md)\n"
        "- [CLI command reference](https://youos.drbaher.com/docs/USAGE.md)\n"
        "- [Architecture](https://youos.drbaher.com/docs/ARCHITECTURE.md)\n\n"
        "## Source\n\n"
        "- Repo: https://github.com/DrBaher/youos\n"
    )
    (out_root / "llms.txt").write_text(llms_txt, encoding="utf-8")

    # robots.txt — explicit allow on /docs/ so crawlers/LLM training indexes pick it up.
    robots_txt = (
        "User-agent: *\n"
        "Allow: /\n"
        "Sitemap: https://youos.drbaher.com/sitemap.xml\n"
    )
    (out_root / "robots.txt").write_text(robots_txt, encoding="utf-8")

    # Minimal sitemap (the index page + each doc).
    urls = [
        "https://youos.drbaher.com/",
        "https://youos.drbaher.com/docs/",
        *[f"https://youos.drbaher.com/docs/{href}" for _, _, href in index_items],
    ]
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(f"  <url><loc>{u}</loc></url>\n" for u in urls)
        + "</urlset>\n"
    )
    (out_root / "sitemap.xml").write_text(sitemap, encoding="utf-8")

    print(f"docs built: {len(index_items)} pages + index + llms.txt + sitemap.xml")


if __name__ == "__main__":
    render()
