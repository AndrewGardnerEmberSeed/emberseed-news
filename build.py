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
  python3 build.py --force      # run now, even outside 1am to 10am in Adelaide
  python3 build.py --dry-run    # full run with Claude that prints the stories and saves nothing
"""

import argparse
import datetime as dt
import email.utils
import gzip
import hashlib
import html
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
DATA = ROOT / "data"
STATIC = ROOT / "static"  # icons and the web app manifest, copied into the site as they are
TZ = ZoneInfo("Australia/Adelaide")
SITE_URL = (os.environ.get("NEWS_SITE_URL") or "https://news.emberseedautomation.com.au").rstrip("/")
MAIN_SITE = "https://emberseedautomation.com.au"
# One model picks the stories and another writes them. Set NEWS_PICK_MODEL or
# NEWS_WRITE_MODEL to change either.
PICK_MODEL = os.environ.get("NEWS_PICK_MODEL") or "claude-sonnet-5-5"
WRITE_MODEL = os.environ.get("NEWS_WRITE_MODEL") or "claude-haiku-5-5"
EFFORT = os.environ.get("NEWS_EFFORT") or "medium"  # how hard Claude thinks: low, medium or high
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) EmberSeedNews/1.0"
WINDOW_HOURS = 28
MIN_STORIES, MAX_STORIES = 4, 8
MAX_PER_CATEGORY = 3   # (17 Oct 2026: five of eight stories were Safety & policy)
RESERVES = 3           # the picker ranks a few more, in case some don't pass the checks
MIN_SOURCE_CHARS = 1000  # a story needs more than a feed's excerpt to go on (a feed gives at most 600 characters)
# US$ per million input and output tokens, for the cost line at the end of each
# run. Check https://platform.claude.com/docs/en/about-claude/pricing if they change.
PRICES = {"claude-opus-5-5": (4, 20), "claude-sonnet-5-5": (2, 10), "claude-haiku-5-5": (0.10, 0.50)}
USAGE = {}  # tokens used, per model

# Robotics is out of scope, and so is very technical material.
EXCLUDE = re.compile(
    r"\b(robot|robots|robotic|robotics|humanoid|humanoids|drone|drones|quadruped|"
    r"exoskeleton|arxiv|preprint)\b", re.I)

TIP_ONLY = re.compile(r"YouTube|podcast", re.I)
TIP_ONLY_CATEGORY = "Commentators & newsletters"   # YouTube, podcasts and newsletters: tip-offs, not sources


def tip_only(item):
    return bool(TIP_ONLY.search(item["source"])) or item.get("category_hint") == TIP_ONLY_CATEGORY

CATEGORIES = ["Trends", "New models", "Industry", "Safety & policy", "Business",
              "Gaming & entertainment", "Applications"]

# Structured outputs: Claude's reply has to match these shapes exactly.
SELECT_SCHEMA = {
    "type": "object",
    "properties": {"stories": {"type": "array", "items": {
        "type": "object",
        "properties": {"ids": {"type": "array", "items": {"type": "integer"}},
                       "category": {"type": "string", "enum": CATEGORIES}},
        "required": ["ids", "category"], "additionalProperties": False}}},
    "required": ["stories"], "additionalProperties": False}

STORY_FIELDS = ["headline", "summary", "why_it_matters", "category", "source_url"]
CHECK_SCHEMA = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": {
        "type": "object",
        "properties": {"index": {"type": "integer"},
                       "verdict": {"type": "string", "enum": ["ok", "fix", "drop"]},
                       "problems": {"type": "array", "items": {"type": "string"}},
                       "headline": {"type": "string"}, "summary": {"type": "string"},
                       "why_it_matters": {"type": "string"}},
        "required": ["index", "verdict", "problems", "headline", "summary", "why_it_matters"],
        "additionalProperties": False}}},
    "required": ["results"], "additionalProperties": False}
HEADLINE_SCHEMA = {"type": "object", "properties": {"headline": {"type": "string"}},
                   "required": ["headline"], "additionalProperties": False}
STORY_SCHEMA = {
    "type": "object",
    "properties": {f: ({"type": "string", "enum": CATEGORIES} if f == "category" else {"type": "string"})
                   for f in STORY_FIELDS},
    "required": STORY_FIELDS, "additionalProperties": False}


# ---------------------------------------------------------------- feeds

def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    # Some servers (Google DeepMind's blog, for one) send gzip even when it wasn't asked for.
    return gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw


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


def recent_stories(before, editions=3):
    """Stories from the last few editions, so the same news doesn't run twice."""
    files = [f for f in sorted(DATA.glob("*.json"), reverse=True) if f.stem < before.isoformat()]
    return [s for f in files[:editions] for s in json.loads(f.read_text())]


