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

LEETCODE_URL     = "https://leetcode.com/discuss/topic/interview-experience/"
PROCESSED_FILE   = "/data/processed_posts.json"
MAX_POSTS        = 6
REPROCESS_HOURS  = 10
SCRAPE_DELAY     = 2

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
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    driver = webdriver.Chrome(options=opts)
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"}
    )

    if cookies:
        driver.get("https://leetcode.com")
        time.sleep(2)
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
    try:
        driver.get(url)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div[class*='content']"))
        )
        time.sleep(1)
        soup = BeautifulSoup(driver.page_source, "html.parser")
        el = soup.select_one("div.content__u3I1") or soup.find("div", class_=lambda c: c and "content" in c)
        return el.get_text(separator="\n").strip() if el else driver.find_element(By.TAG_NAME, "body").text
    except Exception as e:
        log.error(f"Detail scrape failed for {url}: {e}")
        return None



def is_today_strict(timestamp: str) -> bool:
    """
    Returns True ONLY for posts from TODAY.
    Accepted:  '5 minutes ago', '2 hours ago', 'just now', '30 seconds ago'
    Rejected:  'Mar 18, 2026', '2 days ago', 'yesterday', 'Mar 20, 2026' (absolute = unknown exact time)
    """
    import re
    t = timestamp.strip().lower()

    if not t:
        return False  # no timestamp = skip (safer)

    # Reject absolute dates like "Mar 18, 2026" — even today's absolute date
    # because LeetCode only shows absolute dates for OLDER posts
    if re.search(r"[a-z]{3}\s+\d{1,2},?\s+\d{4}", t):
        return False

    # Reject explicit old relative times
    if "yesterday" in t:
        return False
    m = re.search(r"(\d+)\s+day", t)
    if m and int(m.group(1)) >= 1:
        return False
    if "week" in t or "month" in t or "year" in t:
        return False

    # Accept only: seconds, minutes, hours, just now
    if re.search(r"\d+\s+(second|minute|hour)", t):
        return True
    if "just now" in t:
        return True

    return False  # unknown format → skip to be safe


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


def scrape_listing(driver: webdriver.Chrome) -> list:
    driver.get(LEETCODE_URL)

    # Wait for ANY post card to appear — try multiple selectors
    waited = False
    for wait_sel in [
        "div.flex.flex-col.gap-4",          # outer feed container
        "div[class*='topic-item']",          # topic card
        "a[href*='/discuss/']",              # any discuss link
        "div.overflow-hidden",               # generic card
    ]:
        try:
            WebDriverWait(driver, 15).until(
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
        time.sleep(1)

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

    for el in containers[: MAX_POSTS * 5]:
        if len(posts) >= MAX_POSTS:
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

        # ── Interview experience filter ──────────────────────────────────
        if "interview" not in title.lower() and "experience" not in title.lower():
            log.info(f"Skipping non-interview post: {title!r}")
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
    Scrape the listing page only.
    Returns lightweight post metadata for Make.com to check against its datastore.
    No content scraping, no AI — just the list.
    """
    cookies = load_cookies_from_env()
    driver  = None
    posts   = []

    try:
        driver = build_driver(cookies)
        raw    = scrape_listing(driver)
        for post in raw:
            posts.append({
                "post_id":    post_hash(post["url"]),
                "title":      post["title"],
                "timestamp":  post["timestamp"],
                "post_url":   post["url"],
            })
        log.info(f"List cycle done — {len(posts)} posts")

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
    OpenRouter extraction then runs entirely inside Make.com.
    """
    if not auth_check():
        return jsonify({"error": "Unauthorized"}), 401

    body     = request.get_json(force=True, silent=True) or {}
    post_url = body.get("post_url", "").strip()

    if not post_url:
        return jsonify({"error": "Missing post_url in request body"}), 400

    result = run_content_scrape(post_url)
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
