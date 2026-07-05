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
PRICE_REFRESH_HOURS = 48   # keep updating price_now for hits this fresh
MAX_PRICE_REFRESH = 25     # Yahoo lookups per run for the refresh pass

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

def fetch_price(ticker):
    """Latest price from Yahoo's chart endpoint, or None."""
    try:
        url = YAHOO_CHART_URL.format(ticker=urllib.parse.quote(ticker)) + "?range=1d&interval=1d"
        data = json.loads(http_get(url, timeout=15, browser=True))
        meta = data["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        return float(price) if price else None
    except Exception:
        return None


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
    """Attach market cap and detection price. Fail-soft on every field."""
    if not hit["ticker"]:
        return
    price = fetch_price(hit["ticker"])
    hit["price_at_detect"] = price
    hit["price_now"] = price
    hit["price_change_pct"] = 0.0 if price else None
    hit["price_updated_at"] = datetime.now(timezone.utc).isoformat()
    if price and hit.get("cik"):
        shares = fetch_shares(hit["cik"].lstrip("0") or "0")
        if shares:
            hit["market_cap"] = int(price * shares)
            hit["cap_bucket"] = cap_bucket(hit["market_cap"])


def refresh_prices(hits):
    """Update price_now/price_change_pct on recent hits (the 'am I late?' number)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=PRICE_REFRESH_HOURS)).isoformat()
    budget = MAX_PRICE_REFRESH
    updated = 0
    for hit in hits:
        if budget <= 0:
            break
        if hit["detected_at"] < cutoff or not hit["ticker"] or not hit.get("price_at_detect"):
            continue
        price = fetch_price(hit["ticker"])
        budget -= 1
        if price:
            hit["price_now"] = price
            hit["price_change_pct"] = round(
                (price - hit["price_at_detect"]) / hit["price_at_detect"] * 100, 2)
            hit["price_updated_at"] = datetime.now(timezone.utc).isoformat()
            updated += 1
        time.sleep(0.2)
    if updated:
        log("price refresh: %d hit(s) updated" % updated)


# ------------------------------------------------------------ AI annotate

AI_SYSTEM_PROMPT = (
    "Du er aktieanalytiker. Du får teksten fra en SEC 8-K-filing eller "
    "pressemeddelelse. Svar KUN med JSON: {\"summary\": \"én kort sætning på "
    "dansk om hvad aftalen konkret er og med hvem\", \"category\": "
    "\"partnership|contract_award|financing|dilution|merger|other\", "
    "\"materiality\": \"high|medium|low\"}. materiality = hvor væsentlig "
    "aftalen virker for selskabets omsætning. Udvandende finansiering "
    "(securities purchase, ATM, warrants) er dilution."
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
        return {"ai_summary": summary[:300], "ai_category": cat, "ai_materiality": mat}
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
        tickers = obj.get("tickers") or []
        fields["ai_tickers"] = [str(t).upper()[:6] for t in tickers if t][:8]
        return fields
    except (ValueError, KeyError, IndexError, TypeError):
        return None


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


# ------------------------------------------------------------------ main

def run(doc_limit):
    seen = prune_seen(load_json(SEEN_FILE, {}))
    hits = load_json(HITS_FILE, [])
    known_ids = {h["id"] for h in hits}
    ticker_map = load_ticker_map()

    results = []
    try:
        results += scan_sec_8k(seen, ticker_map, doc_limit)
    except Exception as exc:
        log("error: SEC scan failed (%s)" % exc)
    for source, feed_url, keyword_override, max_age in RSS_FEEDS:
        try:
            results += scan_rss(source, feed_url, seen, keyword_override, max_age)
        except Exception as exc:
            log("warning: %s scan failed (%s)" % (source, exc))
    for person, platform, feed_url in SOCIAL_FEEDS:
        try:
            results += scan_social(person, platform, feed_url, seen)
        except Exception as exc:
            log("warning: %s/%s scan failed (%s)" % (person, platform, exc))

    results = [(h, t) for h, t in results if h["id"] not in known_ids]

    ai_budget = MAX_AI_CALLS
    social_budget = MAX_SOCIAL_AI
    have_key = bool(os.environ.get("OPENAI_API_KEY"))
    for hit, text in results:
        enrich_hit(hit)
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

    new_hits = [h for h, _ in results]
    if new_hits:
        hits = sorted(new_hits + hits, key=lambda h: h["detected_at"], reverse=True)
        hits = hits[:MAX_HITS_KEPT]

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

SELFTEST_AI_RESPONSE = json.dumps({
    "choices": [{"message": {"content": json.dumps({
        "summary": "3-årig leveringsaftale med Walmart om private label-produkter.",
        "category": "partnership",
        "materiality": "high",
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

    fields = parse_ai_response(SELFTEST_AI_RESPONSE)
    check("AI response parsing", fields is not None
          and fields["ai_category"] == "partnership"
          and fields["ai_materiality"] == "high")
    check("AI response parsing rejects garbage", parse_ai_response("not json") is None)

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
