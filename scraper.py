"""
LeetCode Interview Experience Scraper
Hosted on Railway | Triggered by Make.com every 10 hours
Two endpoints: /list (metadata) and /scrape-content (full text)
"""

import os
import json
import time
import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from flask import Flask, jsonify, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

LEETCODE_URL_1   = "https://leetcode.com/discuss/topic/interview-experience/"
LEETCODE_URL_2   = "https://leetcode.com/discuss/topic/interview/"
PROCESSED_FILE   = "/data/processed_posts.json"
MAX_POSTS_PER_URL = 6   # max 6 from URL1, max 8 from URL2
MAX_POSTS_COMBINED = 12  # final combined max
REPROCESS_HOURS  = 10
SCRAPE_DELAY     = 1

app = Flask(__name__)


# ── Persistent State ──────────────────────────────────────────────────────────

def load_processed() -> dict:
    os.makedirs(os.path.dirname(PROCESSED_FILE), exist_ok=True)
    if not os.path.exists(PROCESSED_FILE):
        return {}
    try:
        with open(PROCESSED_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_processed(data: dict) -> None:
    os.makedirs(os.path.dirname(PROCESSED_FILE), exist_ok=True)
    with open(PROCESSED_FILE, "w") as f:
        json.dump(data, f, indent=2)


def post_hash(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()


def already_processed(url: str, processed: dict) -> bool:
    h = post_hash(url)
    if h not in processed:
        return False
    scraped_at = processed[h].get("scraped_at", "")
    if not scraped_at:
        return True
    dt = datetime.fromisoformat(scraped_at)
    age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    return age_hours < REPROCESS_HOURS


def mark_processed(url: str, processed: dict) -> None:
    processed[post_hash(url)] = {
        "url": url,
        "scraped_at": datetime.now(timezone.utc).isoformat()
    }


# ── Selenium Driver ───────────────────────────────────────────────────────────

def build_driver(cookies: Optional[list] = None) -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--window-size=1280,720")
    opts.add_argument("--blink-settings=imagesEnabled=false")
    opts.add_argument("--disable-extensions")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-software-rasterizer")
    opts.add_argument("--no-first-run")
    opts.add_argument("--disable-default-apps")
    opts.add_argument("--disable-background-networking")
    opts.page_load_strategy = "eager"  # dont wait for full page load
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    opts.set_capability("pageLoadStrategy", "eager")
    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(25)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"}
    )

    if cookies:
        driver.get("https://leetcode.com")
        time.sleep(0.5)
        for ck in cookies:
            try:
                driver.add_cookie(ck)
            except Exception as e:
                log.warning(f"Cookie inject failed: {e}")
        log.info(f"Injected {len(cookies)} cookies")

    return driver


def load_cookies_from_env() -> Optional[list]:
    raw = os.environ.get("LEETCODE_COOKIES", "")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception as e:
        log.error(f"Failed to parse LEETCODE_COOKIES: {e}")
        return None


# ── Scraping Logic ────────────────────────────────────────────────────────────

def scrape_post_detail(driver: webdriver.Chrome, url: str) -> Optional[str]:
    """
    Scrape post content using the real LeetCode selector:
    div.break-words > div > div p
    Collects all paragraph text, joins, limits to 150 words
    to avoid 413/499 payload errors in Make.com.
    """
    import re as _re
    try:
        driver.get(url)

        # Wait for page content
        for sel in ["div.break-words", "div[class*='break-words']", "h1", "body"]:
            try:
                WebDriverWait(driver, 12).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, sel))
                )
                log.info(f"Post page loaded: {sel}")
                break
            except TimeoutException:
                continue

        time.sleep(1.5)
        soup = BeautifulSoup(driver.page_source, "html.parser")

        # Remove noise
        for tag in soup.select("nav, footer, header, script, style, aside"):
            tag.decompose()

        paragraphs = []

        # PRIMARY: exact selector from real LeetCode post page
        primary = soup.select("div.break-words > div > div p")
        if primary:
            log.info(f"Primary selector matched: {len(primary)} paragraphs")
            paragraphs = [p.get_text(strip=True) for p in primary if p.get_text(strip=True)]

        # FALLBACK 1: any p inside break-words
        if not paragraphs:
            log.warning("Primary failed — trying break-words p fallback")
            els = soup.select("div.break-words p")
            paragraphs = [p.get_text(strip=True) for p in els if p.get_text(strip=True)]

        # FALLBACK 2: any paragraph on page with real text
        if not paragraphs:
            log.warning("Trying all page paragraphs")
            els = soup.find_all("p")
            paragraphs = [p.get_text(strip=True) for p in els
                         if len(p.get_text(strip=True)) > 20]

        # FALLBACK 3: body text
        if not paragraphs:
            log.warning("Using body text fallback")
            body = driver.find_element(By.TAG_NAME, "body").text
            paragraphs = [body[:3000]]

        # Join all paragraphs
        full_text = " ".join(paragraphs)
        full_text = _re.sub(r"\s+", " ", full_text).strip()

        # Limit to 6000 chars — safe for OpenRouter free model context
        # (full post kept, only hard-truncated if extremely long)
        if len(full_text) > 6000:
            full_text = full_text[:6000].strip() + "..."
            log.info(f"Truncated to 6000 chars")
        else:
            log.info(f"Full content: {len(full_text)} chars")

        return full_text if full_text else None

    except Exception as e:
        log.error(f"Detail scrape failed for {url}: {e}")
        try:
            body = driver.find_element(By.TAG_NAME, "body").text
            return body[:6000].strip() if body else None
        except Exception:
            pass
        return None


