import os
import time
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI
from pydantic import BaseModel
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

import trafilatura
from bs4 import BeautifulSoup
from langdetect import detect, LangDetectException

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("scraper")

# ---------------------------------------------------------------------------
# Optional error tracking via Sentry. If SENTRY_DSN isn't set, this quietly
# does nothing — the service still works fine without it.
# ---------------------------------------------------------------------------
SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if SENTRY_DSN:
    import sentry_sdk
    sentry_sdk.init(dsn=SENTRY_DSN, traces_sample_rate=0.1)
    logger.info("Sentry error tracking enabled.")
else:
    logger.info("SENTRY_DSN not set — Sentry error tracking disabled.")

BROWSERLESS_TOKEN = os.environ.get("BROWSERLESS_TOKEN", "")

# Browserless free tier is typically limited (check your plan's exact cap).
# This is a soft, in-memory safety cap so a bad day of blocked sites can't
# silently burn through your whole monthly quota without you noticing.
BROWSERLESS_MONTHLY_CAP = int(os.environ.get("BROWSERLESS_MONTHLY_CAP", "900"))

BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}

# Holds the shared playwright + browser objects for the whole app lifetime
state = {"playwright_cm": None, "playwright": None, "browser": None}

# In-memory stats — resets if the Space restarts/sleeps, but gives you a
# live picture without needing to dig through logs.
stats = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "total_requests": 0,
    "phase1_success": 0,
    "phase1_failed": 0,
    "browserless_calls": 0,
    "browserless_calls_this_month": 0,
    "browserless_month": datetime.now(timezone.utc).strftime("%Y-%m"),
    "total_failures": 0,
    "last_error": None,
}


def _reset_browserless_counter_if_new_month():
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    if stats["browserless_month"] != current_month:
        stats["browserless_month"] = current_month
        stats["browserless_calls_this_month"] = 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting shared Chromium instance...")
    stealth = Stealth()
    playwright_cm = stealth.use_async(async_playwright())
    playwright = await playwright_cm.__aenter__()
    browser = await playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    )
    state["playwright_cm"] = playwright_cm
    state["playwright"] = playwright
    state["browser"] = browser
    logger.info("Shared Chromium launched successfully.")
    yield
    logger.info("Shutting down Chromium...")
    await browser.close()
    await playwright_cm.__aexit__(None, None, None)


app = FastAPI(lifespan=lifespan)


class ScrapeRequest(BaseModel):
    url: str

def looks_blocked_or_thin(text: str) -> bool:
    if not text:
        return True

    signatures = [
        "checking your browser",
        "enable javascript",
        "are you human",
        "access denied",
        "attention required",
        "captcha",
        "just a moment",
    ]
    lowered = text.lower()

    # Signature match = definitely blocked, regardless of length
    if any(sig in lowered for sig in signatures):
        return True

    # No blocking signature found — use length only as a weaker secondary
    # signal, and drop the threshold since legitimate short pages exist
    # (landing pages, minimal SaaS homepages, etc.)
    if len(text.strip()) < 80:
        return True

    return False


async def _block_heavy_resources(route):
    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
        await route.abort()
    else:
        await route.continue_()

def extract_structured_content(html: str, raw_text: str) -> dict:
    """
    Pulls clean article text + high-signal metadata out of raw HTML.
    Falls back gracefully at every step if a given extractor comes up empty.
    """
    # --- Clean main content via trafilatura (strips nav/ads/footers) ---
    clean_text = trafilatura.extract(
        html, include_comments=False, include_tables=False
    ) or ""

    # If trafilatura found nothing usable (happens on some non-article
    # pages like pure landing pages), fall back to the raw innerText
    # rather than returning empty.
    if len(clean_text.strip()) < 50:
        clean_text = raw_text

    # --- Meta description, OG tags, headings via BeautifulSoup ---
    soup = BeautifulSoup(html, "html.parser")

    meta_tag = soup.find("meta", attrs={"name": "description"})
    meta_description = meta_tag.get("content", "").strip() if meta_tag else ""

    og_tag = soup.find("meta", attrs={"property": "og:description"})
    og_description = og_tag.get("content", "").strip() if og_tag else ""

    headings = " ".join(
        h.get_text(strip=True) for h in soup.find_all(["h1", "h2", "h3"])
    )

    # --- Language detection ---
    lang = "unknown"
    detect_source = (clean_text or raw_text)[:500]
    if len(detect_source.strip()) >= 20:
        try:
            lang = detect(detect_source)
        except LangDetectException:
            lang = "unknown"

    return {
        "clean_text": clean_text[:4000],
        "meta_description": meta_description,
        "og_description": og_description,
        "headings": headings,
        "language": lang,
    }


