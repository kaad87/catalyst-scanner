#!/usr/bin/env python3
"""Catalyst scanner: flags fresh stock catalysts from primary sources.

Sources:
  * SEC EDGAR live 8-K feed (partnerships/contracts land as Item 1.01,
    press releases as Item 7.01/8.01).
  * Generic RSS handler for newswires (GlobeNewswire, PR Newswire).

Each hit is scored (positive keywords, megacap co-mentions, Item 1.01;
financing/dilution terms count against) and tiered A/B/C so the dashboard
can separate real catalyst candidates from credit-facility noise. Hits are
enriched with market cap (SEC shares outstanding x Yahoo price) and price
change since detection, and optionally annotated with a one-line Danish
summary + category via the OpenAI API.

State lives in data/ as JSON so a GitHub Actions cron can commit new hits
back to the repo (which triggers a Netlify rebuild of the dashboard).

Environment:
  SEC_USER_AGENT  required for live runs, e.g. "MyApp you@example.com".
                  SEC blocks requests without a real contact User-Agent.
  NTFY_TOPIC      optional; new A/B-tier hits push to https://ntfy.sh/<topic>.
  OPENAI_API_KEY  optional; enables AI summaries for A/B-tier hits.
  OPENAI_MODEL    optional; default gpt-4o-mini.

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
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
HITS_FILE = DATA_DIR / "hits.json"
SEEN_FILE = DATA_DIR / "seen.json"
TICKERS_FILE = DATA_DIR / "tickers.json"
HEALTH_FILE = DATA_DIR / "health.json"
INSIDER_FILE = DATA_DIR / "insider.json"

SEC_ATOM_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
    "&type=8-K&company=&dateb=&owner=include&count=100&output=atom"
)
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SHARES_URL = ("https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:0>10}"
                  "/dei/EntityCommonStockSharesOutstanding.json")
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

# Newswire RSS feeds: (source, url, keyword_override, max_age_hours).
# keyword_override None = the global catalyst KEYWORDS; a list scopes
# matching to source-specific terms. max_age_hours guards deep-archive
# feeds against flooding the tape (Google News queries return ~100 items
# regardless of age). Failures are logged per feed and never fatal.
# Business Wire deactivated its public RSS channels; DoD's contracts feed
# died in the defense.gov->war.gov move.
RSS_FEEDS = [
    ("GlobeNewswire", "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies", None, 48),
    ("PR Newswire", "https://www.prnewswire.com/rss/news-releases-list.rss", None, 48),
    # fda.gov blocks datacenter IPs (404 from GitHub runners); Google News
    # carries the same approvals and usually names the company in the title.
    ("FDA", "https://news.google.com/rss/search?q=%22FDA+approves%22+OR+%22FDA+clears%22+OR+%22FDA+authorizes%22+OR+%22FDA+grants%22&hl=en-US&gl=US&ceid=US:en", [
        "approves", "approval", "authorizes", "authorization", "clearance",
        "clears", "breakthrough therapy", "fast track", "priority review",
        "recall",
    ], 6),
]

# Social accounts of market movers: (person, platform, feed_url). Posts are
# prefiltered for market-ish content, then AI-gated so only company-relevant
# posts survive. X/Twitter has no free feed (mirrors are bot-walled); add a
# working mirror here if you find one, e.g.
#   ("Musk", "X", "https://<working-nitter>/elonmusk/rss"),
SOCIAL_FEEDS = [
    ("Trump", "Truth Social", "https://trumpstruth.org/feed"),
]

# Cheap prefilter before spending AI calls on a social post.
SOCIAL_MARKET_RE = re.compile(
    r"\$[A-Z]{2,5}\b|\b(tariff|tariffs|stock|stocks|shares|market|markets|"
    r"merger|acquisition|deal|deals|contract|contracts|trade|company|"
    r"companies|factory|factories|production|chip|chips|semiconductor|drug|"
    r"pharma|oil|gas|energy|ipo|earnings|invest|investment|subsid|sanction|"
    r"sanctions|export|import|bank|banks|crypto|bitcoin)\b", re.I)

# Phrases that mark a catalyst. Matched case-insensitively on word
# boundaries. NOTE: "definitive agreement" is deliberately absent — the
# Item 1.01 heading puts that phrase in every 1.01 filing, including
# credit facilities; the item flag itself carries that signal.
KEYWORDS = [
    "strategic partnership",
    "expanded partnership",
    "strategic alliance",
    "strategic collaboration",
    "collaboration agreement",
    "partnership agreement",
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

# Contract awards move stocks harder than generic partnerships; weigh up.
CONTRACT_KEYWORDS = {
    "awarded a contract", "awarded contract", "contract award",
    "wins contract", "purchase order",
}

# Financing/comp/lease terms: the bulk of Item 1.01 noise, often negative
# (dilution). Each distinct match subtracts from the score.
NEGATIVE_KEYWORDS = [
    "credit agreement",
    "credit facility",
    "revolving credit",
    "term loan",
    "loan agreement",
    "promissory note",
    "securities purchase agreement",
    "note purchase agreement",
    "subscription agreement",
    "at-the-market",
    "equity distribution agreement",
    "open market sale agreement",
    "underwriting agreement",
    "convertible note",
    "warrant",
    "indenture",
    "employment agreement",
    "separation agreement",
    "severance",
    "lease agreement",
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

# 8-K items that contribute to the score on their own. 1.01 = Entry into
# a Material Definitive Agreement — where signed partnerships/contracts land.
HOT_ITEMS = {"1.01"}

TIER_A_MIN = 6   # strong candidate
TIER_B_MIN = 2   # possible
CAP_SMALL = 2_000_000_000
CAP_MID = 10_000_000_000

MAX_HITS_KEPT = 500
SEEN_MAX_AGE_DAYS = 7
SEC_REQUEST_DELAY = 0.15   # stay well under SEC's 10 req/s limit
MAX_DOC_FETCHES = 40       # deep-fetch cap per run
MAX_NOTIFICATIONS = 10
MAX_AI_CALLS = 15          # per run; A/B-tier hits only
MAX_SOCIAL_AI = 8          # per run; AI relevance gate for social posts
PRICE_REFRESH_HOURS = 80   # keep updating price_now for hits this fresh (covers 3d snapshot)
MAX_PRICE_REFRESH = 30     # Yahoo lookups per run for the refresh pass
MAX_FORM4_FETCHES = 25     # Form 4 deep-fetch cap per run
MIN_INSIDER_BUY = 100_000  # USD; open-market buys below this are ignored
INSIDER_CLUSTER_DAYS = 30  # window for counting distinct insider buyers
INSIDER_LEDGER_KEEP_DAYS = 40
# Research: a CEO/CFO open-market buy is far more predictive than a director's,
# and several insiders buying at once (a cluster) is the strongest tell.
_TOP_EXEC_RE = re.compile(r"\b(CEO|CFO|COO|chief|president|chair)\b", re.I)

# Theme radar (an information edge, not sentiment): SEC full-text search
# surfaces companies filing about an emerging theme — catch a small-cap
# associating itself with a hot narrative before it's mainstream. Curated
# and easily edited: (search phrase, dashboard label).
THEMES = [
    ("GLP-1", "GLP-1 / vægttab"),
    ("small modular reactor", "Atomkraft (SMR)"),
    ("quantum computing", "Kvantecomputere"),
    ("stablecoin", "Stablecoins"),
    ("humanoid robot", "Humanoide robotter"),
    ("sovereign AI", "Suveræn AI"),
    ("rare earth", "Sjældne jordarter"),
    ("nuclear fusion", "Fusionsenergi"),
]
SEC_FTS_URL = "https://efts.sec.gov/LATEST/search-index"
THEME_FORMS = "8-K,S-1,424B4,425,6-K,10-Q"  # skip routine 10-K/proxy noise
THEME_MAX_PER_THEME = 8

_KEYWORD_RES = [re.compile(r"\b" + re.escape(k).replace(r"\ ", r"\s+") + r"\b", re.I)
                for k in KEYWORDS]
_NEGATIVE_RES = [re.compile(r"\b" + re.escape(k).replace(r"\ ", r"\s+") + r"\b", re.I)
                 for k in NEGATIVE_KEYWORDS]
_MEGACAP_RES = [(m, re.compile(r"\b" + re.escape(m) + r"\b", re.I)) for m in MEGACAPS]
_ITEM_RE = re.compile(r"Item\s+(\d+\.\d+)\s*:\s*([^<\n]+)")
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)


def log(msg):
    print("[catalyst] " + msg, flush=True)


# ---------------------------------------------------------------- matching

def compile_keywords(words):
    return [(k, re.compile(r"\b" + re.escape(k).replace(r"\ ", r"\s+") + r"\b", re.I))
            for k in words]


_KEYWORD_CACHE = {}


def match_keyword_list(text, words):
    """Match text against an arbitrary keyword list (compiled once)."""
    key = tuple(words)
    if key not in _KEYWORD_CACHE:
        _KEYWORD_CACHE[key] = compile_keywords(words)
    return [k for k, rx in _KEYWORD_CACHE[key] if rx.search(text)]


def match_keywords(text):
    """Return the list of catalyst keywords found in text."""
    return [k for k, rx in zip(KEYWORDS, _KEYWORD_RES) if rx.search(text)]


def match_negative(text):
    """Return the list of financing/noise keywords found in text."""
    return [k for k, rx in zip(NEGATIVE_KEYWORDS, _NEGATIVE_RES) if rx.search(text)]


def match_megacaps(text):
    """Return the list of megacap partners mentioned in text."""
    return [name for name, rx in _MEGACAP_RES if rx.search(text)]


def is_stale(pubdate_str, hours=48):
    """True if an RSS pubDate is parseable and older than `hours`.

    Guards against first-run floods from feeds with deep archives
    (e.g. Google News queries return ~100 items regardless of age).
    """
    try:
        dt = parsedate_to_datetime(pubdate_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt < datetime.now(timezone.utc) - timedelta(hours=hours)
    except (TypeError, ValueError):
        return False


def parse_items(summary_html):
    """Extract 8-K item numbers/titles from an EDGAR atom <summary>."""
    return [(num, title.strip()) for num, title in _ITEM_RE.findall(summary_html)]


def html_to_text(raw):
    text = _SCRIPT_RE.sub(" ", raw)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text)


# ----------------------------------------------------------------- scoring

def score_hit(keywords, megacaps, item101, negatives):
    """Score a hit; see module docstring for the rationale."""
    score = 0
    for k in keywords[:5]:
        score += 3 if k in CONTRACT_KEYWORDS else 2
    if megacaps:
        score += 3
    if item101:
        score += 1
    score -= 3 * min(len(negatives), 3)
    return score


def tier_for(score):
    if score >= TIER_A_MIN:
        return "A"
    if score >= TIER_B_MIN:
        return "B"
    return "C"


def cap_bucket(market_cap):
    if not market_cap:
        return None
    if market_cap < CAP_SMALL:
        return "small"
    if market_cap < CAP_MID:
        return "mid"
    return "large"


def adjust_tier_after_ai(hit):
    """Let the AI classification veto/boost the keyword-based tier."""
    cat = hit.get("ai_category")
    if cat in ("financing", "dilution") and hit["tier"] != "C":
        hit["tier"] = "C"
        hit["reason"] += "; AI: " + cat
    elif (cat in ("partnership", "contract_award")
          and hit.get("ai_materiality") == "high" and hit["tier"] == "B"):
        hit["tier"] = "A"


# ------------------------------------------------------------------- http

def http_get(url, timeout=30, browser=False):
    if browser:
        ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    else:
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


# ------------------------------------------------------------- enrichment

def fetch_quote(ticker):
    """(price, volume_ratio) from one Yahoo chart call, or (None, None).

    volume_ratio = today's volume vs. the average of the prior sessions —
    already-at-1.5x intraday means the market is reacting to something.
    """
    try:
        url = YAHOO_CHART_URL.format(ticker=urllib.parse.quote(ticker)) + "?range=10d&interval=1d"
        data = json.loads(http_get(url, timeout=15, browser=True))
        result = data["chart"]["result"][0]
        price = result["meta"].get("regularMarketPrice")
        price = float(price) if price else None
        ratio = None
        vols = [v for v in (result.get("indicators", {}).get("quote", [{}])[0]
                            .get("volume") or []) if v]
        if len(vols) >= 4:
            baseline = sum(vols[:-1]) / len(vols[:-1])
            if baseline > 0:
                ratio = round(vols[-1] / baseline, 1)
        return price, ratio
    except Exception:
        return None, None


def fetch_price(ticker):
    return fetch_quote(ticker)[0]


_spy_cache = {}


def spy_price():
    """SPY price, fetched once per run (the market baseline for alpha)."""
    if "p" not in _spy_cache:
        _spy_cache["p"] = fetch_price("SPY")
    return _spy_cache["p"]


def spy_field(price_field):
    """price_1d -> spy_1d, price_at_detect -> spy_at_detect."""
    return "spy_" + price_field.split("_", 1)[1]


def fetch_shares(cik):
    """Shares outstanding from SEC companyfacts, or None."""
    try:
        time.sleep(SEC_REQUEST_DELAY)
        data = json.loads(http_get(SEC_SHARES_URL.format(cik=cik), timeout=15))
        facts = []
        for unit in data.get("units", {}).values():
            facts.extend(unit)
        facts = [f for f in facts if f.get("val")]
        if not facts:
            return None
        latest = max(facts, key=lambda f: f.get("end", ""))
        return int(latest["val"])
    except Exception:
        return None


def enrich_hit(hit):
    """Attach market cap, detection price and volume ratio. Fail-soft."""
    if not hit["ticker"]:
        return
    price, ratio = fetch_quote(hit["ticker"])
    hit["price_at_detect"] = price
    hit["price_now"] = price
    hit["price_change_pct"] = 0.0 if price else None
    hit["price_updated_at"] = datetime.now(timezone.utc).isoformat()
    hit["volume_ratio"] = ratio
    if price:
        hit["spy_at_detect"] = spy_price()
    if price and hit.get("cik"):
        shares = fetch_shares(hit["cik"].lstrip("0") or "0")
        if shares:
            hit["market_cap"] = int(price * shares)
            hit["cap_bucket"] = cap_bucket(hit["market_cap"])


# Outcome snapshots for the track record:
# (field, due after N hours, useless after M hours). A snapshot taken far
# past its horizon poisons the stats (the initial backfill proved it —
# identical 1h/1d columns), so late ones are skipped, not approximated.
# 20d captures post-earnings drift, which plays out over weeks.
SNAPSHOTS = [("price_1h", 1, 6), ("price_1d", 24, 48), ("price_3d", 72, 168),
             ("price_20d", 480, 960)]


def due_snapshots(hit, now):
    """Snapshot fields that are due, unfilled and still within their window."""
    age_h = (now - datetime.fromisoformat(hit["detected_at"])).total_seconds() / 3600
    return [f for f, hrs, max_h in SNAPSHOTS
            if hit.get(f) is None and hrs <= age_h <= max_h]


def refresh_prices(hits):
    """Update live prices and fill due outcome snapshots (track record)."""
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=PRICE_REFRESH_HOURS)).isoformat()
    trackable = [h for h in hits if h["ticker"] and h.get("price_at_detect")]
    # hits with due snapshots first — that data is lost if we skip it long enough
    queue = [h for h in trackable if due_snapshots(h, now)]
    queue += [h for h in trackable if h["detected_at"] >= cutoff and h not in queue]
    updated = snapped = 0
    for hit in queue[:MAX_PRICE_REFRESH]:
        price, ratio = fetch_quote(hit["ticker"])
        if price:
            hit["price_now"] = price
            hit["price_change_pct"] = round(
                (price - hit["price_at_detect"]) / hit["price_at_detect"] * 100, 2)
            hit["price_updated_at"] = now.isoformat()
            if ratio and hit["detected_at"] >= (now - timedelta(hours=24)).isoformat():
                hit["volume_ratio"] = max(ratio, hit.get("volume_ratio") or 0)
            for field in due_snapshots(hit, now):
                hit[field] = price
                hit[spy_field(field)] = spy_price()  # market baseline at the same moment
                snapped += 1
            updated += 1
        time.sleep(0.2)
    if updated:
        log("price refresh: %d hit(s), %d snapshot(s)" % (updated, snapped))


# ------------------------------------------------------------ AI annotate

AI_SYSTEM_PROMPT = (
    "Du er aktieanalytiker. Du får teksten fra en SEC 8-K-filing eller "
    "pressemeddelelse. Svar KUN med JSON: {\"summary\": \"én kort sætning på "
    "dansk om hvad aftalen konkret er og med hvem\", \"category\": "
    "\"partnership|contract_award|financing|dilution|merger|other\", "
    "\"materiality\": \"high|medium|low\", \"tickers\": [\"CTSH\"]}. "
    "tickers = primære amerikanske tickers for de børsnoterede selskaber i "
    "teksten (hovedaktøren først; tom liste hvis ingen er børsnoterede). "
    "materiality = hvor væsentlig aftalen virker for selskabets omsætning. "
    "Udvandende finansiering (securities purchase, ATM, warrants) er dilution."
)


AI_CATEGORIES = ("partnership", "contract_award", "financing", "dilution",
                 "merger", "policy", "other")

SOCIAL_AI_PROMPT = (
    "Du er aktieanalytiker. Du får et opslag fra en person der kan flytte "
    "markedet (fx Trump eller Musk). Vurdér om opslaget er relevant for "
    "specifikke virksomheder/sektorer på aktiemarkedet. Svar KUN med JSON: "
    "{\"relevant\": true/false, \"summary\": \"én kort sætning på dansk om "
    "hvad opslaget betyder for hvilke aktier/sektorer\", \"category\": "
    "\"partnership|contract_award|financing|dilution|merger|policy|other\", "
    "\"materiality\": \"high|medium|low\", \"tickers\": [\"TSLA\"]}. "
    "Politik uden virksomheds-/sektorkonsekvens er ikke relevant."
)


def parse_ai_response(raw):
    """Parse the model's JSON reply into the three ai_* fields, or None."""
    try:
        content = json.loads(raw)["choices"][0]["message"]["content"]
        obj = json.loads(content)
        cat = obj.get("category")
        mat = obj.get("materiality")
        summary = str(obj.get("summary", "")).strip()
        if cat not in AI_CATEGORIES:
            cat = "other"
        if mat not in ("high", "medium", "low"):
            mat = "low"
        if not summary:
            return None
        tickers = obj.get("tickers") or []
        return {"ai_summary": summary[:300], "ai_category": cat, "ai_materiality": mat,
                "ai_tickers": [str(t).upper()[:6] for t in tickers if t][:8]}
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def parse_social_response(raw):
    """Parse the social relevance gate reply: fields + relevant flag, or None."""
    fields = parse_ai_response(raw)
    if fields is None:
        return None
    try:
        obj = json.loads(json.loads(raw)["choices"][0]["message"]["content"])
        fields["relevant"] = bool(obj.get("relevant"))
        return fields
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def should_adopt_ai_ticker(hit, now=None):
    """Adopt the AI-extracted ticker for price tracking?

    Only when the hit has no SEC ticker, the AI found one, and detection is
    recent enough that today's price is still an honest baseline.
    """
    if hit["ticker"] or not hit.get("ai_tickers"):
        return False
    now = now or datetime.now(timezone.utc)
    age_h = (now - datetime.fromisoformat(hit["detected_at"])).total_seconds() / 3600
    return age_h <= 2