def is_today_strict(timestamp: str) -> bool:
    """
    Accept ONLY these exact patterns:
      - 1..59 minutes ago
      - 1..23 hours ago
      - just now / X seconds ago
    Reject everything else: days, weeks, months, absolute dates.
    """
    import re
    t = timestamp.strip().lower()

    if not t:
        return False

    # Reject absolute dates e.g. "Mar 18, 2026"
    if re.search(r"[a-z]{3}\s+\d{1,2},?\s+\d{4}", t):
        return False

    # Reject days / weeks / months / years / yesterday
    if "yesterday" in t:
        return False
    if "week" in t or "month" in t or "year" in t:
        return False
    day_m = re.search(r"(\d+)\s+day", t)
    if day_m:
        return False

    # Accept: just now
    if "just now" in t:
        return True

    # Accept: "a minute ago" / "a second ago" / "an hour ago"
    if re.match(r"^a\s+minute", t):
        return True
    if re.match(r"^a\s+second", t):
        return True
    if re.match(r"^an?\s+hour", t):
        return True

    # Accept: 1–59 seconds ago
    sec_m = re.search(r"(\d+)\s+second", t)
    if sec_m:
        return True

    # Accept: 1–59 minutes ago
    min_m = re.search(r"(\d+)\s+minute", t)
    if min_m:
        n = int(min_m.group(1))
        if 1 <= n <= 59:
            return True
        return False

    # Accept: 1–23 hours ago
    hr_m = re.search(r"(\d+)\s+hour", t)
    if hr_m:
        n = int(hr_m.group(1))
        if 1 <= n <= 23:
            return True
        return False

    return False