def collect(feeds, now, skip_urls=()):
    cutoff = now - dt.timedelta(hours=WINDOW_HOURS)
    seen, out, problems = set(), [], []
    for feed in feeds:
        try:
            items = parse_feed(fetch(feed["url"]), feed)
        except Exception as e:  # noqa: BLE001 - keep going if one feed fails
            problems.append(f"{feed['name']}: {str(e)[:80] or e.__class__.__name__}")
            continue
        for it in items:
            when = dt.datetime.fromisoformat(it["published"])
            if when < cutoff or when > now + dt.timedelta(hours=2):
                continue
            if EXCLUDE.search(it["title"] + " " + it["description"]) or it["link"] in skip_urls:
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

def claude(prompt, system, schema, model, max_tokens=16000, cache=False):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY is not set")
    # With cache=True the style guide is cached, so the calls after the first
    # read it at a tenth of the price.
    system_block = {"type": "text", "text": system}
    if cache:
        system_block["cache_control"] = {"type": "ephemeral"}
    body = {
        "model": model,
        "max_tokens": max_tokens,  # room for Claude's thinking as well as the reply
        "system": [system_block],
        "messages": [{"role": "user", "content": prompt}],
        # Current Claude models always think; effort sets how much.
        "output_config": {"effort": EFFORT, "format": {"type": "json_schema", "schema": schema}},
    }
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    # If a safety check declines the request, Anthropic reruns it on a suitable
    # model. Haiku has no such fallback.
    if not model.startswith("claude-haiku"):
        body["fallbacks"] = "default"
        headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
    req = urllib.request.Request("https://api.anthropic.com/v1/messages",
                                 data=json.dumps(body).encode(), method="POST", headers=headers)
    # Rate limits (429) and busy periods (5xx, 529) usually clear within a minute,
    # so wait and try again. Other errors stop with the API's own explanation.
    for attempt in range(1, 6):
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                data = json.load(r)
            used = USAGE.setdefault(model, {"calls": 0, "input": 0, "cache_write": 0, "cache_read": 0, "output": 0})
            u = data.get("usage", {})
            used["calls"] += 1
            used["input"] += u.get("input_tokens", 0)
            used["cache_write"] += u.get("cache_creation_input_tokens", 0)
            used["cache_read"] += u.get("cache_read_input_tokens", 0)
            used["output"] += u.get("output_tokens", 0)
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:400]
            if e.code not in (429, 500, 502, 503, 504, 529) or attempt == 5:
                raise RuntimeError(f"Claude API error {e.code}: {detail}") from None
            after = (e.headers.get("retry-after") or "").strip()
            wait = float(after) if after.replace(".", "", 1).isdigit() else 15 * attempt
        except OSError:  # network trouble or a timeout
            if attempt == 5:
                raise
            wait = 15 * attempt
        print(f"  Claude is busy (attempt {attempt}); trying again in {min(wait, 90):.0f}s")
        time.sleep(min(wait, 90))
    if data.get("stop_reason") == "max_tokens":
        raise ValueError("Claude ran out of room before finishing")
    if data.get("stop_reason") == "refusal":
        raise ValueError(f"Claude declined ({(data.get('stop_details') or {}).get('category') or 'no reason given'})")
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}|\[.*\]", text, re.S)
    if not m:
        raise ValueError("Claude did not return JSON")
    return json.loads(m.group(0))


def select_stories(candidates, style, recent=()):
    listing = "\n".join(
        f"[{c['id']}] {c['source']}{' (tip-off)' if tip_only(c) else ''} | {c['published'][:16]} | {c['title']} | "
        f"{c['description'][:160]}"
        for c in candidates)
    prompt = (
        f"Here are today's AI news items from our feeds, one per line as "
        f"[id] source | published (UTC) | title | description.\n\n{listing}\n\n"
        f"Rank the {MAX_STORIES + RESERVES} most interesting stories for small-business owners, "
        f"strongest first, following the editorial scope in the style guide: we publish the first "
        f"{MAX_STORIES} that pass our checks. Group items that cover the same story. Items marked "
        f"(tip-off) are YouTube, podcasts and newsletters: never pick a story unless a news outlet "
        f"or the company itself also covers it. Prefer stories more than one outlet covers. Spread "
        f"across categories: at most {MAX_PER_CATEGORY} stories in any one category, and include a "
        f"Business story (AI in real companies, jobs, productivity) when there is a good one. Pick at "
        f"most two stories about any one company. "
        f"For each story, give the ids of the items that cover it and its category.")
    if recent:
        prompt += ("\n\nThese stories ran in the last few editions. Skip them unless there's a "
                   "significant new development:\n" + "\n".join(f"- {h}" for h in recent))
    picked = claude(prompt, style, SELECT_SCHEMA, PICK_MODEL)
    if isinstance(picked, dict):
        picked = picked.get("stories", [])
    return picked[:MAX_STORIES + RESERVES]


