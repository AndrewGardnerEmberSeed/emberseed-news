#!/usr/bin/env python3
"""EmberSeed AI News: nightly build.

Reads the RSS/Atom feeds in feeds.json, picks the most interesting AI stories
from the last day, has Claude rewrite them in the EmberSeed news style, and
writes a small static site (daily edition, one page per story, archive and an
RSS feed) into ./site.

Standard library only. Needs the ANTHROPIC_API_KEY environment variable,
unless run with --no-ai (a test mode that uses the feed text as-is).

Usage:
  python3 build.py              # normal nightly run
  python3 build.py --no-ai      # test the feeds and pages without calling Claude
  python3 build.py --force      # run now, even outside 1am to 4am in Adelaide
"""

import argparse
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
DATA = ROOT / "data"
TZ = ZoneInfo("Australia/Adelaide")
SITE_URL = (os.environ.get("NEWS_SITE_URL") or "https://news.emberseedautomation.com.au").rstrip("/")
MAIN_SITE = "https://emberseedautomation.com.au"
MODEL = os.environ.get("NEWS_MODEL") or "claude-opus-5-5"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) EmberSeedNews/1.0"
WINDOW_HOURS = 28
MIN_STORIES, MAX_STORIES = 4, 8

# Robotics is out of scope, and so is very technical material.
EXCLUDE = re.compile(
    r"\b(robot|robots|robotic|robotics|humanoid|humanoids|drone|drones|quadruped|"
    r"exoskeleton|arxiv|preprint)\b", re.I)

TIP_ONLY = re.compile(r"YouTube|podcast", re.I)

CATEGORIES = ["Trends", "New models", "Industry", "Safety & policy", "Business",
              "Gaming & entertainment", "Applications"]


# ---------------------------------------------------------------- feeds

def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def strip_tags(s):
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        d = None
    if d is None:
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_feed(raw, feed):
    """Return a list of items from RSS 2.0 or Atom bytes."""
    items = []
    root = ET.fromstring(raw)
    for el in root.iter():
        name = local(el.tag)
        if name not in ("item", "entry"):
            continue
        title = link = date = desc = ""
        for ch in el:
            n = local(ch.tag)
            if n == "title":
                title = "".join(ch.itertext())
            elif n == "link":
                href = ch.get("href") or (ch.text or "").strip()
                if href and ch.get("rel") in (None, "alternate") and not link:
                    link = href
            elif n in ("pubDate", "published", "updated", "date") and not date:
                date = ch.text or ""
            elif n in ("description", "summary", "content", "encoded") and not desc:
                desc = "".join(ch.itertext())
            elif n == "group":  # YouTube media:group
                for g in ch:
                    if local(g.tag) == "description" and not desc:
                        desc = g.text or ""
        when = parse_date(date)
        if not title or not link or not when:
            continue
        items.append({
            "title": strip_tags(title),
            "link": link.strip(),
            "published": when.astimezone(dt.timezone.utc).isoformat(),
            "description": strip_tags(desc)[:600],
            "source": feed["name"],
            "category_hint": feed.get("category", ""),
        })
    return items


def collect(feeds, now):
    cutoff = now - dt.timedelta(hours=WINDOW_HOURS)
    seen, out, problems = set(), [], []
    for feed in feeds:
        try:
            items = parse_feed(fetch(feed["url"]), feed)
        except Exception as e:  # noqa: BLE001 - keep going if one feed fails
            problems.append(f"{feed['name']}: {e.__class__.__name__}")
            continue
        for it in items:
            when = dt.datetime.fromisoformat(it["published"])
            if when < cutoff or when > now + dt.timedelta(hours=2):
                continue
            if EXCLUDE.search(it["title"] + " " + it["description"]):
                continue
            key = re.sub(r"[^a-z0-9]", "", it["title"].lower())[:80]
            url_key = it["link"].split("?")[0].rstrip("/")
            if key in seen or url_key in seen:
                continue
            seen.update({key, url_key})
            out.append(it)
    for i, it in enumerate(out):
        it["id"] = i
    return out, problems


