#!/usr/bin/env python3
"""Catalyst scanner: flags fresh stock catalysts from primary sources.

Sources:
  * SEC EDGAR live 8-K feed (partnerships/contracts land as Item 1.01,
    press releases as Item 7.01/8.01).
  * Generic RSS handler for newswires (Business Wire, GlobeNewswire,
    PR Newswire).

State lives in data/ as JSON so a GitHub Actions cron can commit new hits
back to the repo (which triggers a Netlify rebuild of the dashboard).

Environment:
  SEC_USER_AGENT  required for live runs, e.g. "MyApp you@example.com".
                  SEC blocks requests without a real contact User-Agent.
  NTFY_TOPIC      optional; new hits are pushed to https://ntfy.sh/<topic>.

Usage:
  python3 catalyst_scanner.py             # one scan pass (what the cron runs)
  python3 catalyst_scanner.py --selftest  # offline check of the matchers
  python3 catalyst_scanner.py --limit 10  # only deep-fetch 10 filings (testing)
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
HITS_FILE = DATA_DIR / "hits.json"
SEEN_FILE = DATA_DIR / "seen.json"
TICKERS_FILE = DATA_DIR / "tickers.json"

SEC_ATOM_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
    "&type=8-K&company=&dateb=&owner=include&count=100&output=atom"
)
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# Newswire RSS feeds. Failures are logged per feed and never fatal, so a
# feed that changes its URL just drops out until fixed here. Business Wire
# deactivated its public RSS channels; add a working one here if you find it.
RSS_FEEDS = [
    ("GlobeNewswire", "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies"),
    ("PR Newswire", "https://www.prnewswire.com/rss/news-releases-list.rss"),
]

# Phrases that mark a catalyst. Matched case-insensitively on word
# boundaries; bare "agreement"/"partner" alone is too noisy.
KEYWORDS = [
    "strategic partnership",
    "expanded partnership",
    "strategic alliance",
    "strategic collaboration",
    "collaboration agreement",
    "partnership agreement",
    "definitive agreement",
    "material definitive agreement",
    "awarded a contract",
    "awarded contract",
    "contract award",
    "wins contract",
    "multi-year agreement",
    "multi-year contract",
    "supply agreement",
    "licensing agreement",
    "license agreement",
    "distribution agreement",
    "joint venture",
    "memorandum of understanding",
    "letter of intent",
    "purchase order",
]

# Large partners that make a small-cap catalyst interesting ("samarbejde
# med store virksomheder"). Co-mentions are recorded on the hit.
MEGACAPS = [
    "Nvidia", "Microsoft", "Amazon", "Google", "Alphabet", "Apple", "Meta",
    "Tesla", "OpenAI", "Anthropic", "Oracle", "Palantir", "Brookfield",
    "Lockheed Martin", "Boeing", "RTX", "Raytheon", "Northrop Grumman",
    "Department of Defense", "U.S. Army", "U.S. Navy", "U.S. Air Force",
    "SpaceX", "Walmart", "Costco", "Target Corporation", "Home Depot",
    "Pfizer", "Merck", "Eli Lilly", "Novo Nordisk", "AstraZeneca",
    "Johnson & Johnson", "Exxon", "Chevron", "Shell",
]

# 8-K items that are a hit on their own. 1.01 = Entry into a Material
# Definitive Agreement — where signed partnerships/contracts land.
HOT_ITEMS = {"1.01"}

MAX_HITS_KEPT = 500
SEEN_MAX_AGE_DAYS = 7
SEC_REQUEST_DELAY = 0.15  # stay well under SEC's 10 req/s limit
MAX_DOC_FETCHES = 40      # deep-fetch cap per run
MAX_NOTIFICATIONS = 10

_KEYWORD_RES = [re.compile(r"\b" + re.escape(k).replace(r"\ ", r"\s+") + r"\b", re.I)
                for k in KEYWORDS]
_MEGACAP_RES = [(m, re.compile(r"\b" + re.escape(m) + r"\b", re.I)) for m in MEGACAPS]
_ITEM_RE = re.compile(r"Item\s+(\d+\.\d+)\s*:\s*([^<\n]+)")
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)


def log(msg):
    print("[catalyst] " + msg, flush=True)


# ---------------------------------------------------------------- matching

def match_keywords(text):
    """Return the list of catalyst keywords found in text."""
    return [k for k, rx in zip(KEYWORDS, _KEYWORD_RES) if rx.search(text)]


def match_megacaps(text):
    """Return the list of megacap partners mentioned in text."""
    return [name for name, rx in _MEGACAP_RES if rx.search(text)]


def parse_items(summary_html):
    """Extract 8-K item numbers/titles from an EDGAR atom <summary>."""
    return [(num, title.strip()) for num, title in _ITEM_RE.findall(summary_html)]


def html_to_text(raw):
    text = _SCRIPT_RE.sub(" ", raw)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text)


# ------------------------------------------------------------------- http

def http_get(url, timeout=30):
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua:
        raise RuntimeError(
            "SEC_USER_AGENT is not set. SEC requires a contact User-Agent, "
            'e.g. export SEC_USER_AGENT="CatalystScanner you@example.com"'
        )
    req = urllib.request.Request(url, headers={
        "User-Agent": ua,
        "Accept-Encoding": "identity",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ------------------------------------------------------------------ state

def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (ValueError, OSError):
            log("warning: could not read %s, starting fresh" % path.name)
    return default


def save_json(path, data):
    DATA_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")


def prune_seen(seen):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SEEN_MAX_AGE_DAYS)).isoformat()
    return {k: v for k, v in seen.items() if v >= cutoff}


def load_ticker_map():
    """CIK -> ticker map from SEC, cached for 7 days in data/tickers.json."""
    cached = load_json(TICKERS_FILE, None)
    now = datetime.now(timezone.utc)
    if cached and cached.get("fetched", "") >= (now - timedelta(days=7)).isoformat():
        return cached["map"]
    try:
        raw = json.loads(http_get(SEC_TICKERS_URL))
        cik_map = {}
        for row in raw.values():  # keep first entry per CIK = primary listing
            cik_map.setdefault(str(row["cik_str"]), row["ticker"])
        save_json(TICKERS_FILE, {"fetched": now.isoformat(), "map": cik_map})
        return cik_map
    except Exception as exc:  # ticker names are nice-to-have, never fatal
        log("warning: ticker map unavailable (%s)" % exc)
        return (cached or {}).get("map", {})


# -------------------------------------------------------------- SEC 8-K

def find_primary_doc(index_url):
    """Return URL of the filing's primary .htm document, or None.

    Heuristic on the folder's index.json: largest .htm that is not the
    EDGAR-generated index or an XBRL R-file.
    """
    folder = index_url.rsplit("/", 1)[0]
    listing = json.loads(http_get(folder + "/index.json"))
    best = None
    for item in listing.get("directory", {}).get("item", []):
        name = item.get("name", "")
        if not name.lower().endswith(".htm"):
            continue
        if "-index" in name or re.match(r"^R\d+\.htm$", name):
            continue
        size = int(item.get("size") or 0)
        if best is None or size > best[0]:
            best = (size, name)
    return folder + "/" + best[1] if best else None


def scan_sec_8k(seen, ticker_map, doc_limit):
    """Scan EDGAR's live 8-K feed; return list of new hits."""
    hits = []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    feed = ET.fromstring(http_get(SEC_ATOM_URL))
    entries = feed.findall("a:entry", ns)
    log("SEC feed: %d entries" % len(entries))
    fetched = 0

    for entry in entries:
        entry_id = entry.findtext("a:id", "", ns)
        if not entry_id or entry_id in seen:
            continue
        seen[entry_id] = datetime.now(timezone.utc).isoformat()

        title = entry.findtext("a:title", "", ns)
        link_el = entry.find("a:link", ns)
        url = link_el.get("href") if link_el is not None else ""
        summary = entry.findtext("a:summary", "", ns)
        filed = entry.findtext("a:updated", "", ns)

        m = re.match(r"(.+?) - (.+?) \((\d+)\)", title)
        form, company, cik = m.groups() if m else (title, title, "")
        ticker = ticker_map.get(cik.lstrip("0"), "")

        items = parse_items(summary)
        item_nums = [n for n, _ in items]
        hot = [n for n in item_nums if n in HOT_ITEMS]

        keywords, megacaps = [], []
        if fetched < doc_limit:
            try:
                time.sleep(SEC_REQUEST_DELAY)
                doc_url = find_primary_doc(url)
                if doc_url:
                    time.sleep(SEC_REQUEST_DELAY)
                    text = html_to_text(http_get(doc_url))[:400_000]
                    keywords = match_keywords(text)
                    megacaps = match_megacaps(text)
                fetched += 1
            except Exception as exc:
                log("warning: deep fetch failed for %s (%s)" % (company, exc))

        if not hot and not keywords:
            continue

        reason = []
        if hot:
            reason.append("Item 1.01 (Material Definitive Agreement)")
        if keywords:
            reason.append("keywords: " + ", ".join(keywords[:5]))
        hits.append({
            "id": entry_id,
            "source": "SEC 8-K",
            "company": company,
            "ticker": ticker,
            "cik": cik,
            "title": "%s — %s" % (form, company),
            "url": url,
            "published": filed,
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "items": ["%s %s" % (n, t) for n, t in items],
            "keywords": keywords,
            "megacaps": megacaps,
            "reason": "; ".join(reason),
        })
    return hits