def outlet(feed_name):
    """'The Verge (AI)' -> 'The Verge'."""
    return re.sub(r"\s*\(.*\)$", "", feed_name)


def write_story(group, candidates, style):
    ids = [int(i) for i in group.get("ids", []) if str(i).isdigit()]
    srcs = [candidates[i] for i in ids if i < len(candidates)]
    srcs = [s for s in srcs if not tip_only(s)]
    if not srcs:
        raise ValueError("only YouTube, podcast or newsletter sources")
    material, longest = [], 0
    for s in srcs[:3]:
        text = article_text(s["link"]) or s["description"]
        longest = max(longest, len(text))
        material.append(f"SOURCE: {s['source']}\nURL: {s['link']}\nPUBLISHED: {s['published']}\n"
                        f"TITLE: {s['title']}\nTEXT:\n{text}")
    # (17 Oct 2026: a story written from a feed's excerpt had no period or market for its main figure)
    if longest < MIN_SOURCE_CHARS:
        raise ValueError(f"only a short excerpt to go on (the article couldn't be read): {srcs[0]['source']}: {srcs[0]['title']}")
    prompt = (
        "Write one EmberSeed news item from the source material below, following the style "
        "guide exactly. Use only facts stated in the sources. If a detail isn't in the sources, "
        "leave it out. Give the headline, summary, why it matters, category, and the URL of "
        "the main source.\n\n"
        + "\n\n---\n\n".join(material))
    story = claude(prompt, style, STORY_SCHEMA, WRITE_MODEL, cache=True)
    if not isinstance(story, dict):
        raise ValueError("Claude didn't return a story")
    for k in ("headline", "summary", "why_it_matters"):
        story[k] = str(story.get(k) or "").strip()
        if not story[k]:
            raise ValueError(f"the story has no {k}")
    # Only ever link to an article Claude was given, and only list outlets that
    # actually covered the story in our feeds.
    if story.get("source_url") not in {s["link"] for s in srcs}:
        story["source_url"] = srcs[0]["link"]
    main_src = next(s for s in srcs if s["link"] == story["source_url"])
    story["source_name"] = outlet(main_src["source"])
    also = []
    for s in srcs:
        name = outlet(s["source"])
        if s is not main_src and name not in also and name != story["source_name"]:
            also.append(name)
    story["also_reported_by"] = also
    story["category"] = story.get("category") if story.get("category") in CATEGORIES else group.get("category", "Trends")
    # Haiku sometimes runs a word or two over the ten-word limit.
    if len(story["headline"].split()) > 10:
        shorter = claude("Rewrite this news headline in ten words or fewer. Keep it specific "
                         "(who did what) and keep its facts: " + story["headline"],
                         style, HEADLINE_SCHEMA, WRITE_MODEL, cache=True)
        story["headline"] = str(shorter.get("headline") or story["headline"]).strip()
    story["_source_text"] = " ".join(material)
    story["_sources"] = [(s["link"], m) for s, m in zip(srcs[:3], material)]   # (for the check)
    return story


