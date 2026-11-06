"""
Player news bot (Google News RSS + Bing News RSS)
-------------------------------------------------
Searches Google News and Bing News (both RSS, no API key, no registration) for the
people listed in players.json, drops anything published by Bosnian portals, merges
the results (same story from both engines is posted once) and posts new articles to
a Discord channel via webhook.

State (already posted articles) is kept in seen.json, which the GitHub Action
commits back to the repo after every run.
"""

import hashlib
import html
import json
import os
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, quote_plus, urlparse

import requests

CONFIG_FILE = "players.json"
SEEN_FILE = "seen.json"
MAX_SEEN = 3000              # keep the seen list from growing forever
FIRST_RUN_HOURS = 7 * 24     # first run: post everything from the last week
MAX_QUERY_LEN = 300          # several names are combined into one search
PAUSE = 2                    # seconds between requests, to stay polite
RETRY_WAITS = [10, 30, 60]   # waits after "Too Many Requests" / "Service Unavailable"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml",
}

# Bosnian portals that do NOT use a .ba domain (all .ba domains are blocked automatically).
BLOCKED_DOMAINS = {
    "nezavisne.com", "rtvbn.com", "atvbl.com", "srpskainfo.com", "bljesak.info",
    "hercegovina.info", "mostarski.info", "zurnal.info", "glassrpske.com",
    "rtrs.tv", "6yka.com", "bih-x.info",
}
# Bosnian portal names, for Google items where only the name is known
BLOCKED_NAMES = {
    "klix", "sport.ba", "sportsport", "avaz", "dnevni avaz", "n1 bih", "faktor",
    "hayat", "oslobođenje", "oslobodjenje", "nezavisne novine", "bljesak", "rtrs",
    "bhrt", "federalna", "istraga", "raport", "depo portal", "radio sarajevo",
    "srpskainfo", "glas srpske", "atv", "bn televizija", "hercegovina.info",
}


# ---------------------------------------------------------------- helpers

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def domain_of(url):
    host = urlparse(url or "").netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def ascii_name(name):
    """'Feđa Pervan' -> 'Fedja Pervan', 'Šošić' -> 'Sosic' (how foreign media usually write it)."""
    name = name.replace("đ", "dj").replace("Đ", "Dj")
    return unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()


def normalize(text):
    """Lowercase, strip accents and punctuation – used for matching and duplicates."""
    return re.sub(r"[^a-z0-9]+", " ", ascii_name(text or "").lower()).strip()


def strip_tags(text):
    return html.unescape(re.sub(r"<[^>]+>", " ", text or ""))


def is_bosnian(item, extra_blocked):
    d = item["domain"]
    if d.endswith(".ba"):
        return True
    if any(d == b or d.endswith("." + b) for b in BLOCKED_DOMAINS | extra_blocked):
        return True
    name = (item["source"] or "").lower()
    return bool(re.search(r"\.ba\b", name)) or name.strip() in BLOCKED_NAMES


def parse_date(s):
    try:
        dt = parsedate_to_datetime(s) if s else None
    except (TypeError, ValueError):
        return None
    if dt and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def local(tag):
    """XML tag without namespace: '{ns}Source' -> 'Source'."""
    return tag.split("}")[-1]


# ---------------------------------------------------------------- queries

def all_names(person):
    names = [person["name"]] + person.get("aliases", [])
    for n in list(names):  # add the version without č/ć/š/ž/đ automatically
        a = ascii_name(n)
        if a not in names:
            names.append(a)
    return names


def or_group(terms):
    terms = [f'"{t}"' for t in terms]
    return terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")"


def make_query(members, context):
    terms = [n for p in members for n in all_names(p)]
    return or_group(terms) + ((" " + or_group(list(context))) if context else "")


def build_groups(people):
    """Combine several people into one search (fewer requests). People are grouped
    by their 'context' words; the club searches come last."""
    buckets = {}
    for p in people:
        key = (p.get("role") == "Klub", tuple(p.get("context") or []))
        buckets.setdefault(key, []).append(p)
    groups = []
    for (_, context), members in sorted(buckets.items(), key=lambda kv: kv[0][0]):
        chunk = []
        for p in members:
            if chunk and len(make_query(chunk + [p], context)) > MAX_QUERY_LEN:
                groups.append((chunk, context))
                chunk = []
            chunk.append(p)
        if chunk:
            groups.append((chunk, context))
    return groups