# ----------------------------------------------------------------- RSS

def scan_rss(source, feed_url, seen):
    """Generic newswire RSS scanner; returns new keyword hits."""
    hits = []
    root = ET.fromstring(http_get(feed_url))
    channel_items = root.findall(".//item")
    log("%s: %d items" % (source, len(channel_items)))

    for item in channel_items:
        guid = (item.findtext("guid") or item.findtext("link") or "").strip()
        if not guid:
            continue
        key = "rss:" + guid
        if key in seen:
            continue
        seen[key] = datetime.now(timezone.utc).isoformat()

        title = (item.findtext("title") or "").strip()
        desc = html_to_text(item.findtext("description") or "")
        text = title + " " + desc
        keywords = match_keywords(text)
        if not keywords:
            continue
        hits.append({
            "id": key,
            "source": source,
            "company": "",
            "ticker": "",
            "cik": "",
            "title": title,
            "url": (item.findtext("link") or "").strip(),
            "published": (item.findtext("pubDate") or "").strip(),
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "items": [],
            "keywords": keywords,
            "megacaps": match_megacaps(text),
            "reason": "keywords: " + ", ".join(keywords[:5]),
        })
    return hits


# ---------------------------------------------------------------- alerts

def notify(hits):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    for hit in hits[:MAX_NOTIFICATIONS]:
        label = hit["ticker"] or hit["company"] or hit["source"]
        body = "%s\n%s\n%s" % (hit["title"], hit["reason"], hit["url"])
        try:
            req = urllib.request.Request(
                "https://ntfy.sh/" + topic,
                data=body.encode(),
                headers={"Title": "Catalyst: %s" % label, "Tags": "chart_with_upwards_trend"},
            )
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as exc:
            log("warning: ntfy failed (%s)" % exc)