def article_text(url, limit=12000):
    """Best-effort plain text of an article page."""
    try:
        raw = fetch(url).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return ""
    m = re.search(r"<article.*?</article>", raw, re.S | re.I) or re.search(r"<main.*?</main>", raw, re.S | re.I)
    body = m.group(0) if m else raw
    paras = re.findall(r"<p[^>]*>(.*?)</p>", body, re.S | re.I)
    text = "\n".join(t for t in (strip_tags(p) for p in paras) if len(t) > 40)
    return text[:limit]


# ---------------------------------------------------------------- Claude

def claude(prompt, system, max_tokens=4000):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY is not set")
    body = json.dumps({
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.load(r)
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}|\[.*\]", text, re.S)
    if not m:
        raise ValueError("Claude did not return JSON")
    return json.loads(m.group(0))


def select_stories(candidates, style):
    listing = "\n".join(
        f"[{c['id']}] {c['source']} | {c['published'][:16]} | {c['title']} | {c['description'][:280]}"
        for c in candidates)
    prompt = (
        f"Here are today's AI news items from our feeds, one per line as "
        f"[id] source | published (UTC) | title | description.\n\n{listing}\n\n"
        f"Pick the {MIN_STORIES}-{MAX_STORIES} most interesting stories for small-business "
        f"owners, following the editorial scope in the style guide. Group items that cover the "
        f"same story. YouTube and podcast items are tip-offs only: never pick a story unless a news "
        f"outlet or the company itself also covers it. Spread across categories where you can. Return JSON only: "
        f'{{"stories":[{{"ids":[...], "category": one of {CATEGORIES}}}]}} '
        f"with the strongest story first.")
    return claude(prompt, style)["stories"][:MAX_STORIES]


def write_story(group, candidates, style):
    srcs = [candidates[i] for i in group["ids"] if i < len(candidates)]
    srcs = [s for s in srcs if not TIP_ONLY.search(s["source"])]
    if not srcs:
        raise ValueError("only YouTube or podcast sources")
    material = []
    for s in srcs[:3]:
        text = article_text(s["link"]) or s["description"]
        material.append(f"SOURCE: {s['source']}\nURL: {s['link']}\nPUBLISHED: {s['published']}\n"
                        f"TITLE: {s['title']}\nTEXT:\n{text}")
    prompt = (
        "Write one EmberSeed news item from the source material below, following the style "
        "guide exactly. Use only facts stated in the sources. If a detail isn't in the sources, "
        "leave it out. Return JSON only: "
        '{"headline": str, "summary": str, "why_it_matters": str, "category": str, '
        '"source_name": str, "source_url": str, "also_reported_by": [str]}\n\n'
        + "\n\n---\n\n".join(material))
    story = claude(prompt, style, max_tokens=1500)
    story["category"] = story.get("category") if story.get("category") in CATEGORIES else group.get("category", "Trends")
    story["_source_text"] = " ".join(material)
    return story


def numbers_check(story):
    """Every number in the summary must appear somewhere in the source material."""
    nums = re.findall(r"\d[\d,.]*\d|\d", story["summary"] + " " + story.get("why_it_matters", ""))
    src = story["_source_text"].replace(",", "")
    return all(n.replace(",", "").rstrip(".") in src for n in nums)


# ---------------------------------------------------------------- pages

def slugify(s):
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:60].rstrip("-") or hashlib.sha1(s.encode()).hexdigest()[:8]


def esc(s):
    return html.escape(s or "", quote=True)