async def scrape_with_local_browser(url: str) -> dict:
    browser = state["browser"]
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1366, "height": 768},
    )
    try:
        page = await context.new_page()
        await page.route("**/*", _block_heavy_resources)

        # Item 4: slightly longer timeout + longer settle wait for SPAs
        await page.goto(url, timeout=12000, wait_until="domcontentloaded")
        await page.wait_for_timeout(1200)

        title = await page.title()
        body_text = await page.evaluate(
            "() => document.body ? document.body.innerText : ''"
        )
        html = await page.content()

        # Items 1 + 2: structured extraction
        structured = extract_structured_content(html, body_text)

        return {
            "ok": True,
            "title": title,
            "text": body_text,
            "html": html,
            **structured,
        }
    finally:
        await context.close()


async def scrape_with_browserless(url: str) -> dict:
    if not BROWSERLESS_TOKEN:
        return {"ok": False, "error": "No BROWSERLESS_TOKEN configured"}

    _reset_browserless_counter_if_new_month()
    if stats["browserless_calls_this_month"] >= BROWSERLESS_MONTHLY_CAP:
        logger.warning(
            f"Browserless monthly cap ({BROWSERLESS_MONTHLY_CAP}) reached — refusing call to avoid overage."
        )
        return {"ok": False, "error": "Browserless monthly usage cap reached"}

    endpoint = f"https://chrome.browserless.io/content?token={BROWSERLESS_TOKEN}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(endpoint, json={"url": url})
        resp.raise_for_status()
        html = resp.text

        # Browserless reports the TARGET site's real status here — separate
        # from Browserless's own 200 OK, which just means "request attempted."
        target_status = resp.headers.get("X-Response-Code", "")
        stats["browserless_calls"] += 1
        stats["browserless_calls_this_month"] += 1

        if target_status and not target_status.startswith("2"):
            return {
                "ok": False,
                "error": f"Target site returned {target_status} (likely blocking Browserless's IP)"
            }

        if len(html.strip()) < 100:
            return {"ok": False, "error": "Browserless returned near-empty content"}

        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text(strip=True) if soup.title else ""
        structured = extract_structured_content(html, "")

        return {
            "ok": True,
            "html": html,
            "title": title,
            "text": structured["clean_text"],
            **structured,
        }
    
@app.get("/health")
@app.head("/health")
def health():
    return {
        "status": "ok",
        "browser_ready": state["browser"] is not None
    }

@app.get("/stats")
def get_stats():
    _reset_browserless_counter_if_new_month()
    return {
        **stats,
        "browserless_cap": BROWSERLESS_MONTHLY_CAP,
        "browserless_remaining_this_month": max(
            0, BROWSERLESS_MONTHLY_CAP - stats["browserless_calls_this_month"]
        ),
    }


@app.post("/scrape")
async def scrape(req: ScrapeRequest):
    url = req.url
    if not url.startswith("http"):
        url = "https://" + url

    stats["total_requests"] += 1
    start = time.monotonic()

    # Phase 1: local Playwright + stealth
    try:
        result = await scrape_with_local_browser(url)
        if result["ok"] and not looks_blocked_or_thin(result["text"]):
            elapsed = round(time.monotonic() - start, 2)
            stats["phase1_success"] += 1
            logger.info(f"OK  source=local  time={elapsed}s  url={url}")
            return {
                "success": True,
                "source": "local",
                "url": url,
                "title": result["title"],
                "html": result["html"],
                "data": result["text"],
                "clean_text": result.get("clean_text", ""),
                "meta_description": result.get("meta_description", ""),
                "og_description": result.get("og_description", ""),
                "headings": result.get("headings", ""),
                "language": result.get("language", "unknown"),
            }
        
        stats["phase1_failed"] += 1
        logger.info(f"Phase 1 thin/blocked result for {url}, escalating to Phase 3 (Browserless).")
    except Exception as e:
        stats["phase1_failed"] += 1
        logger.warning(f"Phase 1 failed for {url}: {type(e).__name__}: {e}")
        if SENTRY_DSN:
            import sentry_sdk
            sentry_sdk.capture_exception(e)

    # Phase 3: Browserless.io fallback
    try:
        fallback = await scrape_with_browserless(url)
        elapsed = round(time.monotonic() - start, 2)
        if fallback["ok"]:
            logger.info(f"OK  source=browserless  time={elapsed}s  url={url}")
            return {
                "success": True,
                "source": "browserless",
                "url": url,
                "title": fallback.get("title", ""),
                "html": fallback["html"],
                "data": fallback.get("text", ""),
            }
        else:
            stats["total_failures"] += 1
            stats["last_error"] = f"{url}: {fallback['error']}"
            logger.error(f"FAIL  time={elapsed}s  url={url}  error={fallback['error']}")
            return {"success": False, "url": url, "error": fallback["error"]}
    except Exception as e:
        elapsed = round(time.monotonic() - start, 2)
        stats["total_failures"] += 1
        stats["last_error"] = f"{url}: {type(e).__name__}: {e}"
        logger.error(f"FAIL  time={elapsed}s  url={url}  error={type(e).__name__}: {e}")
        if SENTRY_DSN:
            import sentry_sdk
            sentry_sdk.capture_exception(e)
        return {"success": False, "url": url, "error": f"{type(e).__name__}: {e}"}