def openai_chat(system_prompt, user_content):
    """Raw OpenAI chat call; returns the response body or None. Fail-soft."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    model = os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"  # env can be set-but-empty
    payload = json.dumps({
        "model": model,
        "response_format": {"type": "json_object"},
        "max_completion_tokens": 200,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    }).encode()
    req = urllib.request.Request(OPENAI_URL, data=payload, headers={
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=45) as resp:
        return resp.read().decode()


def ai_annotate(hit, text):
    """One-line Danish summary + classification via OpenAI. Fail-soft.

    Social hits go through the relevance gate instead: an irrelevant
    classification demotes the hit to C.
    """
    if not os.environ.get("OPENAI_API_KEY"):
        return False
    try:
        if hit.get("social"):
            fields = parse_social_response(
                openai_chat(SOCIAL_AI_PROMPT, "%s\n\n%s" % (hit["title"], text[:6000])))
            if fields:
                relevant = fields.pop("relevant")
                hit.update(fields)
                if not relevant:
                    hit["tier"] = "C"
                    hit["reason"] += "; AI: ikke virksomhedsrelevant"
                elif fields.get("ai_materiality") == "high":
                    hit["tier"] = "A"
                return True
        else:
            fields = parse_ai_response(
                openai_chat(AI_SYSTEM_PROMPT, "%s\n\n%s" % (hit["title"], text[:6000])))
            if fields:
                hit.update(fields)
                adjust_tier_after_ai(hit)
                return True
    except Exception as exc:
        log("warning: AI annotation failed for %s (%s)" % (hit["title"][:40], exc))
    return False


def refetch_text(hit):
    """Re-fetch source text for an existing hit (used by AI backfill)."""
    try:
        if hit["source"] == "SEC 8-K":
            doc = find_primary_doc(hit["url"])
            if doc:
                time.sleep(SEC_REQUEST_DELAY)
                return html_to_text(http_get(doc))[:400_000]
        elif hit["url"]:
            return html_to_text(http_get(hit["url"], browser=True))[:400_000]
    except Exception as exc:
        log("warning: text refetch failed for %s (%s)" % (hit["title"][:40], exc))
    return ""


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


def make_hit(**fields):
    """Hit skeleton with all enrichment fields present (dashboard-friendly)."""
    hit = {
        "id": "", "source": "", "company": "", "ticker": "", "cik": "",
        "title": "", "url": "", "published": "", "detected_at": "",
        "items": [], "keywords": [], "neg_keywords": [], "megacaps": [],
        "score": 0, "tier": "C", "reason": "",
        "market_cap": None, "cap_bucket": None,
        "price_at_detect": None, "price_now": None,
        "price_change_pct": None, "price_updated_at": None,
        "price_1h": None, "price_1d": None, "price_3d": None, "price_20d": None,
        # SPY at the same moments, so the dashboard can show market-relative
        # alpha ("did it beat the market?") instead of raw return.
        "spy_at_detect": None, "spy_1h": None, "spy_1d": None,
        "spy_3d": None, "spy_20d": None,
        "volume_ratio": None,
        "ai_summary": None, "ai_category": None, "ai_materiality": None,
        "ai_tickers": [], "social": False,
    }
    hit.update(fields)
    return hit


def scan_sec_8k(seen, ticker_map, doc_limit):
    """Scan EDGAR's live 8-K feed; return list of (hit, doc_text) tuples."""
    results = []
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
        item101 = any(n in HOT_ITEMS for n in item_nums)

        keywords, negatives, megacaps, text = [], [], [], ""
        if fetched < doc_limit:
            try:
                time.sleep(SEC_REQUEST_DELAY)
                doc_url = find_primary_doc(url)
                if doc_url:
                    time.sleep(SEC_REQUEST_DELAY)
                    text = html_to_text(http_get(doc_url))[:400_000]
                    keywords = match_keywords(text)
                    negatives = match_negative(text)
                    megacaps = match_megacaps(text)
                fetched += 1
            except Exception as exc:
                log("warning: deep fetch failed for %s (%s)" % (company, exc))

        if not item101 and not keywords:
            continue

        score = score_hit(keywords, megacaps, item101, negatives)
        reason = []
        if item101:
            reason.append("Item 1.01 (Material Definitive Agreement)")
        if keywords:
            reason.append("keywords: " + ", ".join(keywords[:5]))
        if negatives:
            reason.append("financing terms: " + ", ".join(negatives[:3]))
        hit = make_hit(
            id=entry_id,
            source="SEC 8-K",
            company=company,
            ticker=ticker,
            cik=cik,
            title="%s — %s" % (form, company),
            url=url,
            published=filed,
            detected_at=datetime.now(timezone.utc).isoformat(),
            items=["%s %s" % (n, t) for n, t in items],
            keywords=keywords,
            neg_keywords=negatives,
            megacaps=megacaps,
            score=score,
            tier=tier_for(score),
            reason="; ".join(reason),
        )
        results.append((hit, text))
    return results