HEAD = """<!doctype html>
<html lang="en-AU">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{description}">
<link rel="alternate" type="application/rss+xml" title="EmberSeed AI News" href="{base}feed.xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500..800&amp;family=Figtree:wght@400;500;600;700&amp;display=swap">
<style>
*{{box-sizing:border-box}}
body{{margin:0;font-family:'Figtree',system-ui,-apple-system,'Segoe UI',sans-serif;color:#1A1F2D;background:#FFFFFF;font-size:17px;line-height:1.6;-webkit-font-smoothing:antialiased}}
a{{color:#C2410C;text-underline-offset:3px}}
a:hover{{opacity:.85}}
.wrap{{max-width:1160px;margin:0 auto;padding:16px 24px}}
header.site{{border-bottom:1px solid #E7E2DA}}
header.site .wrap{{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:16px}}
.brand{{display:flex;align-items:center;gap:10px;text-decoration:none;color:#1A1F2D;font-family:'Bricolage Grotesque',sans-serif;font-weight:700;font-size:23px;letter-spacing:-0.01em}}
nav{{display:flex;flex-wrap:wrap;align-items:center;gap:8px 28px;font-size:16px;font-weight:500}}
nav a{{color:#1A1F2D;text-decoration:none;padding:10px 0}}
nav a.cta{{background:#C2410C;color:#FFFFFF;padding:12px 20px;border-radius:999px;font-weight:600}}
main{{display:block;max-width:820px;margin:0 auto;padding:72px 24px 96px}}
.eyebrow{{margin:0 0 14px;font-weight:700;color:#C2410C;font-size:14px;letter-spacing:0.08em;text-transform:uppercase}}
h1{{margin:0;font-family:'Bricolage Grotesque',sans-serif;font-weight:700;font-size:clamp(34px,4.6vw,52px);line-height:1.06;letter-spacing:-0.025em}}
h2{{margin:48px 0 4px;font-family:'Bricolage Grotesque',sans-serif;font-size:28px;line-height:1.2;letter-spacing:-0.01em}}
.lede{{margin:22px 0 0;font-size:19px;color:#3A4152}}
.note{{margin:14px 0 0;font-size:15px;color:#565C6B}}
.story{{padding:28px 0;border-top:1px solid #E7E2DA}}
.tag{{margin:0 0 8px;font-size:13px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;color:#C2410C}}
.story h3{{margin:0;font-family:'Bricolage Grotesque',sans-serif;font-size:24px;line-height:1.25;letter-spacing:-0.01em}}
.story h3 a{{color:#1A1F2D;text-decoration:none}}
.story p{{margin:10px 0 0;color:#3A4152}}
.why{{color:#1A1F2D}}
.src{{font-size:15px;color:#565C6B}}
footer.site{{background:#1A1F2D;color:#C3C7D1;font-size:14px}}
footer.site .wrap{{display:flex;flex-wrap:wrap;gap:10px 24px;justify-content:space-between;padding:22px 24px}}
footer.site a{{color:#C3C7D1}}
footer.site p{{margin:0}}
</style>
</head>
<body>
<header class="site"><div class="wrap">
<a class="brand" href="{main}/"><svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="#E8A33D" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2.5c1.6 2.6 4.8 4.8 4.8 9a4.8 4.8 0 0 1-9.6 0c0-2 1-3.4 2.1-4.3.1 1.8 1 3 2.1 3.3-.4-2.6-.9-5.3.6-8Z"></path><circle cx="12" cy="20.5" r="1.1"></circle></svg>EmberSeed</a>
<nav aria-label="Main"><a href="{base}index.html">AI news</a><a href="{base}archive.html">Archive</a><a href="{main}/#prices">Prices</a><a class="cta" href="{main}/#contact">Book a free chat</a></nav>
</div></header>
<main>
"""

FOOT = """</main>
<footer class="site"><div class="wrap">
<p>EmberSeed is a trading name of Wilderness Properties Pty Ltd · ACN 614 946 897 · Mylor SA 5153</p>
<p><a href="{base}feed.xml">RSS</a> · <a href="{main}/privacy">Privacy</a> · <a href="{main}/how-i-use-ai">How I use AI</a></p>
</div></footer>
</body>
</html>
"""

