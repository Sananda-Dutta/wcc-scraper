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
    if not text or len(text.strip()) < 200:
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
    return any(sig in lowered for sig in signatures)


async def _block_heavy_resources(route):
    if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
        await route.abort()
    else:
        await route.continue_()


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
        await page.goto(url, timeout=10000, wait_until="domcontentloaded")
        await page.wait_for_timeout(500)

        title = await page.title()
        body_text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        html = await page.content()

        return {
            "ok": True,
            "title": title,
            "text": body_text,
            "html": html,
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
        stats["browserless_calls"] += 1
        stats["browserless_calls_this_month"] += 1
        return {"ok": True, "html": html, "title": "", "text": ""}


@app.get("/health")
def health():
    return {"status": "ok", "browser_ready": state["browser"] is not None}


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


async def _do_scrape(url: str) -> dict:
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


@app.post("/scrape")
async def scrape(req: ScrapeRequest):
    url = req.url
    if not url.startswith("http"):
        url = "https://" + url

    # Hard overall cap: Phase 1 (~10-13s worst case) + Phase 3 (~15s worst
    # case) could otherwise approach or exceed RapidAPI's gateway timeout.
    # Failing fast and cleanly here is better than letting the gateway
    # kill the connection with an opaque 504.
    try:
        return await asyncio.wait_for(_do_scrape(url), timeout=22.0)
    except asyncio.TimeoutError:
        stats["total_failures"] += 1
        stats["last_error"] = f"{url}: overall scrape timeout (22s)"
        logger.error(f"FAIL  time=22.0s+  url={url}  error=overall timeout")
        return {"success": False, "url": url, "error": "Scrape timed out after 22s"}
