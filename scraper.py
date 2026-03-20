"""
LeetCode Interview Experience Scraper
Hosted on Railway | Triggered by Make.com every 15 minutes
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
OPENROUTER_KEY   = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "mistralai/mistral-7b-instruct:free")
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


# ── OpenRouter AI Extraction ──────────────────────────────────────────────────

EXTRACTION_PROMPT = """\
You are a data extraction assistant. Given the raw text of a LeetCode interview experience post, extract ALL coding problems mentioned.

For each problem, output a JSON array where every element has:
- "problem_title": string
- "problem_description": string (brief, from post)
- "leetcode_url": string (direct LC URL if identifiable, else "")
- "difficulty": "Easy" | "Medium" | "Hard" | "Unknown"
- "date_mentioned": string (any date near this problem, else "")

Rules:
- One object per unique problem.
- Return ONLY the raw JSON array. No markdown, no explanation.
- If no problems found, return [].

POST TEXT:
{post_text}
"""


def extract_problems_via_ai(post_text: str) -> list:
    if not OPENROUTER_KEY:
        log.warning("OPENROUTER_API_KEY not set — skipping AI extraction")
        return []

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_KEY}",
                "Content-Type":  "application/json",
                "HTTP-Referer":  "https://railway.app",
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": [{"role": "user", "content": EXTRACTION_PROMPT.format(post_text=post_text[:6000])}],
                "temperature": 0.1,
            },
            timeout=30,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        problems = json.loads(content.strip())
        return problems if isinstance(problems, list) else []
    except Exception as e:
        log.error(f"OpenRouter extraction failed: {e}")
        return []


# ── Main Scrape Cycle ─────────────────────────────────────────────────────────

def run_scrape_cycle() -> dict:
    processed = load_processed()
    cookies   = load_cookies_from_env()
    driver    = None
    results   = []

    try:
        driver = build_driver(cookies)
        posts  = scrape_listing(driver)

        for post in posts:
            url = post["url"]
            if already_processed(url, processed):
                log.info(f"Duplicate — skipping: {url}")
                continue

            post_text = scrape_post_detail(driver, url)
            if post_text is None:
                continue

            problems = extract_problems_via_ai(post_text)
            base = {
                "post_url":         url,
                "post_title":       post["title"],
                "post_description": post["description"],
                "post_timestamp":   post["timestamp"],
                "scraped_at":       datetime.now(timezone.utc).isoformat(),
            }

            if problems:
                for prob in problems:
                    results.append({**base,
                        "problem_title":       prob.get("problem_title", ""),
                        "problem_description": prob.get("problem_description", ""),
                        "leetcode_url":        prob.get("leetcode_url", ""),
                        "difficulty":          prob.get("difficulty", "Unknown"),
                        "date_mentioned":      prob.get("date_mentioned", ""),
                    })
            else:
                results.append({**base,
                    "problem_title": "", "problem_description": "",
                    "leetcode_url": "", "difficulty": "Unknown", "date_mentioned": ""
                })

            mark_processed(url, processed)

        save_processed(processed)
        log.info(f"Cycle done — {len(results)} rows")

    except Exception as e:
        log.exception(f"Cycle crashed: {e}")
        return {"status": "error", "message": str(e), "data": []}
    finally:
        if driver:
            driver.quit()

    return {"status": "success", "count": len(results), "data": results}


# ── Flask Endpoints ───────────────────────────────────────────────────────────

@app.route("/scrape", methods=["POST", "GET"])
def scrape_endpoint():
    api_key  = request.headers.get("X-API-Key", "")
    expected = os.environ.get("SCRAPER_API_KEY", "")
    if expected and api_key != expected:
        return jsonify({"error": "Unauthorized"}), 401
    result = run_scrape_cycle()
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