LABEL = ('<p class="note"><strong style="color:#1A1F2D">Summarised by AI, read by Andrew.</strong> '
         'Each night AI picks the most interesting AI stories and summarises them from the original '
         'reporting. I read every edition and correct anything that\'s wrong, and every story links '
         'to its source. <a href="{main}/how-i-use-ai">How I use AI</a></p>')


def page(title, description, body, base=""):
    return (HEAD.format(title=esc(title), description=esc(description), base=base, main=MAIN_SITE)
            + body + FOOT.format(base=base, main=MAIN_SITE))


def nice_date(d):
    return f"{d:%A} {d.day} {d:%B %Y}"


def story_block(s, link_headline=True, base=""):
    head = (f'<a href="{base}stories/{s["slug"]}.html">{esc(s["headline"])}</a>'
            if link_headline else esc(s["headline"]))
    also = ""
    if s.get("also_reported_by"):
        also = " · Also reported by " + esc(", ".join(s["also_reported_by"][:3]))
    return (f'<article class="story"><p class="tag">{esc(s["category"])}</p><h3>{head}</h3>'
            f'<p>{esc(s["summary"])}</p>'
            f'<p class="why"><strong>Why it matters:</strong> {esc(s["why_it_matters"])}</p>'
            f'<p class="src">Source: <a href="{esc(s["source_url"])}">{esc(s["source_name"])}</a>{also}</p>'
            f'</article>\n')


def render(edition_date, stories, history):
    SITE.joinpath("stories").mkdir(parents=True, exist_ok=True)
    SITE.joinpath("editions").mkdir(parents=True, exist_ok=True)
    day = nice_date(edition_date)
    count = f"{len(stories)} stories" if len(stories) != 1 else "1 story"

    intro = (f'<p class="eyebrow">AI news</p><h1>What\'s happening in AI</h1>'
             f'<p class="lede">The AI news worth knowing, in plain English. New models, big moves, '
             f'safety, and what it all means for business.</p>'
             + LABEL.format(main=MAIN_SITE))
    def edition(base):
        return f'<h2>{esc(day)}</h2><p class="note">{count}</p>\n' + "".join(story_block(s, base=base) for s in stories)
    SITE.joinpath("index.html").write_text(page("AI news | EmberSeed", f"AI news for {day}, in plain English.", intro + edition("")))
    SITE.joinpath("editions", f"{edition_date:%Y-%m-%d}.html").write_text(
        page(f"AI news, {day} | EmberSeed", f"AI news for {day}, in plain English.", intro + edition("../"), base="../"))

    for s in stories:
        body = (f'<p class="eyebrow"><a href="../index.html">AI news</a> · {esc(day)}</p>'
                + story_block(s, link_headline=False).replace('<article class="story">', '<article class="story" style="border-top:0;padding-top:0">')
                + LABEL.format(main=MAIN_SITE))
        SITE.joinpath("stories", f"{s['slug']}.html").write_text(
            page(f"{s['headline']} | EmberSeed AI news", s["summary"][:155], body, base="../"))

    rows = "".join(
        f'<li><a href="editions/{h["date"]}.html">{esc(h["label"])}</a> · {h["count"]} stories</li>'
        for h in history)
    SITE.joinpath("archive.html").write_text(page("AI news archive | EmberSeed", "Every edition of EmberSeed AI news.",
        f'<p class="eyebrow">AI news</p><h1>Archive</h1><ul style="padding-left:22px;margin-top:28px">{rows}</ul>'))