# ---------------------------------------------------------------- fetching

def http_get(url):
    for wait in RETRY_WAITS + [None]:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code not in (429, 503) or wait is None:
            r.raise_for_status()
            return r
        print(f"   busy ({r.status_code}), waiting {wait}s…")
        time.sleep(wait)


def google_news(query, edition, hours):
    days = max(1, -(-hours // 24))  # round up
    url = (
        "https://news.google.com/rss/search?q=" + quote_plus(f"{query} when:{days}d")
        + f"&hl={edition['hl']}&gl={edition['gl']}&ceid={edition['ceid']}"
    )
    root = ET.fromstring(http_get(url).content)
    items = []
    for it in root.iter("item"):
        src = it.find("source")
        source = (src.text or "").strip() if src is not None else ""
        title = (it.findtext("title") or "").strip()
        if source and title.endswith(" - " + source):  # Google appends " - Source"
            title = title[: -len(" - " + source)]
        items.append({
            "title": title,
            "link": (it.findtext("link") or "").strip(),
            "source": source,
            "domain": domain_of(src.get("url", "")) if src is not None else "",
            "published": parse_date(it.findtext("pubDate")),
            "text": strip_tags(it.findtext("description")),
            "image": None,
            "label": edition["label"],
        })
    return items


def unwrap_bing(link):
    """bing.com/news/apiclick.aspx?...&url=<real url> -> real url"""
    u = urlparse(link)
    if u.netloc.endswith("bing.com") and "apiclick" in u.path:
        return parse_qs(u.query).get("url", [link])[0]
    return link


def bing_news(query, market):
    url = (
        "https://www.bing.com/news/search?format=rss&q=" + quote_plus(query)
        + '&qft=interval%3d"8"'          # last week; exact age is filtered later
        + f"&setmkt={market['mkt']}&setlang={market['mkt'].split('-')[0]}"
    )
    root = ET.fromstring(http_get(url).content)
    items = []
    for it in root.iter("item"):
        extra = {local(c.tag): (c.text or "").strip() for c in it}
        link = unwrap_bing(extra.get("link", ""))
        image = extra.get("Image") or None
        if image and image.startswith("http://"):
            image = "https://" + image[len("http://"):]
        title, source = extra.get("title", ""), extra.get("Source", "")
        if source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)]
        items.append({
            "title": title,
            "link": link,
            "source": extra.get("Source", ""),
            "domain": domain_of(link),
            "published": parse_date(extra.get("pubDate")),
            "text": strip_tags(extra.get("description")),
            "image": image,
            "label": market["label"],
        })
    return items


# ---------------------------------------------------------------- Discord

CLUB_WORDS = ["zeljeznicar", "zeljo", "grbavica"]


def has(hay, phrase):
    return f" {normalize(phrase)} " in hay


def who_is_it(item, members):
    """Strict check: which people does the article REALLY mention (title + snippet)?
    Search engines sometimes ignore quotes, so anything that doesn't pass is dropped."""
    hay = " " + normalize(f"{item['title']} {item['text']}") + " "
    club_mentioned = any(has(hay, w) for w in CLUB_WORDS)
    matched = []
    for p in members:
        context = p.get("context") or []
        context_ok = not context or any(has(hay, c) for c in context)
        if p.get("role") == "Klub":
            # 'Željezničar' + Sarajevo/Grbavica/FK, or a full club name
            if any(has(hay, n) for n in all_names(p) if " " in n) or \
               (has(hay, "zeljeznicar") and (has(hay, "sarajevo") or has(hay, "grbavica") or has(hay, "fk"))):
                matched.append(p)
            continue
        full_name = any(has(hay, n) for n in all_names(p))
        surname = p["name"].split()[-1]
        # surname alone ("Gojak scores") only counts if the club is mentioned too
        surname_ok = len(surname) > 3 and has(hay, surname) and club_mentioned
        if (full_name or surname_ok) and context_ok:
            matched.append(p)
    return matched