def timestamp_to_sort_key(timestamp: str) -> int:
    """
    Convert LeetCode timestamp to an integer for sorting (higher = more recent).
    Handles:
      - Relative: '27 minutes ago', '5 hours ago', 'just now'
      - Absolute: 'Mar 18, 2026', 'Mar 20, 2026'
    """
    import re
    from datetime import datetime as dt2, timedelta
    t = timestamp.strip().lower()
    now = datetime.now(timezone.utc)

    if not t:
        return 0

    # Relative: minutes
    m = re.search(r"(\d+)\s+minute", t)
    if m:
        return int((now - timedelta(minutes=int(m.group(1)))).timestamp())

    # Relative: hours
    m = re.search(r"(\d+)\s+hour", t)
    if m:
        return int((now - timedelta(hours=int(m.group(1)))).timestamp())

    # Relative: days
    m = re.search(r"(\d+)\s+day", t)
    if m:
        return int((now - timedelta(days=int(m.group(1)))).timestamp())

    # just now / seconds
    if "just now" in t or "second" in t:
        return int(now.timestamp())

    # yesterday
    if "yesterday" in t:
        return int((now - timedelta(days=1)).timestamp())

    # Absolute: 'Mar 18, 2026' or 'Mar 18 2026'
    m = re.search(r"([a-z]{3})\s+(\d{1,2}),?\s+(\d{4})", t)
    if m:
        try:
            d = dt2.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y")
            return int(d.timestamp())
        except Exception:
            pass

    return 0


def scrape_listing(driver: webdriver.Chrome, url: str, max_posts: int = 6) -> list:
    driver.get(url)

    # Wait for ANY post card to appear — try multiple selectors
    waited = False
    for wait_sel in [
        "div.flex.flex-col.gap-4",          # outer feed container
        "div[class*='topic-item']",          # topic card
        "a[href*='/discuss/']",              # any discuss link
        "div.overflow-hidden",               # generic card
    ]:
        try:
            WebDriverWait(driver, 8).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, wait_sel))
            )
            log.info(f"Page loaded — wait selector matched: {wait_sel}")
            waited = True
            break
        except TimeoutException:
            continue

    if not waited:
        log.error("Timed out — no post cards found after all wait selectors")
        # Last resort: dump page source snippet for debugging
        log.info("PAGE TITLE: " + driver.title)
        log.info("PAGE SNIPPET: " + driver.page_source[:2000])
        return []

    # Scroll to trigger lazy-loaded posts
    for _ in range(3):
        driver.execute_script("window.scrollBy(0, 400);")
        time.sleep(0.5)

    soup = BeautifulSoup(driver.page_source, "html.parser")

    # ── Strategy: find all <a> tags that link to /discuss/ posts ────────────
    # LeetCode renders post cards as <a href="/discuss/..."> wrappers
    # We try a cascade of selectors from most-specific to broadest

    containers = []

    # Selector 1: Tailwind class combo seen in LeetCode 2025-2026 UI
    containers = soup.select("a[href*='/discuss/'][class*='no-underline']")

    # Selector 2: any <a> linking to a discuss topic (numeric ID pattern)
    if not containers:
        log.warning("Selector 1 empty, trying selector 2")
        import re
        containers = [
            a for a in soup.find_all("a", href=True)
            if re.search(r"/discuss/\d+/", a.get("href", ""))
        ]

    # Selector 3: broadest fallback — any discuss link with meaningful text
    if not containers:
        log.warning("Selector 2 empty, trying selector 3")
        containers = [
            a for a in soup.find_all("a", href=True)
            if "/discuss/" in a.get("href", "") and len(a.get_text(strip=True)) > 10
        ]

    log.info(f"Raw containers found: {len(containers)}")

    posts = []
    seen_urls = set()

    for el in containers[: max_posts * 5]:
        if len(posts) >= max_posts:
            break

        href = el.get("href", "")
        url  = f"https://leetcode.com{href}" if href.startswith("/") else href

        # Skip duplicates and non-post links
        if not url or url in seen_urls:
            continue
        if "/discuss/topic/" in url or url == LEETCODE_URL:
            continue
        seen_urls.add(url)

        # ── Extract title ────────────────────────────────────────────────
        # Try specific selectors first, then fall back to largest text node
        title = ""

        # Specific Tailwind classes used in LeetCode discuss cards
        for title_sel in [
            "div.text-sd-foreground.line-clamp-1",
            "div[class*='line-clamp-1']",
            "p[class*='line-clamp-1']",
            "span[class*='line-clamp-1']",
        ]:
            t = el.select_one(title_sel)
            if t:
                title = t.get_text(strip=True)
                break

        # Fallback: find the longest direct text block inside the <a>
        if not title:
            candidates = [
                tag.get_text(strip=True)
                for tag in el.find_all(["div", "p", "span", "h3"])
                if len(tag.get_text(strip=True)) > 10
            ]
            title = max(candidates, key=len) if candidates else el.get_text(strip=True)[:120]

        if not title:
            continue

        log.info(f"Post found: {title!r}")

        # ── Title filter: interview, experience, or SDE ─────────────────
        t_lower = title.lower()
        if not any(kw in t_lower for kw in ["interview", "experience", "sde"]):
            log.info(f"Skipping — no keyword match: {title!r}")
            continue

        # ── Extract description ──────────────────────────────────────────
        description = ""
        for desc_sel in [
            "div.text-sd-muted-foreground.line-clamp-2",
            "div[class*='line-clamp-2']",
            "p[class*='line-clamp-2']",
        ]:
            d = el.select_one(desc_sel)
            if d:
                description = d.get_text(strip=True)
                break

        # ── Extract timestamp ────────────────────────────────────────────
        timestamp = ""
        for ts_sel in [
            "span[data-state='closed']",
            "span[class*='text-sd-muted']",
            "span[class*='time']",
            "time",
        ]:
            t = el.select_one(ts_sel)
            if t:
                # prefer datetime attr on <time> tags
                timestamp = t.get("datetime", "") or t.get_text(strip=True)
                break

        # Also check relative time text patterns anywhere inside the card
        if not timestamp:
            import re
            full_text = el.get_text(" ", strip=True)
            m = re.search(r"(\d+\s+(?:minute|hour|day|week|month)s?\s+ago|just now|yesterday)", full_text, re.I)
            if m:
                timestamp = m.group(1)

        log.info(f"Timestamp: {timestamp!r}")

        # ── Only accept TODAY's posts ─────────────────────────────────────
        # "Today" = relative timestamps only: minutes/hours/seconds/just now
        # Absolute dates like "Mar 18, 2026" = old post → skip
        if not is_today_strict(timestamp):
            log.info(f"Skipping — not today ({timestamp!r}): {title!r}")
            continue

        posts.append({
            "url":         url,
            "title":       title,
            "description": description,
            "timestamp":   timestamp,
            "sort_key":    timestamp_to_sort_key(timestamp),
        })
        time.sleep(SCRAPE_DELAY)

    # Sort newest first, strip sort_key
    posts.sort(key=lambda p: p["sort_key"], reverse=True)
    for p in posts:
        p.pop("sort_key", None)

    log.info(f"Returning {len(posts)} TODAY's interview posts (newest first)")
    for p in posts:
        log.info(f"  [{p['timestamp']}] {p['title']!r}")
    return posts