SEC_FORM4_ATOM_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
    "&type=4&company=&dateb=&owner=include&count=100&output=atom"
)

# Post-earnings-announcement drift (PEAD): stocks with big positive earnings
# surprises tend to drift up for weeks — the market underreacts. Nasdaq's
# earnings calendar is free, keyless and gives actual EPS vs. forecast.
NASDAQ_EARNINGS_URL = "https://api.nasdaq.com/api/calendar/earnings?date={date}"
PEAD_MIN_SURPRISE = 10.0    # percent; below this the drift signal is weak
PEAD_A_SURPRISE = 25.0      # a blowout quarter
PEAD_MIN_FORECAST = 0.05    # ignore penny-EPS where % surprise is noise


def _money(s):
    """'$1,234.56' / '($0.12)' -> float, or None."""
    if not s or s in ("N/A", ""):
        return None
    neg = s.strip().startswith("(")
    try:
        v = float(re.sub(r"[^\d.]", "", s))
        return -v if neg else v
    except ValueError:
        return None


def parse_earnings_row(row):
    """A Nasdaq earnings row -> (surprise_pct, actual, forecast, market_cap) or None.

    Only rows that have actually reported (actual + forecast present) with a
    meaningful forecast magnitude qualify.
    """
    actual = _money(row.get("eps"))
    forecast = _money(row.get("epsForecast"))
    if actual is None or forecast is None or abs(forecast) < PEAD_MIN_FORECAST:
        return None
    try:
        surprise = float(row.get("surprise"))
    except (TypeError, ValueError):
        if forecast == 0:
            return None
        surprise = (actual - forecast) / abs(forecast) * 100
    return surprise, actual, forecast, _money(row.get("marketCap"))