# ------------------------------------------------------------------ main

def run(doc_limit):
    seen = prune_seen(load_json(SEEN_FILE, {}))
    hits = load_json(HITS_FILE, [])
    known_ids = {h["id"] for h in hits}
    ticker_map = load_ticker_map()

    new_hits = []
    try:
        new_hits += scan_sec_8k(seen, ticker_map, doc_limit)
    except Exception as exc:
        log("error: SEC scan failed (%s)" % exc)
    for source, feed_url in RSS_FEEDS:
        try:
            new_hits += scan_rss(source, feed_url, seen)
        except Exception as exc:
            log("warning: %s scan failed (%s)" % (source, exc))

    new_hits = [h for h in new_hits if h["id"] not in known_ids]
    if new_hits:
        hits = sorted(new_hits + hits, key=lambda h: h["detected_at"], reverse=True)
        save_json(HITS_FILE, hits[:MAX_HITS_KEPT])
        notify(new_hits)
    save_json(SEEN_FILE, seen)

    log("done: %d new hit(s), %d total" % (len(new_hits), len(hits)))
    for hit in new_hits:
        log("  HIT [%s] %s — %s" % (hit["source"], hit["title"], hit["reason"]))
    return new_hits


# -------------------------------------------------------------- selftest

SELFTEST_SUMMARY = """
 <b>Filed:</b> 2026-07-01 <b>AccNo:</b> 0001213900-26-074223 <b>Size:</b> 225 KB
<br>Item 1.01: Entry into a Material Definitive Agreement
<br>Item 5.07: Submission of Matters to a Vote of Security Holders
"""

SELFTEST_DOC = """
<html><head><style>p{color:red}</style></head><body>
<p>On July 1, 2026, the Company entered into a strategic&nbsp;partnership
with NVIDIA Corporation and was awarded a contract by the U.S. Army.</p>
<script>var x = "no partnership here should not count twice";</script>
</body></html>
"""


def selftest():
    failures = []

    checks = [0]

    def check(name, cond):
        checks[0] += 1
        log("%s %s" % ("PASS" if cond else "FAIL", name))
        if not cond:
            failures.append(name)

    items = parse_items(SELFTEST_SUMMARY)
    check("item parsing finds 1.01 and 5.07",
          [n for n, _ in items] == ["1.01", "5.07"])
    check("item 1.01 title", items[0][1] == "Entry into a Material Definitive Agreement")

    text = html_to_text(SELFTEST_DOC)
    check("html_to_text strips script/style", "color:red" not in text and "var x" not in text)
    check("html_to_text unescapes &nbsp;", "strategic partnership" in text.lower())

    kw = match_keywords(text)
    check("keyword: strategic partnership", "strategic partnership" in kw)
    check("keyword: awarded a contract", "awarded a contract" in kw)
    check("no false keyword on plain text", match_keywords("the quarterly report was filed") == [])

    caps = match_megacaps(text)
    check("megacap: Nvidia", "Nvidia" in caps)
    check("megacap: U.S. Army", "U.S. Army" in caps)

    title_match = re.match(r"(.+?) - (.+?) \((\d+)\)",
                           "8-K - Singularity Future Technology Ltd. (0001422892) (Filer)")
    check("feed title parsing", title_match is not None
          and title_match.group(3) == "0001422892")

    if failures:
        log("SELFTEST FAILED: %s" % ", ".join(failures))
        return 1
    log("selftest OK (%d checks)" % checks[0])
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true", help="offline matcher checks")
    ap.add_argument("--limit", type=int, default=MAX_DOC_FETCHES,
                    help="max filings to deep-fetch this run")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(selftest())
    run(args.limit)


if __name__ == "__main__":
    main()
