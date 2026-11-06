"""
Player news bot (GDELT version)
-------------------------------
Searches the GDELT DOC 2.0 API (free, no key, worldwide news in 65+ languages,
updated every ~15 min) for the players listed in players.json, drops anything
published by Bosnian sources, and posts new articles to a Discord channel.

State (already posted articles) is kept in seen.json, which the GitHub Action
commits back to the repo after every run.
"""

import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

CONFIG_FILE = "players.json"
SEEN_FILE = "seen.json"
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
MAX_SEEN = 3000              # keep the seen list from growing forever
FIRST_RUN_HOURS = 7 * 24     # first run: post everything from the last week
GDELT_PAUSE = 10             # GDELT asks for max. 1 request every 5 seconds (we stay well below)
RETRY_WAITS = [20, 40, 60, 90]  # waits after "Too Many Requests"
MAX_QUERY_LEN = 450          # keep each combined GDELT query reasonably short
HEADERS = {"User-Agent": "player-news-bot"}
COUNTRY_FILTER_OK = True     # set to False if GDELT rejects "-sourcecountry:bk"

# Second safety net: Bosnian portals that don't use a .ba domain.
# (Country filter + all .ba domains are handled automatically.)
BLOCKED_DOMAINS = {
    "nezavisne.com", "rtvbn.com", "atvbl.com", "srpskainfo.com", "bljesak.info",
    "hercegovina.info", "mostarski.info", "zurnal.info", "glassrpske.com",
    "rtrs.tv", "6yka.com", "bih-x.info",
}

# Language names as GDELT returns them -> flag for the Discord post
LANG_FLAGS = {
    "English": "🇬🇧", "German": "🇩🇪", "Italian": "🇮🇹", "Turkish": "🇹🇷",
    "Croatian": "🇭🇷", "Serbian": "🇷🇸", "Spanish": "🇪🇸", "French": "🇫🇷",
    "Portuguese": "🇵🇹", "Dutch": "🇳🇱", "Arabic": "🇸🇦", "Slovenian": "🇸🇮",
    "Russian": "🇷🇺", "Polish": "🇵🇱", "Greek": "🇬🇷", "Swedish": "🇸🇪",
}


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def clean_domain(d):
    d = (d or "").lower()
    if "/" in d:
        d = urlparse(d).netloc or d
    return d[4:] if d.startswith("www.") else d


def is_bosnian(article, extra_blocked):
    if "bosnia" in (article.get("sourcecountry") or "").lower():
        return True
    d = clean_domain(article.get("domain") or article.get("url"))
    if d.endswith(".ba"):
        return True
    return any(d == b or d.endswith("." + b) for b in BLOCKED_DOMAINS | extra_blocked)