# ── /list endpoint — returns title, timestamp, URL, unique ID ─────────────────

def run_list_cycle() -> dict:
    """
    Scrape both listing pages.
    URL1 (interview-experience): max 6 posts
    URL2 (interview): max 8 posts
    Combined: max 12 posts, sorted newest first, deduped by URL.
    """
    cookies = load_cookies_from_env()
    driver  = None
    posts   = []

    try:
        driver = build_driver(cookies)

        # ── URL 1: interview-experience — max 6 ──────────────────────────
        log.info(f"Scraping URL1: {LEETCODE_URL_1}")
        raw1 = scrape_listing(driver, LEETCODE_URL_1, max_posts=6)
        log.info(f"URL1 returned {len(raw1)} posts")

        # ── URL 2: interview — max 8 ──────────────────────────────────────
        log.info(f"Scraping URL2: {LEETCODE_URL_2}")
        raw2 = scrape_listing(driver, LEETCODE_URL_2, max_posts=8)
        log.info(f"URL2 returned {len(raw2)} posts")

        # ── Combine, dedupe by URL, sort newest first, cap at 12 ─────────
        seen_urls = set()
        combined  = []
        for post in raw1 + raw2:
            if post["url"] not in seen_urls:
                seen_urls.add(post["url"])
                combined.append(post)

        # Sort by timestamp (newest first) using sort_key if present
        combined.sort(key=lambda p: timestamp_to_sort_key(p.get("timestamp", "")), reverse=True)
        combined = combined[:MAX_POSTS_COMBINED]

        for post in combined:
            posts.append({
                "post_id":   post_hash(post["url"]),
                "title":     post["title"],
                "timestamp": post["timestamp"],
                "post_url":  post["url"],
            })

        log.info(f"List cycle done — {len(posts)} combined posts (max {MAX_POSTS_COMBINED})")

    except Exception as e:
        log.exception(f"List cycle crashed: {e}")
        return {"status": "error", "message": str(e), "posts": []}
    finally:
        if driver:
            driver.quit()

    return {"status": "success", "count": len(posts), "posts": posts}