def describe(people):
    if not people:
        return "🔵 **FK Željezničar**  ·  igrač / stručni štab"
    lines = []
    for p in people[:3]:
        label = p.get("display", p["name"])
        lines.append(f"{p.get('icon', '👤')} **{label}**" + (f"  ·  {p['role']}" if p.get("role") else ""))
    return "\n".join(lines)


def post_to_discord(webhook, people, item):
    embed = {
        "title": (item["title"] or "(bez naslova)")[:256],
        "url": item["link"],
        "description": f"{describe(people)}\n🗞️ {item['source'] or item['domain']}  ·  {item['label']}",
        "color": 0x1E4FA3,
    }
    if item["image"]:
        embed["thumbnail"] = {"url": item["image"]}
    if item["published"]:
        embed["timestamp"] = item["published"].astimezone(timezone.utc).isoformat()
    payload = {"username": "Vijesti o igračima", "embeds": [embed]}

    for _ in range(4):
        r = requests.post(webhook, json=payload, timeout=20)
        if r.status_code == 429:  # Discord rate limit – wait and retry
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        if r.status_code == 400 and "thumbnail" in embed:  # bad image URL – retry without it
            embed.pop("thumbnail")
            continue
        r.raise_for_status()
        return
    raise RuntimeError("Discord rejected the post")


# ---------------------------------------------------------------- main

def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    dry_run = not webhook
    if dry_run:
        print("DISCORD_WEBHOOK_URL not set → dry run, printing instead of posting.")

    config = load_json(CONFIG_FILE, None)
    if not config:
        sys.exit(f"{CONFIG_FILE} missing")

    seen = load_json(SEEN_FILE, [])
    first_run = not seen  # nothing posted yet (also true if an earlier run found nothing)
    seen_set = set(seen)
    extra_blocked = {domain_of("https://" + d) or d for d in config.get("extra_blocked_domains", [])}
    hours = FIRST_RUN_HOURS if first_run else int(config.get("max_age_hours", 48))
    oldest = datetime.now(timezone.utc) - timedelta(hours=hours)
    if first_run:
        print("First run → posting all news from the last 7 days.")

    sources = [("Google", e, lambda q, e=e: google_news(q, e, hours)) for e in config.get("google_editions", [])]
    sources += [("Bing", m, lambda q, m=m: bing_news(q, m)) for m in config.get("bing_markets", [])]
    groups = build_groups(config["players"])
    print(f"{len(config['players'])} people in {len(groups)} searches × {len(sources)} sources")

    for members, context in groups:
        names = ", ".join(p["name"] for p in members)
        query = make_query(members, context)
        candidates, found = {}, 0
        for engine, src, fetch in sources:
            time.sleep(PAUSE)
            try:
                items = fetch(query)
            except Exception as e:  # one failing source shouldn't stop the rest
                print(f"   {engine} {src['label']} failed: {e}")
                continue
            found += len(items)
            for item in items:
                if not item["link"] or is_bosnian(item, extra_blocked):
                    continue
                if item["published"] and item["published"] < oldest:
                    continue
                # same headline from Google + Bing, or on several portals = one story
                key = hashlib.sha1(normalize(re.sub(r"\s+[-–|]\s+[^-–|]{2,40}$", "", item["title"]) or item["link"]).encode()).hexdigest()[:16]
                if key in seen_set or key in candidates:
                    continue
                candidates[key] = item

        # oldest first, so Discord shows them in chronological order
        far_past = datetime(1970, 1, 1, tzinfo=timezone.utc)
        ordered = sorted(candidates.items(), key=lambda kv: kv[1]["published"] or far_past)
        print(f"[{names}] {found} found, {len(ordered)} new")
        posted = 0
        for key, item in ordered:
            people = who_is_it(item, members)
            if not people:  # not really about our people – skip (not marked as seen,
                continue    # another search may be the right one for it)
            seen_set.add(key)
            seen.append(key)
            posted += 1
            if dry_run:
                who = ", ".join(p["name"] for p in people) or "?"
                print(f"   - {item['title']}  ({item['source'] or item['domain']}, {item['label']}) → {who}")
            else:
                post_to_discord(webhook, people, item)
                time.sleep(1)
        print(f"   posted {posted}, skipped {len(ordered) - posted} unrelated")

    save_json(SEEN_FILE, seen[-MAX_SEEN:])


if __name__ == "__main__":
    main()