def scan_earnings(seen, fetch_dates=2):
    """Scan Nasdaq's earnings calendar for big positive surprises (PEAD)."""
    results = []
    now = datetime.now(timezone.utc)
    for delta in range(fetch_dates):
        date = (now - timedelta(days=delta)).strftime("%Y-%m-%d")
        try:
            data = json.loads(http_get(NASDAQ_EARNINGS_URL.format(date=date), browser=True))
        except Exception as exc:
            log("warning: earnings fetch failed for %s (%s)" % (date, exc))
            continue
        rows = (data.get("data") or {}).get("rows") or []
        for row in rows:
            symbol = (row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            key = "earn:%s:%s" % (symbol, row.get("fiscalQuarterEnding", date))
            if key in seen:
                continue
            parsed = parse_earnings_row(row)
            if parsed is None:
                continue  # not reported yet or penny-EPS noise
            surprise, actual, forecast, mcap = parsed
            seen[key] = now.isoformat()
            if surprise < PEAD_MIN_SURPRISE:
                continue  # negative or small surprise: no drift edge
            score = 6 if surprise >= PEAD_A_SURPRISE else 4
            reason = ("Regnskabsoverraskelse: EPS $%.2f vs. forventet $%.2f (+%.1f%%)"
                      % (actual, forecast, surprise))
            hit = make_hit(
                id=key,
                source="Regnskab",
                company=(row.get("name") or symbol).strip(),
                ticker=symbol,
                title="Regnskab — %s" % (row.get("name") or symbol).strip(),
                url="https://www.nasdaq.com/market-activity/stocks/%s/earnings" % symbol.lower(),
                published=date,
                detected_at=now.isoformat(),
                score=score,
                tier=tier_for(score),
                reason=reason,
                market_cap=int(mcap) if mcap else None,
                cap_bucket=cap_bucket(int(mcap)) if mcap else None,
                ai_category="earnings",
            )
            results.append((hit, ""))
    log("Nasdaq earnings: %d surprise hit(s)" % len(results))
    return results


def parse_display_name(display):
    """'Acme Inc.  (ACME, ACMW)  (CIK 0001234567)' -> ('Acme Inc.', 'ACME')."""
    name = re.split(r"\s*\(", display, 1)[0].strip()
    m = re.search(r"\(([A-Z][A-Z.\-]*)(?:,[^)]*)?\)\s*\(CIK", display)
    return name, (m.group(1) if m else "")


def scan_themes(seen, ticker_map, now=None):
    """SEC full-text theme radar. Gated to run ~hourly (efts updates slowly).

    Flags companies filing about a curated emerging theme — an information
    edge (surfacing) rather than a hard catalyst, so these land as Tier B.
    """
    now = now or datetime.now(timezone.utc)
    if now.minute >= 5:
        return []  # cron fires every 5 min; only run at the top of the hour
    results = []
    startdt = (now - timedelta(days=3)).strftime("%Y-%m-%d")
    enddt = now.strftime("%Y-%m-%d")
    for phrase, label in THEMES:
        params = urllib.parse.urlencode({
            "q": '"%s"' % phrase, "forms": THEME_FORMS,
            "startdt": startdt, "enddt": enddt,
        })
        try:
            time.sleep(SEC_REQUEST_DELAY)
            data = json.loads(http_get(SEC_FTS_URL + "?" + params))
        except Exception as exc:
            log("warning: theme search failed for %s (%s)" % (label, exc))
            continue
        seen_this = 0
        for entry in data.get("hits", {}).get("hits", []):
            src = entry.get("_source", {})
            ciks = src.get("ciks") or [""]
            cik = ciks[0]
            key = "theme:%s:%s" % (phrase, cik)  # one hit per company per theme/week
            if key in seen:
                continue
            seen[key] = now.isoformat()
            if seen_this >= THEME_MAX_PER_THEME:
                continue
            seen_this += 1
            names = src.get("display_names") or [""]
            company, ticker = parse_display_name(names[0])
            form = (src.get("root_forms") or src.get("form") or [""])
            form = form[0] if isinstance(form, list) else form
            adsh = src.get("adsh", "")
            url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                   "&CIK=%s&type=&dateb=&owner=include&count=10" % cik.lstrip("0"))
            if adsh:
                folder = adsh.replace("-", "")
                url = ("https://www.sec.gov/Archives/edgar/data/%s/%s/%s-index.htm"
                       % (cik.lstrip("0"), folder, adsh))
            hit = make_hit(
                id=key,
                source="Tema",
                company=company,
                ticker=ticker or ticker_map.get(cik.lstrip("0"), ""),
                cik=cik,
                title="Tema — %s" % company,
                url=url,
                published=src.get("file_date", ""),
                detected_at=now.isoformat(),
                score=2,
                tier="B",
                reason="Tema: %s nævnt i %s-filing" % (label, form),
                ai_category="theme",
            )
            results.append((hit, ""))
    log("Tema-radar: %d filing(s) på tværs af %d temaer" % (len(results), len(THEMES)))
    return results