def write_feed(all_stories):
    items = []
    for s in all_stories[:60]:
        when = dt.datetime.fromisoformat(s["edition_published"])
        items.append(
            "<item>"
            f"<title>{esc(s['headline'])}</title>"
            f"<link>{SITE_URL}/stories/{s['slug']}.html</link>"
            f"<guid isPermaLink=\"true\">{SITE_URL}/stories/{s['slug']}.html</guid>"
            f"<pubDate>{email.utils.format_datetime(when)}</pubDate>"
            f"<category>{esc(s['category'])}</category>"
            f"<description>{esc(s['summary'] + ' Why it matters: ' + s['why_it_matters'] + ' Source: ' + s['source_name'] + '.')}</description>"
            "</item>")
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>'
           "<title>EmberSeed AI News</title>"
           f"<link>{SITE_URL}/</link>"
           "<description>The AI news worth knowing, in plain English. Summarised by AI, read by Andrew.</description>"
           "<language>en-au</language>"
           + "".join(items) + "</channel></rss>\n")
    SITE.joinpath("feed.xml").write_text(xml)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ai", action="store_true", help="test run without calling Claude")
    ap.add_argument("--force", action="store_true", help="run now, even outside 1am to 4am in Adelaide")
    ap.add_argument("--render-only", action="store_true", help="rebuild pages and feed from data/ without fetching")
    args = ap.parse_args()
    if args.render_only:
        rebuild_site()
        return

    now = dt.datetime.now(dt.timezone.utc)
    here = now.astimezone(TZ)
    # GitHub can start a scheduled run late, or skip it when it's busy, so any
    # run from 1am to 4am counts. The check below stops a second edition.
    if not args.force and not 1 <= here.hour < 4:
        print(f"It's {here:%H:%M} in Adelaide, outside 1am to 4am. Nothing to do.")
        return
    edition_date = here.date()
    DATA.mkdir(exist_ok=True)
    if DATA.joinpath(f"{edition_date}.json").exists() and not args.force:
        print("Today's edition already exists.")
        return

    feeds = json.loads(ROOT.joinpath("feeds.json").read_text())
    style = ROOT.joinpath("style-guide.md").read_text()
    candidates, problems = collect(feeds, now)
    print(f"{len(candidates)} candidate items from {len(feeds)} feeds; {len(problems)} feeds failed")
    for p in problems:
        print("  feed problem:", p)
    if len(candidates) < MIN_STORIES:
        sys.exit("Not enough candidate stories today; leaving yesterday's edition up.")

    stories = []
    if args.no_ai:
        for c in candidates[:6]:
            stories.append({"headline": c["title"], "summary": c["description"][:300] or c["title"],
                            "why_it_matters": "(test mode: no AI rewrite)", "category": "Trends",
                            "source_name": c["source"], "source_url": c["link"], "also_reported_by": []})
    else:
        for group in select_stories(candidates, style):
            try:
                s = write_story(group, candidates, style)
            except Exception as e:  # noqa: BLE001
                print("  skipped a story:", e)
                continue
            if not numbers_check(s):
                print("  dropped (numbers not in source):", s.get("headline"))
                continue
            s.pop("_source_text", None)
            stories.append(s)
    if len(stories) < MIN_STORIES and not args.no_ai:
        sys.exit("Too few stories passed the checks; leaving yesterday's edition up.")

    published = here.replace(hour=1, minute=0, second=0, microsecond=0).isoformat()
    for s in stories:
        s["slug"] = f"{edition_date}-{slugify(s['headline'])}"
        s["edition_published"] = published

    DATA.joinpath(f"{edition_date}.json").write_text(json.dumps(stories, indent=2, ensure_ascii=False))
    rebuild_site()
    print(f"Built the {edition_date} edition: {len(stories)} stories.")


def rebuild_site():
    files = sorted(DATA.glob("*.json"), reverse=True)
    if not files:
        sys.exit("No editions in data/ yet.")
    history, all_stories = [], []
    for f in files:
        day_stories = json.loads(f.read_text())
        history.append({"date": f.stem, "label": nice_date(dt.date.fromisoformat(f.stem)), "count": len(day_stories)})
        all_stories.extend(day_stories)
    render(dt.date.fromisoformat(files[0].stem), json.loads(files[0].read_text()), history)
    write_feed(all_stories)
    print(f"Site rebuilt: {len(files)} editions, latest {files[0].stem}.")


if __name__ == "__main__":
    main()