def normalize(text):
    """Lowercase, strip accents and punctuation – used to spot the same story twice."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def ascii_name(name):
    """'Feđa Pervan' -> 'Fedja Pervan', 'Šošić' -> 'Sosic' (how foreign media usually write it)."""
    name = name.replace("đ", "dj").replace("Đ", "Dj")
    return unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()


def all_names(person):
    names = [person["name"]] + person.get("aliases", [])
    # add the version without č/ć/š/ž/đ automatically
    for n in list(names):
        a = ascii_name(n)
        if a not in names:
            names.append(a)
    return names


def or_group(terms):
    terms = [f'"{t}"' for t in terms]
    return terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")"


def build_groups(people):
    """Put many people into one GDELT query (fewer requests = no 'Too Many Requests').
    People are grouped by their 'context' words; the club searches come last."""
    buckets = {}
    for p in people:
        key = (p.get("role") == "Klub", tuple(p.get("context") or []))
        buckets.setdefault(key, []).append(p)

    groups = []
    for (is_club, context), members in sorted(buckets.items(), key=lambda kv: kv[0][0]):
        ctx = (" " + or_group(list(context))) if context else ""
        chunk, terms = [], []
        for p in members:
            new_terms = terms + all_names(p)
            if chunk and len(or_group(new_terms)) + len(ctx) > MAX_QUERY_LEN:
                groups.append((chunk, or_group(terms) + ctx))
                chunk, new_terms = [], all_names(p)
            chunk.append(p)
            terms = new_terms
        if chunk:
            groups.append((chunk, or_group(terms) + ctx))
    return groups


def gdelt_get(params):
    """GET with patient retries – GitHub runners share IPs, so GDELT often says 429."""
    for wait in RETRY_WAITS:
        r = requests.get(GDELT_URL, params=params, headers=HEADERS, timeout=40)
        if r.status_code != 429:
            r.raise_for_status()
            return r
        print(f"   GDELT busy (429), waiting {wait}s…")
        time.sleep(wait)
    r.raise_for_status()


def gdelt_search(query, hours):
    """Returns a list of article dicts. Tries to exclude Bosnian sources already in
    the query; if GDELT rejects that operator, falls back to filtering locally."""
    global COUNTRY_FILTER_OK
    variants = [query + " -sourcecountry:bk", query] if COUNTRY_FILTER_OK else [query]
    for q in variants:
        r = gdelt_get({
            "query": q,
            "mode": "ArtList",
            "format": "json",
            "maxrecords": 250,
            "sort": "DateDesc",
            "timespan": f"{hours}h",
        })
        text = r.text.strip()
        if not text or text == "{}":
            return []
        try:
            return r.json().get("articles", [])
        except ValueError:
            # GDELT answers errors as plain text (e.g. invalid country, query too long)
            print(f"   GDELT said: {text[:150]}")
            if "-sourcecountry" in q and "ountry" in text:
                COUNTRY_FILTER_OK = False  # don't try it again
                time.sleep(GDELT_PAUSE)
                continue
            return []
    return []


def who_is_it(art, members):
    """Which people from this query does the article mention? Checks title + URL."""
    if len(members) == 1:
        return members
    hay = " " + normalize((art.get("title") or "") + " " + (art.get("url") or "")) + " "
    full = [p for p in members if any(f" {normalize(ascii_name(n))} " in hay for n in all_names(p))]
    if full:
        return full
    # foreign headlines often use just the surname ("Hodzic scores")
    return [p for p in members if f" {normalize(ascii_name(p['name'].split()[-1]))} " in hay]


def parse_seendate(s):
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def describe(people):
    if not people:  # article matched the search, but we can't tell who exactly
        return "🔵 **FK Željezničar**  ·  igrač / stručni štab"
    lines = []
    for p in people[:3]:
        icon = p.get("icon", "👤")
        label = p.get("display", p["name"])
        lines.append(f"{icon} **{label}**" + (f"  ·  {p['role']}" if p.get("role") else ""))
    return "\n".join(lines)


def post_to_discord(webhook, people, art):
    lang = art.get("language", "")
    flag = LANG_FLAGS.get(lang, "🌍")
    embed = {
        "title": (art.get("title") or "(bez naslova)")[:256],
        "url": art["url"],
        "description": f"{describe(people)}\n🗞️ {clean_domain(art.get('domain'))}  ·  {flag} {lang}  ·  {art.get('sourcecountry', '')}",
        "color": 0x1E4FA3,
    }
    if art.get("socialimage"):
        embed["thumbnail"] = {"url": art["socialimage"]}
    seen_at = parse_seendate(art.get("seendate"))
    if seen_at:
        embed["timestamp"] = seen_at.isoformat()
    payload = {"username": "Vijesti o igračima", "embeds": [embed]}

    for _ in range(4):
        r = requests.post(webhook, json=payload, timeout=20)
        if r.status_code == 429:  # rate limited – wait and retry
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        if r.status_code == 400 and "thumbnail" in embed:  # bad image URL – retry without it
            embed.pop("thumbnail")
            continue
        r.raise_for_status()
        return
    raise RuntimeError("Discord rejected the post")


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
    extra_blocked = {clean_domain(d) for d in config.get("extra_blocked_domains", [])}
    only_langs = {l.lower() for l in config.get("only_languages", [])}
    hours = FIRST_RUN_HOURS if first_run else int(config.get("max_age_hours", 48))
    if first_run:
        print("First run → posting all news from the last 7 days.")

    groups = build_groups(config["players"])
    print(f"{len(config['players'])} people in {len(groups)} searches")

    for i, (members, query) in enumerate(groups):
        if i:
            time.sleep(GDELT_PAUSE)
        names = ", ".join(p["name"] for p in members)
        try:
            articles = gdelt_search(query, hours)
        except Exception as e:  # one failing search shouldn't stop the rest
            print(f"[{names}] fetch failed: {e}")
            continue

        candidates = {}
        for art in articles:
            if not art.get("url") or is_bosnian(art, extra_blocked):
                continue
            if only_langs and (art.get("language") or "").lower() not in only_langs:
                continue
            # same headline on several portals (or a re-translated copy) = one story
            key = hashlib.sha1(normalize(art.get("title") or art["url"]).encode()).hexdigest()[:16]
            if key in seen_set or key in candidates:
                continue
            candidates[key] = art

        # oldest first, so Discord shows them in chronological order
        ordered = sorted(candidates.items(), key=lambda kv: kv[1].get("seendate", ""))
        print(f"[{names}] {len(articles)} found, {len(ordered)} new to post")
        for key, art in ordered:
            people = who_is_it(art, members)
            if dry_run:
                who = ", ".join(p["name"] for p in people) or "?"
                print(f"   - {art.get('title')}  ({clean_domain(art.get('domain'))}, {art.get('language')}) → {who}")
            else:
                post_to_discord(webhook, people, art)
                time.sleep(1)
            seen_set.add(key)
            seen.append(key)

    save_json(SEEN_FILE, seen[-MAX_SEEN:])


if __name__ == "__main__":
    main()