def parse_form4(xml_text):
    """Extract open-market insider buys from a Form 4 ownershipDocument.

    Returns {ticker, owner, role, total_usd, shares} summing transactions
    with code P (open-market purchase) + acquired flag A, or None.
    """
    try:
        root = ET.fromstring(xml_text)
        total_usd = total_shares = 0.0
        for tx in root.iter("nonDerivativeTransaction"):
            code = tx.findtext(".//transactionCode", "")
            acq = tx.findtext(".//transactionAcquiredDisposedCode/value", "")
            if code != "P" or acq != "A":
                continue
            shares = float(tx.findtext(".//transactionShares/value", "0") or 0)
            price = float(tx.findtext(".//transactionPricePerShare/value", "0") or 0)
            total_usd += shares * price
            total_shares += shares
        if total_usd <= 0:
            return None
        owner = root.findtext(".//reportingOwnerId/rptOwnerName", "").strip()
        rel = root.find(".//reportingOwnerRelationship")
        role = ""
        if rel is not None:
            title = (rel.findtext("officerTitle") or "").strip()
            if title:
                role = title
            elif (rel.findtext("isDirector") or "").strip() in ("1", "true"):
                role = "Director"
            elif (rel.findtext("isTenPercentOwner") or "").strip() in ("1", "true"):
                role = "10%-ejer"
        return {
            "ticker": (root.findtext(".//issuerTradingSymbol") or "").strip(),
            "owner": owner,
            "role": role,
            "total_usd": int(total_usd),
            "shares": int(total_shares),
        }
    except ET.ParseError:
        return None


def is_top_exec(role):
    """CEO/CFO/COO/President/Chair — the high-signal insider roles."""
    return bool(_TOP_EXEC_RE.search(role or ""))


def insider_score(usd, role, cluster_count):
    """Score an open-market insider buy. See tier_for for A/B cutoffs.

    Baseline 3 (a >=$100k buy is already a B). Top-exec conviction, large
    size, and a cluster of distinct buyers each push it toward A.
    """
    score = 3
    if is_top_exec(role):
        score += 2
    if usd >= 1_000_000:
        score += 2
    elif usd >= 250_000:
        score += 1
    if cluster_count >= 2:  # several insiders buying at once = strongest tell
        score += 3
    return score


def record_insider_buy(ledger, ticker, owner, usd, now):
    """Log a buy and return the count of distinct buyers in the cluster window.

    Ledger: {ticker: [[owner, iso_date, usd], ...]}, pruned by the caller.
    """
    if not ticker:
        return 1
    entries = ledger.setdefault(ticker, [])
    entries.append([owner, now.isoformat(), usd])
    cutoff = (now - timedelta(days=INSIDER_CLUSTER_DAYS)).isoformat()
    owners = {e[0] for e in entries if e[1] >= cutoff}
    return len(owners)


def prune_insider_ledger(ledger, now):
    cutoff = (now - timedelta(days=INSIDER_LEDGER_KEEP_DAYS)).isoformat()
    for ticker in list(ledger):
        kept = [e for e in ledger[ticker] if e[1] >= cutoff]
        if kept:
            ledger[ticker] = kept
        else:
            del ledger[ticker]


def find_form4_xml(index_url):
    """URL of the ownershipDocument XML in a Form 4 filing folder, or None."""
    folder = index_url.rsplit("/", 1)[0]
    listing = json.loads(http_get(folder + "/index.json"))
    for item in listing.get("directory", {}).get("item", []):
        name = item.get("name", "")
        if name.lower().endswith(".xml") and "index" not in name.lower():
            return folder + "/" + name
    return None


def scan_sec_form4(seen, ticker_map, ledger, fetch_limit=MAX_FORM4_FETCHES):
    """Scan EDGAR's live Form 4 feed for significant open-market insider buys.

    Weighs role (CEO/CFO > director) and cluster (several insiders buying the
    same name within 30 days) — both far more predictive than a lone buy.
    """
    results = []
    now = datetime.now(timezone.utc)
    feed = ET.fromstring(http_get(SEC_FORM4_ATOM_URL))
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entries = [e for e in feed.findall("a:entry", ns)
               if "(Issuer)" in e.findtext("a:title", "", ns)]
    log("SEC Form 4 feed: %d issuer entries" % len(entries))
    fetched = 0

    for entry in entries:
        entry_id = entry.findtext("a:id", "", ns) + ":f4"
        if entry_id in seen:
            continue
        seen[entry_id] = datetime.now(timezone.utc).isoformat()
        if fetched >= fetch_limit:
            continue  # marked seen; peak-hour overflow is skipped, not queued

        title = entry.findtext("a:title", "", ns)
        link_el = entry.find("a:link", ns)
        url = link_el.get("href") if link_el is not None else ""
        m = re.match(r"4(?:/A)? - (.+?) \((\d+)\)", title)
        company, cik = m.groups() if m else (title, "")

        try:
            time.sleep(SEC_REQUEST_DELAY)
            xml_url = find_form4_xml(url)
            if not xml_url:
                continue
            time.sleep(SEC_REQUEST_DELAY)
            buy = parse_form4(http_get(xml_url))
            fetched += 1
        except Exception as exc:
            log("warning: Form 4 fetch failed for %s (%s)" % (company, exc))
            continue
        if not buy or buy["total_usd"] < MIN_INSIDER_BUY:
            continue

        ticker = buy["ticker"] or ticker_map.get(cik.lstrip("0"), "")
        cluster = record_insider_buy(ledger, ticker, buy["owner"], buy["total_usd"], now)
        score = insider_score(buy["total_usd"], buy["role"], cluster)
        who = buy["owner"] + (" (%s)" % buy["role"] if buy["role"] else "")
        reason = "Insider-køb: %s købte for $%s i det åbne marked" % (
            who, "{:,.0f}".format(buy["total_usd"]).replace(",", "."))
        if cluster >= 2:
            reason += " · ⚡ klyngekøb: %d insidere seneste 30 dage" % cluster
        if is_top_exec(buy["role"]):
            reason += " · topledelse"
        hit = make_hit(
            id=entry_id,
            source="SEC Form 4",
            company=company,
            ticker=ticker,
            cik=cik,
            title="Form 4 — %s" % company,
            url=url,
            published=entry.findtext("a:updated", "", ns),
            detected_at=now.isoformat(),
            score=score,
            tier=tier_for(score),
            reason=reason,
        )
        results.append((hit, ""))
    return results