def check_stories(stories, style):
    """Before anything is published, the picking model reads every story against its own sources, as a sub-editor
    would: claims the sources don't make, lost conditions, and advice that doesn't follow from the facts or is aimed
    at the wrong reader. It can pass a story, correct it from the sources, or drop it. (Added 17 Oct 2026, when the
    cheaper writer told Australian businesses they'd need a Chinese export licence.)"""
    if not stories:
        return stories
    def sources(s):   # what the writer had: the main article in full (to 12,000 characters), the others shorter
        out = [m[:12000] if url == s.get("source_url") else m[:3000] for url, m in s.get("_sources") or []]
        return "\n\n".join(out) or s["_source_text"][:12000]
    blocks = [f"STORY {i}\nHEADLINE: {s['headline']}\nSUMMARY: {s['summary']}\nWHY IT MATTERS: {s['why_it_matters']}\n"
              f"SOURCES:\n{sources(s)}" for i, s in enumerate(stories)]
    prompt = (
        "You are the sub-editor. Check each story below against its own sources before it is published to "
        "Australian small-business owners. Flag only real errors: a claim the sources don't make, or contradict; a "
        "lost condition or qualifier (who a rule applies to, thresholds, dates); advice in 'why it matters' that "
        "doesn't follow from the facts, or applies them to the wrong reader or country. Facts anywhere in the "
        "sources count. Don't flag what a story leaves out, and don't rewrite for style.\n"
        "Give one result per story, by its number:\n"
        "- 'ok' if it has no real errors: leave headline, summary and why_it_matters empty.\n"
        "- 'fix' if it can be corrected from the sources: give only the fields you corrected, changed as little as "
        "possible and within the style guide's lengths, and leave the others empty. Write corrections as finished "
        "copy for readers: never mention 'the source' or 'the sources'; attribute as the style guide says.\n"
        "- 'drop' if the sources are too thin to support it.\n"
        "Problems: at most three, a few plain words each (none for 'ok').\n\n"
        + "\n\n=====\n\n".join(blocks))
    try:
        out = claude(prompt, style, CHECK_SCHEMA, PICK_MODEL)
    except Exception as e:  # noqa: BLE001 - publish as written rather than lose the edition
        print("  the check didn't run:", e)
        return stories
    results = {r.get("index"): r for r in (out.get("results") or [] if isinstance(out, dict) else [])}
    kept = []
    for i, s in enumerate(stories):
        r = results.get(i)
        if not r or r.get("verdict") == "ok":
            kept.append(s)
            continue
        why = "; ".join(r.get("problems") or []) or "no reason given"
        if r.get("verdict") == "drop":
            print(f"  dropped by the check: {s['headline']} ({why})")
            continue
        fixed = dict(s)
        for k in ("headline", "summary", "why_it_matters"):
            if str(r.get(k) or "").strip():
                fixed[k] = str(r[k]).strip()
        if len(fixed["headline"].split()) > 10:
            fixed["headline"] = s["headline"]
        if not numbers_check(fixed):
            print(f"  dropped (the check's correction has numbers the sources don't): {s['headline']}")
            continue
        print(f"  corrected by the check: {s['headline']} ({why})")
        kept.append(fixed)
    return kept


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
<link rel="icon" href="{base}favicon.svg" type="image/svg+xml">
<link rel="icon" href="{base}favicon-32.png" sizes="32x32" type="image/png">
<link rel="apple-touch-icon" href="{base}apple-touch-icon.png">
<link rel="manifest" href="{base}manifest.webmanifest">
<meta name="apple-mobile-web-app-title" content="AI News">
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
nav a[aria-current]{{color:#C2410C}}
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
footer.site .links{{display:flex;flex-wrap:wrap;gap:6px 20px}}
</style>
</head>
<body>
<header class="site"><div class="wrap">
<a class="brand" href="{main}/"><svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="#E8A33D" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2.5c1.6 2.6 4.8 4.8 4.8 9a4.8 4.8 0 0 1-9.6 0c0-2 1-3.4 2.1-4.3.1 1.8 1 3 2.1 3.3-.4-2.6-.9-5.3.6-8Z"></path><circle cx="12" cy="20.5" r="1.1"></circle></svg>EmberSeed</a>
<nav aria-label="Main"><a href="{main}/#how">How it works</a><a href="{main}/#prices">Prices</a><a href="{main}/#about">About</a><a href="{base}index.html" aria-current="page">News</a><a href="{main}/playbook">Playbook</a><a class="cta" href="{main}/#contact">Book a free chat</a></nav>
</div></header>
<main>
"""

FOOT = """</main>
<footer class="site"><div class="wrap">
<p>EmberSeed is a trading name of Wilderness Properties Pty Ltd · ACN 614 946 897 · Mylor SA 5153</p>
<p class="links"><a href="{base}index.html">News</a><a href="{main}/playbook">Playbook</a><a href="{main}/privacy">Privacy</a><a href="{main}/how-i-use-ai">How I use AI</a><a href="{base}archive.html">Archive</a><a href="{base}feed.xml">RSS</a></p>
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
        return (f'<h2>{esc(day)}</h2><p class="note">{count} · <a href="{base}archive.html">Past editions</a></p>\n'
                + "".join(story_block(s, base=base) for s in stories))
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
    ap.add_argument("--force", action="store_true", help="run now, even outside 1am to 10am in Adelaide")
    ap.add_argument("--render-only", action="store_true", help="rebuild pages and feed from data/ without fetching")
    ap.add_argument("--dry-run", action="store_true", help="full run with Claude that prints the stories and saves nothing")
    args = ap.parse_args()
    if args.render_only:
        rebuild_site()
        return

    now = dt.datetime.now(dt.timezone.utc)
    here = now.astimezone(TZ)
    # GitHub can start a scheduled run hours late, or skip it when it's busy, so
    # any run from 1am to 10am counts. The check below stops a second edition.
    if not (args.force or args.dry_run) and not 1 <= here.hour < 10:
        print(f"It's {here:%H:%M} in Adelaide, outside 1am to 10am. Nothing to do.")
        return
    edition_date = here.date()
    DATA.mkdir(exist_ok=True)
    if DATA.joinpath(f"{edition_date}.json").exists() and not (args.force or args.dry_run):
        print("Today's edition already exists.")
        return

    feeds = json.loads(ROOT.joinpath("feeds.json").read_text())
    style = ROOT.joinpath("style-guide.md").read_text()
    recent = recent_stories(edition_date)
    candidates, problems = collect(feeds, now, {s["source_url"] for s in recent})
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
        per_category = {}
        for group in select_stories(candidates, style, [s["headline"] for s in recent]):
            if len(stories) >= MAX_STORIES:
                break
            if per_category.get(group.get("category"), 0) >= MAX_PER_CATEGORY:
                continue   # (a reserve from a category that's full)
            try:
                s = write_story(group, candidates, style)
            except Exception as e:  # noqa: BLE001
                print("  skipped a story:", e)
                continue
            if not numbers_check(s):
                print("  dropped (numbers not in source):", s.get("headline"))
                continue
            if per_category.get(s["category"], 0) >= MAX_PER_CATEGORY:
                print(f"  skipped (already {MAX_PER_CATEGORY} {s['category']} stories):", s.get("headline"))
                continue
            per_category[s["category"]] = per_category.get(s["category"], 0) + 1
            stories.append(s)
        stories = check_stories(stories, style)
        for s in stories:
            s.pop("_source_text", None)
            s.pop("_sources", None)
    if args.dry_run:
        print(json.dumps(stories, indent=2, ensure_ascii=False))
        print(f"Test run: {len(stories)} stories passed the checks. Nothing was saved.")
        return
    if len(stories) < MIN_STORIES and not args.no_ai:
        sys.exit("Too few stories passed the checks; leaving yesterday's edition up.")

    published = here.replace(second=0, microsecond=0).isoformat()
    for s in stories:
        s["slug"] = f"{edition_date}-{slugify(s['headline'])}"
        s["edition_published"] = published

    DATA.joinpath(f"{edition_date}.json").write_text(json.dumps(stories, indent=2, ensure_ascii=False))
    rebuild_site()
    print(f"Built the {edition_date} edition: {len(stories)} stories.")


def rebuild_site():
    # Every edition is rendered, oldest first, so fixes to older stories show up
    # and the newest edition writes index.html last.
    files = sorted(DATA.glob("*.json"))
    if not files:
        sys.exit("No editions in data/ yet.")
    editions = [(dt.date.fromisoformat(f.stem), json.loads(f.read_text())) for f in files]
    history = [{"date": d.isoformat(), "label": nice_date(d), "count": len(s)} for d, s in reversed(editions)]
    for d, stories in editions:
        render(d, stories, history)
    write_feed([s for _, stories in reversed(editions) for s in stories])
    shutil.copytree(STATIC, SITE, dirs_exist_ok=True)
    print(f"Site rebuilt: {len(files)} editions, latest {files[-1].stem}.")


def usage_report():
    """What this run used, and roughly what it cost."""
    lines, total = ["Claude usage:"], 0.0
    for model, u in USAGE.items():
        tokens_in = u["input"] + u["cache_write"] + u["cache_read"]
        line = (f"  {model}: {u['calls']} calls, {tokens_in:,} input ({u['cache_read']:,} from cache), "
                f"{u['output']:,} output")
        if model in PRICES:
            p_in, p_out = PRICES[model]
            cost = ((u["input"] + 1.25 * u["cache_write"] + 0.1 * u["cache_read"]) * p_in
                    + u["output"] * p_out) / 1e6
            total += cost
            line += f", about US${cost:.3f}"
        lines.append(line)
    lines.append(f"  Total about US${total:.3f}")
    return "\n".join(lines)


if __name__ == "__main__":
    try:
        main()
    finally:
        if USAGE:
            print(usage_report())