# ── /scrape-content endpoint — scrapes full text of ONE post URL ──────────────

def run_content_scrape(post_url: str) -> dict:
    """
    Given a single post URL, scrape its full text content.
    Called by Make.com only for posts not yet in its datastore.
    Returns raw post text — OpenRouter AI extraction happens in Make.com.
    """
    cookies = load_cookies_from_env()
    driver  = None

    try:
        driver    = build_driver(cookies)
        post_text = scrape_post_detail(driver, post_url)

        if post_text is None:
            return {"status": "error", "message": "Could not scrape post content", "content": ""}

        log.info(f"Content scraped ({len(post_text)} chars): {post_url}")
        return {
            "status":   "success",
            "post_url": post_url,
            "content":  post_text,
        }

    except Exception as e:
        log.exception(f"Content scrape crashed: {e}")
        return {"status": "error", "message": str(e), "content": ""}
    finally:
        if driver:
            driver.quit()


# ── Flask Endpoints ───────────────────────────────────────────────────────────

def auth_check() -> bool:
    api_key  = request.headers.get("X-API-Key", "")
    expected = os.environ.get("SCRAPER_API_KEY", "")
    return not expected or api_key == expected


@app.route("/list", methods=["GET", "POST"])
def list_endpoint():
    """
    Make.com calls this every 10 hours.
    Returns: { posts: [ {post_id, title, timestamp, post_url}, ... ] }
    Make.com then checks each post_id against its own datastore.
    """
    if not auth_check():
        return jsonify({"error": "Unauthorized"}), 401
    result = run_list_cycle()
    return jsonify(result), 200 if result["status"] == "success" else 500


@app.route("/scrape-content", methods=["POST"])
def content_endpoint():
    """
    Make.com calls this for each NEW post (not yet in its datastore).
    Body: { "post_url": "https://leetcode.com/discuss/..." }
    Returns: { content: "full post text..." }
    Hard 35s timeout prevents Railway 499 errors.
    """
    if not auth_check():
        return jsonify({"error": "Unauthorized"}), 401

    body     = request.get_json(force=True, silent=True) or {}
    post_url = body.get("post_url", "").strip()

    if not post_url:
        return jsonify({"error": "Missing post_url in request body"}), 400

    import threading
    result_holder = [None]

    def scrape():
        result_holder[0] = run_content_scrape(post_url)

    t = threading.Thread(target=scrape)
    t.start()
    t.join(timeout=35)  # hard 35s limit — Railway times out at 60s

    if result_holder[0] is None:
        log.error(f"Scrape timed out after 35s for {post_url}")
        return jsonify({"status": "error", "message": "Scrape timed out", "content": ""}), 500

    result = result_holder[0]
    return jsonify(result), 200 if result["status"] == "success" else 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})


@app.route("/processed", methods=["GET"])
def list_processed():
    return jsonify(load_processed())


@app.route("/clear", methods=["POST"])
def clear_processed():
    save_processed({})
    return jsonify({"status": "cleared"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