# ----------------------------------------------------------------- RSS

def scan_rss(source, feed_url, seen, keyword_override=None, max_age_hours=48):
    """Generic newswire RSS scanner; returns list of (hit, text) tuples."""
    results = []
    root = ET.fromstring(http_get(feed_url, browser=True).lstrip())
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

        if is_stale(item.findtext("pubDate"), max_age_hours):
            continue
        title = (item.findtext("title") or "").strip()
        desc = html_to_text(item.findtext("description") or "")
        text = title + " " + desc
        if keyword_override is not None:
            keywords = match_keyword_list(text, keyword_override)
        else:
            keywords = match_keywords(text)
        if not keywords:
            continue
        negatives = match_negative(text)
        megacaps = match_megacaps(text)
        score = score_hit(keywords, megacaps, False, negatives)
        reason = ["keywords: " + ", ".join(keywords[:5])]
        if negatives:
            reason.append("financing terms: " + ", ".join(negatives[:3]))
        hit = make_hit(
            id=key,
            source=source,
            title=title,
            url=(item.findtext("link") or "").strip(),
            published=(item.findtext("pubDate") or "").strip(),
            detected_at=datetime.now(timezone.utc).isoformat(),
            keywords=keywords,
            neg_keywords=negatives,
            megacaps=megacaps,
            score=score,
            tier=tier_for(score),
            reason="; ".join(reason),
        )
        results.append((hit, text))
    return results


def scan_social(person, platform, feed_url, seen):
    """Market-mover social feeds; returns list of (hit, text) tuples.

    Posts pass a cheap market-word prefilter here; run() sends survivors
    through the AI relevance gate (irrelevant posts are demoted to C, or —
    without an OpenAI key — only megacap/cashtag posts are kept at all).
    """
    results = []
    source = "%s · %s" % (person, platform)
    root = ET.fromstring(http_get(feed_url, browser=True).lstrip())
    channel_items = root.findall(".//item")
    log("%s: %d posts" % (source, len(channel_items)))

    for item in channel_items:
        guid = (item.findtext("guid") or item.findtext("link") or "").strip()
        if not guid:
            continue
        key = "social:" + guid
        if key in seen:
            continue
        seen[key] = datetime.now(timezone.utc).isoformat()

        if is_stale(item.findtext("pubDate")):
            continue
        title = html_to_text(item.findtext("title") or "")
        desc = html_to_text(item.findtext("description") or "")
        text = re.sub(r"\s+", " ", (title + " " + desc)).strip()
        text = re.sub(r"^\[No Title\][^|]*?Post from [^|]*?\d{4}", "", text).strip()
        if len(text) < 25:          # media-only/empty posts
            continue
        megacaps = match_megacaps(text)
        if not megacaps and not SOCIAL_MARKET_RE.search(text):
            continue
        hit = make_hit(
            id=key,
            source=source,
            title=text[:180],
            url=(item.findtext("link") or "").strip(),
            published=(item.findtext("pubDate") or "").strip(),
            detected_at=datetime.now(timezone.utc).isoformat(),
            megacaps=megacaps,
            score=3 if megacaps else 2,
            tier="B",
            reason="markedsrelevant post fra %s" % person,
            social=True,
        )
        results.append((hit, text))
    return results


# ---------------------------------------------------------------- alerts

def notify(hits):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    alertable = [h for h in hits if h["tier"] in ("A", "B")]
    for hit in alertable[:MAX_NOTIFICATIONS]:
        label = hit["ticker"] or hit["company"] or hit["source"]
        body_lines = ["[%s/%d] %s" % (hit["tier"], hit["score"], hit["title"])]
        if hit.get("ai_summary"):
            body_lines.append(hit["ai_summary"])
        body_lines.append(hit["reason"])
        if (hit.get("volume_ratio") or 0) >= 1.5:
            body_lines.append("⚡ Volumen %.1f× normalt — markedet reagerer" % hit["volume_ratio"])
        # A + high materiality = act fast: 'urgent' repeats vibration in the
        # ntfy app until seen. Tapping the notification opens the filing.
        if hit["tier"] == "A":
            priority = "urgent" if hit.get("ai_materiality") == "high" else "high"
        else:
            priority = "default"
        try:
            req = urllib.request.Request(
                "https://ntfy.sh/" + topic,
                data="\n".join(body_lines).encode(),
                headers={"Title": "Catalyst %s: %s" % (hit["tier"], label),
                         "Tags": "rotating_light" if priority == "urgent" else "chart_with_upwards_trend",
                         "Priority": priority,
                         "Click": hit["url"] or "https://catalyst-tape.netlify.app"},
            )
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as exc:
            log("warning: ntfy failed (%s)" % exc)


# -------------------------------------------------------------- watchdog

WATCHDOG_STALE_HOURS = 24


def update_health(health, source, error=None):
    """Record a source's scan outcome in the health map."""
    now = datetime.now(timezone.utc).isoformat()
    entry = health.setdefault(source, {})
    if error is None:
        entry["last_ok"] = now
        entry.pop("last_error", None)
        entry.pop("error", None)
    else:
        entry["last_error"] = now
        entry["error"] = str(error)[:200]


def check_watchdog(health):
    """ntfy an admin warning for sources that have been down for 24h+."""
    topic = os.environ.get("NTFY_TOPIC")
    now = datetime.now(timezone.utc)
    stale_cutoff = (now - timedelta(hours=WATCHDOG_STALE_HOURS)).isoformat()
    for source, entry in health.items():
        last_ok = entry.get("last_ok")
        if not last_ok or last_ok >= stale_cutoff:
            continue  # healthy, or never worked (config issue, not an outage)
        if entry.get("last_alerted", "") >= stale_cutoff:
            continue  # already alerted within the window
        entry["last_alerted"] = now.isoformat()
        log("WATCHDOG: %s has been down since %s" % (source, last_ok[:16]))
        if not topic:
            continue
        try:
            req = urllib.request.Request(
                "https://ntfy.sh/" + topic,
                data=("Kilden '%s' har fejlet i over %d timer.\nSeneste fejl: %s"
                      % (source, WATCHDOG_STALE_HOURS, entry.get("error", "?"))).encode(),
                headers={"Title": "Watchdog: %s er nede" % source, "Tags": "warning"},
            )
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as exc:
            log("warning: watchdog ntfy failed (%s)" % exc)


# ------------------------------------------------------------------ main

def run(doc_limit):
    _spy_cache.clear()  # one fresh SPY baseline per run
    seen = prune_seen(load_json(SEEN_FILE, {}))
    hits = load_json(HITS_FILE, [])
    known_ids = {h["id"] for h in hits}
    ticker_map = load_ticker_map()
    health = load_json(HEALTH_FILE, {})
    insider_ledger = load_json(INSIDER_FILE, {})

    results = []
    try:
        results += scan_sec_8k(seen, ticker_map, doc_limit)
        update_health(health, "SEC 8-K")
    except Exception as exc:
        log("error: SEC scan failed (%s)" % exc)
        update_health(health, "SEC 8-K", exc)
    try:
        results += scan_sec_form4(seen, ticker_map, insider_ledger)
        update_health(health, "SEC Form 4")
    except Exception as exc:
        log("warning: Form 4 scan failed (%s)" % exc)
        update_health(health, "SEC Form 4", exc)
    try:
        results += scan_earnings(seen)
        update_health(health, "Regnskab")
    except Exception as exc:
        log("warning: earnings scan failed (%s)" % exc)
        update_health(health, "Regnskab", exc)
    try:
        results += scan_themes(seen, ticker_map)
        update_health(health, "Tema")
    except Exception as exc:
        log("warning: theme scan failed (%s)" % exc)
        update_health(health, "Tema", exc)
    for source, feed_url, keyword_override, max_age in RSS_FEEDS:
        try:
            results += scan_rss(source, feed_url, seen, keyword_override, max_age)
            update_health(health, source)
        except Exception as exc:
            log("warning: %s scan failed (%s)" % (source, exc))
            update_health(health, source, exc)
    for person, platform, feed_url in SOCIAL_FEEDS:
        try:
            results += scan_social(person, platform, feed_url, seen)
            update_health(health, "%s · %s" % (person, platform))
        except Exception as exc:
            log("warning: %s/%s scan failed (%s)" % (person, platform, exc))
            update_health(health, "%s · %s" % (person, platform), exc)

    results = [(h, t) for h, t in results if h["id"] not in known_ids]

    ai_budget = MAX_AI_CALLS
    social_budget = MAX_SOCIAL_AI
    have_key = bool(os.environ.get("OPENAI_API_KEY"))
    for hit, text in results:
        enrich_hit(hit)
        if hit["source"] in ("SEC Form 4", "Regnskab", "Tema"):
            continue  # reason is already self-explanatory; save AI budget
        if hit.get("social"):
            if have_key and social_budget > 0:
                if ai_annotate(hit, text or hit["title"]):
                    social_budget -= 1
            elif not hit["megacaps"]:
                # no AI gate available: only megacap posts are trustworthy
                hit["tier"] = "C"
        elif hit["tier"] in ("A", "B") and ai_budget > 0:
            if ai_annotate(hit, text or hit["title"]):
                ai_budget -= 1
        if should_adopt_ai_ticker(hit):
            hit["ticker"] = hit["ai_tickers"][0]
            enrich_hit(hit)  # ticker-less hits were skipped the first time

    new_hits = [h for h, _ in results]
    if new_hits:
        hits = sorted(new_hits + hits, key=lambda h: h["detected_at"], reverse=True)
        hits = hits[:MAX_HITS_KEPT]
    check_watchdog(health)
    save_json(HEALTH_FILE, health)
    prune_insider_ledger(insider_ledger, datetime.now(timezone.utc))
    save_json(INSIDER_FILE, insider_ledger)

    # Spend leftover AI budget on recent A/B hits still missing annotation
    # (self-healing backfill: covers runs where the key was absent/exhausted).
    if ai_budget > 0 and os.environ.get("OPENAI_API_KEY"):
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=PRICE_REFRESH_HOURS)).isoformat()
        for hit in hits:
            if ai_budget <= 0:
                break
            if (hit["detected_at"] < cutoff or hit["tier"] not in ("A", "B")
                    or hit.get("ai_summary")):
                continue
            if ai_annotate(hit, refetch_text(hit) or hit["title"]):
                ai_budget -= 1
                log("AI backfill: %s" % hit["title"][:60])
                if should_adopt_ai_ticker(hit):
                    hit["ticker"] = hit["ai_tickers"][0]
                    enrich_hit(hit)
    refresh_prices(hits)
    save_json(HITS_FILE, hits)
    save_json(SEEN_FILE, seen)
    notify(new_hits)

    log("done: %d new hit(s), %d total" % (len(new_hits), len(hits)))
    for hit in new_hits:
        log("  HIT %s/%d [%s] %s — %s" % (hit["tier"], hit["score"],
                                          hit["source"], hit["title"], hit["reason"]))
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

SELFTEST_CREDIT_DOC = """
<p>Item 1.01. Entry into a Material Definitive Agreement. On July 1, 2026
the Company entered into an amended and restated credit agreement providing
for a revolving credit facility and a term loan.</p>
"""

SELFTEST_FORM4 = """<?xml version="1.0"?>
<ownershipDocument>
  <issuer><issuerCik>0001509745</issuerCik><issuerName>CYPHERPUNK</issuerName>
    <issuerTradingSymbol>CYPH</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Doe Jane</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>1</isDirector><isOfficer>1</isOfficer>
      <officerTitle>Chief Executive Officer</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>50000</value></transactionShares>
        <transactionPricePerShare><value>2.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>25000</value></transactionShares>
        <transactionPricePerShare><value>2.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>F</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>99999</value></transactionShares>
        <transactionPricePerShare><value>2.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""

SELFTEST_AI_RESPONSE = json.dumps({
    "choices": [{"message": {"content": json.dumps({
        "summary": "3-årig leveringsaftale med Walmart om private label-produkter.",
        "category": "partnership",
        "materiality": "high",
        "tickers": ["wmt", "COST"],
    })}}]
})


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

    # scoring: partnership + megacap + contract award + Item 1.01 = A tier
    score = score_hit(kw, caps, True, [])
    check("partnership+megacap scores A", tier_for(score) == "A")

    # scoring: credit facility 8-K = C tier despite Item 1.01
    credit_text = html_to_text(SELFTEST_CREDIT_DOC)
    neg = match_negative(credit_text)
    check("negative: credit terms found", "credit agreement" in neg and "term loan" in neg)
    credit_score = score_hit(match_keywords(credit_text), [], True, neg)
    check("credit 8-K scores C", tier_for(credit_score) == "C")

    # scoring: plain partnership without megacap = B tier
    check("plain partnership scores B",
          tier_for(score_hit(["partnership agreement"], [], True, [])) == "B")

    check("cap buckets", (cap_bucket(500_000_000), cap_bucket(5_000_000_000),
                          cap_bucket(50_000_000_000)) == ("small", "mid", "large"))

    check("spy_field mapping", (spy_field("price_1d"), spy_field("price_at_detect"))
          == ("spy_1d", "spy_at_detect"))

    # PEAD earnings parsing
    er = parse_earnings_row({"eps": "$0.28", "epsForecast": "$0.24", "surprise": "16.67",
                             "marketCap": "$9,372,610,637"})
    check("earnings: surprise parsed", er is not None and round(er[0], 1) == 16.7
          and er[3] == 9_372_610_637)
    check("earnings: not-yet-reported skipped",
          parse_earnings_row({"eps": "", "epsForecast": "$0.24", "surprise": "N/A"}) is None)
    check("earnings: penny-EPS noise skipped",
          parse_earnings_row({"eps": "$0.02", "epsForecast": "$0.01", "surprise": "100"}) is None)
    check("earnings: computes surprise when field missing",
          round(parse_earnings_row({"eps": "$1.10", "epsForecast": "$1.00"})[0], 0) == 10)
    check("money parser", (_money("$1,234.56"), _money("($0.12)"), _money("N/A"))
          == (1234.56, -0.12, None))
    check("20d snapshot in model", "price_20d" in make_hit() and "spy_20d" in make_hit())

    # theme radar: display-name parsing + hourly gate
    check("display name parse", parse_display_name(
        "Acme Robotics Inc.  (ACME, ACMW)  (CIK 0001234567)") == ("Acme Robotics Inc.", "ACME"))
    check("display name no ticker", parse_display_name(
        "Private Fund LLC  (CIK 0009999999)") == ("Private Fund LLC", ""))
    off = datetime.now(timezone.utc).replace(minute=20)
    check("theme gate: skips off-hour (offline)", scan_themes({}, {}, now=off) == [])

    # insider #2: role weighting, size, and cluster detection
    check("insider: top exec detection", is_top_exec("Chief Financial Officer")
          and is_top_exec("President") and not is_top_exec("Director"))
    check("insider: director $150k = B", tier_for(insider_score(150_000, "Director", 1)) == "B")
    check("insider: CEO $300k = A", tier_for(insider_score(300_000, "CEO", 1)) == "A")
    check("insider: cluster lifts director to A", tier_for(insider_score(150_000, "Director", 3)) == "A")
    led = {}
    n0 = datetime.now(timezone.utc)
    record_insider_buy(led, "ACME", "Alice", 200_000, n0)
    c2 = record_insider_buy(led, "ACME", "Bob", 300_000, n0)
    same = record_insider_buy(led, "ACME", "Alice", 100_000, n0)
    check("insider: cluster counts distinct owners", c2 == 2 and same == 2)
    old = {"ACME": [["Zed", (n0 - timedelta(days=50)).isoformat(), 1]]}
    prune_insider_ledger(old, n0)
    check("insider: ledger prunes stale", "ACME" not in old)
    check("make_hit has spy fields", all(k in make_hit()
          for k in ("spy_at_detect", "spy_1h", "spy_1d", "spy_3d")))

    fields = parse_ai_response(SELFTEST_AI_RESPONSE)
    check("AI response parsing", fields is not None
          and fields["ai_category"] == "partnership"
          and fields["ai_materiality"] == "high")
    check("AI response extracts tickers", fields["ai_tickers"] == ["WMT", "COST"])
    check("AI response parsing rejects garbage", parse_ai_response("not json") is None)

    # AI-ticker adoption: only ticker-less, fresh hits
    fresh_iso = datetime.now(timezone.utc).isoformat()
    stale_iso = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    check("ticker adoption: fresh ticker-less hit", should_adopt_ai_ticker(
        {"ticker": "", "ai_tickers": ["CTSH"], "detected_at": fresh_iso}))
    check("ticker adoption: skip when SEC ticker exists", not should_adopt_ai_ticker(
        {"ticker": "SRFM", "ai_tickers": ["CTSH"], "detected_at": fresh_iso}))
    check("ticker adoption: skip stale baseline", not should_adopt_ai_ticker(
        {"ticker": "", "ai_tickers": ["CTSH"], "detected_at": stale_iso}))
    check("ticker adoption: skip without AI tickers", not should_adopt_ai_ticker(
        {"ticker": "", "ai_tickers": [], "detected_at": fresh_iso}))

    # AI veto: financing classification demotes B to C
    veto_hit = {"tier": "B", "reason": "x", "ai_category": "financing", "ai_materiality": "low"}
    adjust_tier_after_ai(veto_hit)
    check("AI financing veto demotes to C", veto_hit["tier"] == "C")
    boost_hit = {"tier": "B", "reason": "x", "ai_category": "partnership", "ai_materiality": "high"}
    adjust_tier_after_ai(boost_hit)
    check("AI high-materiality partnership boosts to A", boost_hit["tier"] == "A")

    # social prefilter: market words / cashtags pass, chit-chat doesn't
    check("social prefilter: tariffs", bool(SOCIAL_MARKET_RE.search("Big TARIFFS on foreign cars!")))
    check("social prefilter: cashtag", bool(SOCIAL_MARKET_RE.search("Buying more $TSLA today")))
    check("social prefilter: chit-chat rejected",
          not SOCIAL_MARKET_RE.search("Happy birthday to a great American!"))

    social_raw = json.dumps({"choices": [{"message": {"content": json.dumps({
        "relevant": True, "summary": "Toldsatser på biler rammer europæiske producenter.",
        "category": "policy", "materiality": "medium", "tickers": ["gm", "F"],
    })}}]})
    sf = parse_social_response(social_raw)
    check("social AI parsing", sf is not None and sf["relevant"]
          and sf["ai_category"] == "policy" and sf["ai_tickers"] == ["GM", "F"])

    check("per-feed keywords", match_keyword_list(
        "FDA approves new drug from Pfizer", ["approves", "recall"]) == ["approves"])

    check("is_stale: old item", is_stale("Mon, 01 Jan 2024 12:00:00 +0000"))
    check("is_stale: fresh item", not is_stale(
        datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")))
    check("is_stale: garbage tolerated", not is_stale("not a date") and not is_stale(None))

    # Form 4: open-market buy summed, sales/awards ignored
    buy = parse_form4(SELFTEST_FORM4)
    check("form4: buy parsed", buy is not None and buy["ticker"] == "CYPH"
          and buy["total_usd"] == 150_000 and buy["owner"] == "Doe Jane")
    check("form4: role from officerTitle", buy["role"] == "Chief Executive Officer")
    check("form4: sale-only returns None",
          parse_form4(SELFTEST_FORM4.replace(">P<", ">S<")) is None)
    check("form4: garbage tolerated", parse_form4("<not-xml") is None)

    # snapshots: due fields depend on age
    now = datetime.now(timezone.utc)
    young = {"detected_at": (now - timedelta(hours=2)).isoformat(), "price_1h": None,
             "price_1d": None, "price_3d": None}
    check("snapshots: 1h due at 2h", due_snapshots(young, now) == ["price_1h"])
    old_hit = {"detected_at": (now - timedelta(hours=75)).isoformat(), "price_1h": 1.0,
               "price_1d": 1.0, "price_3d": None}
    check("snapshots: 3d due at 75h", due_snapshots(old_hit, now) == ["price_3d"])
    late = {"detected_at": (now - timedelta(hours=10)).isoformat(), "price_1h": None,
            "price_1d": None, "price_3d": None}
    check("snapshots: 1h window closed at 10h", due_snapshots(late, now) == [])
    check("snapshots: 20d due at 22d", due_snapshots(
        {"detected_at": (now - timedelta(days=22)).isoformat()}, now) == ["price_20d"])
    ancient = {"detected_at": (now - timedelta(days=45)).isoformat()}
    check("snapshots: too late is skipped", due_snapshots(ancient, now) == [])

    # watchdog: stale source flagged once, healthy source untouched
    health = {"FDA": {"last_ok": (now - timedelta(hours=30)).isoformat(), "error": "503"},
              "SEC 8-K": {"last_ok": now.isoformat()}}
    check_watchdog(health)
    check("watchdog: stale source alerted", "last_alerted" in health["FDA"])
    check("watchdog: healthy source untouched", "last_alerted" not in health["SEC 8-K"])
    alerted_at = health["FDA"]["last_alerted"]
    check_watchdog(health)
    check("watchdog: no re-alert within window", health["FDA"]["last_alerted"] == alerted_at)

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
