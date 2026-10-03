"""
scraper.py — Render Docker Edition (verbose step logs)
------------------------------------------------------
Headless Chromium inside the container. Every step prints what it's
doing so you can watch the flow in Render's Logs tab.

Example output:
    [open] navigating to https://ytshortdown.com/
    [open] page loaded in 1.82s
    [type] waiting for input field (up to 15s)...
    [type] found input via: getByRole('searchbox')
    [type] typed URL: https://www.youtube.com/shorts/-6_TxuGjvG4
    [fetch] waiting for #downloadSection (auto-fetch, up to 12s)...
    [fetch] auto-loaded in 3.41s
    [quality] opening combobox...
    [quality] found <select> with N options
    [quality] selecting 1080p...
    [quality] selected OK
    [getlink] clicking "Get Link" button...
    [getlink] clicked
    [wait] polling network for video candidates...
    [wait] candidate #1 (source=json): https://cdn400...
    [wait] total candidates: 1
    [rank] scoring candidates...
    [rank] winner: score=110 source=json url=https://cdn400...
    [url] https://cdn400.savetube.vip/media/.../...savetube.me.mp4
"""

import os
import asyncio
import re
import json
import time
from urllib.parse import urlparse, urljoin, unquote

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
def _log(tag: str, msg: str = ""):
    """Print a step log line. One line per call."""
    if msg:
        print(f"[{tag}] {msg}", flush=True)
    else:
        print(f"[{tag}]", flush=True)


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------
_VIDEO_EXT_RE = re.compile(r"\.(mp4|m4v|webm|mkv|mov|ts)(\?|$|#)", re.IGNORECASE)
_HTTP_URL_RE  = re.compile(r'https?://[^\s"\'<>\\\\)\]\}]+', re.IGNORECASE)

_VIDEO_HOSTS = (
    "savetube.vip", "savetube.me", "googlevideo.com", "ytimg.com",
    "akamaized.net", "cloudfront.net", "video.twimg.com",
)

_API_PATH_RE = re.compile(
    r"/(api|download|info|fetch|extract|resolve|get|media/info)(/|$|\?)",
    re.IGNORECASE,
)

_URL_KEY_HINTS = {
    "url", "link", "file", "src", "source", "download", "downloadurl",
    "download_url", "downloadlink", "download_link", "mp4", "hd", "sd",
    "video", "videourl", "video_url", "media", "stream",
}


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _is_video_host(host: str) -> bool:
    return any(h in host for h in _VIDEO_HOSTS)


def _is_json_api(url: str, content_type: str | None) -> bool:
    ct = (content_type or "").lower()
    if "application/json" not in ct:
        host = _host_of(url)
        if not (_is_video_host(host) and _API_PATH_RE.search(url)):
            return False
    lower = url.lower()
    if "/api/random-cdn" in lower or "/v2/info" in lower:
        return False
    return True


def _is_real_video(url: str, content_type: str | None, content_length: int) -> bool:
    if not url or url.startswith(("data:", "blob:", "about:")):
        return False
    host = _host_of(url)
    lower = url.lower()
    ct = (content_type or "").lower()

    if "application/json" in ct:
        return False
    if _API_PATH_RE.search(lower) and not _VIDEO_EXT_RE.search(lower):
        return False
    if _VIDEO_EXT_RE.search(url):
        return True
    if ct.startswith(("video/", "application/octet-stream", "application/mp4")):
        return True
    if "googlevideo.com" in host:
        return True
    if _is_video_host(host) and "/media/" in lower and content_length > 100_000:
        return True
    return False


def _score(url: str, *, source: str, content_type: str | None = None,
           content_length: int = 0) -> int:
    if not url or not url.lower().startswith("http"):
        return -1
    score = 0
    lower = url.lower()
    host = _host_of(url)
    ct = (content_type or "").lower()

    score += {"direct": 50, "json": 40, "redirect": 35, "page": 25, "dom": 20}.get(source, 0)
    if ct.startswith("video/"):                     score += 30
    elif ct.startswith("application/mp4"):          score += 25
    elif ct.startswith("application/octet-stream"): score += 15
    elif "application/json" in ct:                  score -= 40
    elif "text/html" in ct:                         score -= 20
    if _VIDEO_EXT_RE.search(lower):                 score += 25
    if _is_video_host(host):                        score += 20
    if "savetube" in host:                          score += 15
    if "googlevideo" in host:                       score += 10
    if _API_PATH_RE.search(lower):                  score -= 30
    if "/media/" in lower:                          score += 10
    if content_length > 1_000_000:                  score += 15
    elif content_length > 100_000:                  score += 8
    elif 0 < content_length < 20_000:               score -= 15
    if "videoplayback" in lower:                    score += 10
    if lower.rstrip("/").endswith("/download"):     score -= 25
    return score


def _urls_from_json(text: str) -> list[tuple[int, str]]:
    results: list[tuple[int, str]] = []
    try:
        data = json.loads(text)
    except Exception:
        for m in _HTTP_URL_RE.findall(text):
            results.append((1, m))
        return results

    def walk(obj, key_hint=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, k.lower() if isinstance(k, str) else key_hint)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, key_hint)
        elif isinstance(obj, str):
            if obj.startswith("http"):
                results.append((0 if key_hint in _URL_KEY_HINTS else 1, obj))
            else:
                dec = unquote(obj)
                if dec != obj and dec.startswith("http"):
                    results.append((0 if key_hint in _URL_KEY_HINTS else 1, dec))
                else:
                    for m in _HTTP_URL_RE.findall(obj):
                        results.append((0 if key_hint in _URL_KEY_HINTS else 1, m))

    walk(data)
    results.sort(key=lambda p: p[0])
    return results


# ---------------------------------------------------------------------------
# Browser launch — headless Chromium in the container
# ---------------------------------------------------------------------------
async def _launch_chrome(p):
    _log("browser", "launching headless chromium...")
    t0 = time.time()
    browser = await p.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    _log("browser", f"chromium ready in {time.time() - t0:.2f}s")
    return browser


# ---------------------------------------------------------------------------
# Locator helper with per-attempt logging
# ---------------------------------------------------------------------------
async def _first_visible(page, getters, names, per_try_timeout=1500):
    """
    Try each locator, log which one wins. `names` must be parallel to `getters`.
    """
    for getter, name in zip(getters, names):
        try:
            loc = getter()
            await loc.wait_for(state="visible", timeout=per_try_timeout)
            _log("locator", f"found via: {name}")
            return loc
        except Exception:
            continue
    return None


async def _resolve_redirects(page, url: str, max_hops: int = 5) -> str | None:
    current = url
    for hop in range(max_hops):
        try:
            resp = await page.context.request.get(current, max_redirects=0)
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location")
                if not loc:
                    return current
                current = urljoin(current, loc)
                _log("redirect", f"hop {hop + 1} -> {current}")
                continue
            return current
        except Exception:
            return None
    return current


async def _scrape_page_for_video(page, url: str) -> list[str]:
    found: list[str] = []
    try:
        resp = await page.context.request.get(url, timeout=10000)
        if not resp.ok:
            return found
        ct = (resp.headers.get("content-type") or "").lower()
        if "text/html" not in ct and "javascript" not in ct and "json" not in ct:
            return [url]
        text = await resp.text()
    except Exception:
        return found

    for m in _HTTP_URL_RE.findall(text):
        if _VIDEO_EXT_RE.search(m) or _is_video_host(_host_of(m)):
            found.append(m)
    for m in re.findall(r'"(?:url|file|link|download|src|source)"\s*:\s*"(https?:[^"]+)"', text):
        found.append(m.replace("\\/", "/"))
    for m in re.findall(r"(https?://[^\s\"'<>]*savetube[^\s\"'<>]*)", text):
        found.append(m)

    seen, uniq = set(), []
    for u in found:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def fetch_download_url(short_url: str, quality: str = "1080p",
                             timeout_ms: int = 45000) -> dict:
    result = {"success": False, "download_url": None, "quality": quality,
              "error": None, "source": None}
    candidates: list[dict] = []

    def add(url, source, ct=None, length=0):
        if url and url.lower().startswith("http"):
            candidates.append({"url": url, "source": source, "ct": ct, "len": length})
            _log("wait", f"candidate #{len(candidates)} "
                         f"(source={source}, ct={ct}, len={length}): {url[:100]}")

    async with async_playwright() as p:
        browser = await _launch_chrome(p)

        _log("context", "creating browser context...")
        context = await browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"),
            locale="en-US",
            accept_downloads=True,
        )
        page = await context.new_page()
        page.set_default_timeout(timeout_ms)
        _log("context", "context + page ready")

        # ------------------------------------------------------------------
        # Network interception
        # ------------------------------------------------------------------
        async def on_response(response):
            try:
                url = response.url
                h = response.headers or {}
                ct = h.get("content-type")
                try:
                    clen = int(h.get("content-length", "0") or "0")
                except ValueError:
                    clen = 0

                if _is_real_video(url, ct, clen):
                    add(url, "direct", ct, clen)
                    return
                if _is_json_api(url, ct):
                    _log("net", f"json api: {url[:100]}")
                    try:
                        text = await response.text()
                    except Exception:
                        return
                    extracted = _urls_from_json(text)
                    if extracted:
                        _log("net", f"extracted {len(extracted)} URL(s) from JSON")
                    for _, u in extracted:
                        add(u, "json")
            except Exception:
                pass

        page.on("response", on_response)

        try:
            # --- OPEN ---
            _log("open", "navigating to https://ytshortdown.com/")
            t0 = time.time()
            await page.goto("https://ytshortdown.com/",
                            wait_until="domcontentloaded",
                            timeout=timeout_ms)
            _log("open", f"page loaded in {time.time() - t0:.2f}s")
            _log("open", "settling 2s for JS hydration...")
            await page.wait_for_timeout(2000)

            # --- TYPE ---
            _log("type", "waiting for input field (up to 15s)...")
            t0 = time.time()
            search_box = None
            loop = asyncio.get_event_loop()
            deadline = loop.time() + 15

            input_locators = [
                lambda: page.getByRole("searchbox",
                                       name="Paste your YouTube Shorts URL here..."),
                lambda: page.getByPlaceholder("Paste your YouTube Shorts URL here..."),
                lambda: page.getByPlaceholder(re.compile("Paste.*URL", re.I)),
                lambda: page.locator('input[type="search"]').first,
                lambda: page.locator('input[type="url"]').first,
                lambda: page.locator('input[name*="url" i]').first,
                lambda: page.locator('input[placeholder*="Short" i]').first,
                lambda: page.locator("form input[type='text']").first,
                lambda: page.locator("input").first,
            ]
            input_names = [
                "getByRole('searchbox')",
                "getByPlaceholder(exact)",
                "getByPlaceholder(regex)",
                'input[type="search"]',
                'input[type="url"]',
                'input[name*=url i]',
                'input[placeholder*=Short i]',
                "form input[type=text]",
                "first <input>",
            ]

            attempts = 0
            while search_box is None and loop.time() < deadline:
                attempts += 1
                search_box = await _first_visible(
                    page, input_locators, input_names, per_try_timeout=500)
                if search_box is None:
                    await page.wait_for_timeout(500)

            if search_box is None:
                html = await page.content()
                title = await page.title()
                _log("type", f"FAILED after {attempts} attempts "
                             f"({time.time() - t0:.2f}s)")
                _log("type", f"url={page.url}")
                _log("type", f"title={title!r}")
                _log("type", f"html_len={len(html)}")
                raise RuntimeError("URL input not found.")

            _log("type", f"input found after {attempts} attempt(s) "
                         f"({time.time() - t0:.2f}s)")

            _log("type", "clicking input...")
            await search_box.click()
            _log("type", f"typing URL: {short_url}")
            await search_box.fill(short_url)
            _log("type", "typed")

            # --- FETCH ---
            _log("fetch", "waiting for #downloadSection (auto-fetch, up to 12s)...")
            t0 = time.time()
            auto_loaded = False
            try:
                await page.locator("#downloadSection").wait_for(
                    state="visible", timeout=12000)
                auto_loaded = True
                _log("fetch", f"auto-loaded in {time.time() - t0:.2f}s")
            except Exception:
                auto_loaded = False
                _log("fetch", f"auto-load timed out after {time.time() - t0:.2f}s")

            if not auto_loaded:
                _log("fetch", "fallback: trying to click 'Fetch Video' button...")
                fetch_btn = await _first_visible(
                    page,
                    [
                        lambda: page.getByRole("button", name="Fetch Video"),
                        lambda: page.getByRole("button",
                                               name=re.compile("Fetch", re.I)),
                        lambda: page.locator("button:has-text('Fetch Video')").first,
                        lambda: page.locator("button:has-text('Fetch')").first,
                        lambda: page.locator("button[type='submit']").first,
                    ],
                    [
                        "getByRole('button', Fetch Video)",
                        "getByRole('button', /Fetch/i)",
                        "button:has-text('Fetch Video')",
                        "button:has-text('Fetch')",
                        "button[type=submit]",
                    ],
                    per_try_timeout=1500,
                )
                if fetch_btn is not None:
                    _log("fetch", "clicking 'Fetch Video' button")
                    await fetch_btn.click()
                    _log("fetch", "waiting for #downloadSection after click...")
                    await page.locator("#downloadSection").wait_for(
                        state="visible", timeout=timeout_ms)
                    _log("fetch", "#downloadSection visible")
                else:
                    raise RuntimeError(
                        "#downloadSection never appeared and "
                        "'Fetch Video' button not found."
                    )

            await page.wait_for_timeout(150)

            # --- QUALITY ---
            _log("quality", "locating combobox inside #downloadSection...")
            section = page.locator("#downloadSection")
            combo = section.locator("select").first
            if await combo.count() == 0:
                _log("quality", "no <select>, falling back to getByRole('combobox')")
                combo = page.get_by_role("combobox").first

            await combo.wait_for(state="visible", timeout=8000)
            option_count = await combo.locator("option").count()
            _log("quality", f"combobox visible with {option_count} option(s)")

            _log("quality", f"selecting {quality}...")
            selected = False
            for label_attempt, name in (
                (lambda: combo.select_option(label=quality), f"label={quality}"),
                (lambda: combo.select_option(value=quality), f"value={quality}"),
                (lambda: combo.select_option(label=f"{quality} (Full HD)"),
                 f"label={quality} (Full HD)"),
                (lambda: combo.select_option(label=f"{quality} (HD)"),
                 f"label={quality} (HD)"),
            ):
                try:
                    await label_attempt()
                    _log("quality", f"selected via {name}")
                    selected = True
                    break
                except Exception:
                    continue

            if not selected:
                _log("quality", "exact match failed — picking highest option")
                for opt in reversed(await combo.locator("option").all()):
                    val = await opt.get_attribute("value")
                    if val:
                        await combo.select_option(value=val)
                        txt = (await opt.inner_text()).strip()
                        result["quality"] = txt
                        _log("quality", f"fallback selected: {txt}")
                        break

            # --- GET LINK ---
            _log("getlink", "locating 'Get Link' button...")
            get_link_btn = await _first_visible(
                page,
                [
                    lambda: page.getByRole("button", name="Get Link"),
                    lambda: page.getByRole("button",
                                           name=re.compile("Get Link", re.I)),
                    lambda: page.locator("button:has-text('Get Link')").first,
                    lambda: page.locator("a:has-text('Get Link')").first,
                ],
                [
                    "getByRole('button', Get Link)",
                    "getByRole('button', /Get Link/i)",
                    "button:has-text('Get Link')",
                    "a:has-text('Get Link')",
                ],
                per_try_timeout=2000,
            )
            if get_link_btn is None:
                raise RuntimeError("'Get Link' button not found.")

            _log("getlink", "clicking...")
            await get_link_btn.click()
            _log("getlink", "clicked")

            # --- WAIT ---
            _log("wait", "polling network for video candidates...")
            for i in range(40):
                if candidates:
                    _log("wait", f"got {len(candidates)} candidate(s) "
                                 f"after {(i + 1) * 0.25:.2f}s")
                    _log("wait", "settling 1.5s for follow-up responses...")
                    await page.wait_for_timeout(1500)
                    break
                await page.wait_for_timeout(250)

            if not candidates:
                _log("wait", "no candidates after 10s")

            # --- DOM (only if network missed) ---
            if not any(_is_real_video(c["url"], None, 0) for c in candidates):
                _log("dom", "network missed real video — trying DOM...")
                dom_link = await _first_visible(
                    page,
                    [
                        lambda: page.getByRole("link", name="DOWNLOAD"),
                        lambda: page.getByRole("link",
                                               name=re.compile("DOWNLOAD", re.I)),
                        lambda: page.locator("a:has-text('DOWNLOAD')").first,
                        lambda: page.locator("a[href*='.mp4']").first,
                        lambda: page.locator("a[download]").first,
                        lambda: page.locator("a[href*='savetube']").first,
                    ],
                    [
                        "getByRole('link', DOWNLOAD)",
                        "getByRole('link', /DOWNLOAD/i)",
                        "a:has-text('DOWNLOAD')",
                        "a[href*='.mp4']",
                        "a[download]",
                        "a[href*='savetube']",
                    ],
                    per_try_timeout=1500,
                )
                if dom_link is not None:
                    href = await dom_link.get_attribute("href")
                    if not href:
                        href = await dom_link.evaluate(
                            "el => el.href || el.getAttribute('href')")
                    if href and href.lower().startswith("http"):
                        _log("dom", f"found href: {href[:100]}")
                        add(href, "dom")
                else:
                    _log("dom", "no DOWNLOAD link found")
            else:
                _log("dom", "skipped — already have real video from network")

            # --- RESOLVE (only if still no real video) ---
            if not any(_is_real_video(c["url"], None, 0) for c in candidates):
                _log("resolve", "trying redirects + page scraping...")
                seen = set()
                for c in list(candidates):
                    url = c["url"]
                    if url in seen:
                        continue
                    seen.add(url)
                    if _is_real_video(url, None, 0):
                        continue
                    _log("resolve", f"investigating: {url[:80]}")
                    resolved = await _resolve_redirects(page, url)
                    target = resolved if resolved and resolved != url else url
                    for found in await _scrape_page_for_video(page, target):
                        if _is_real_video(found, None, 0):
                            add(found, "redirect" if resolved != url else "page")
                        else:
                            add(found, "page")
            else:
                _log("resolve", "skipped — already have real video from network")

            # --- RANK ---
            _log("rank", f"scoring {len(candidates)} candidate(s)...")
            best, best_score = None, -10_000
            for c in candidates:
                s = _score(c["url"], source=c["source"],
                           content_type=c.get("ct"),
                           content_length=c.get("len") or 0)
                _log("rank", f"  score={s:>4} source={c['source']:<8} "
                             f"{c['url'][:90]}")
                if s > best_score:
                    best_score, best = s, c

            if best and best_score > 0:
                result["success"] = True
                result["download_url"] = best["url"]
                result["source"] = best["source"]
                _log("rank", f"WINNER: score={best_score} source={best['source']}")
            else:
                result["error"] = "No viable download URL found."
                _log("rank", "no candidate scored above 0")

        except PWTimeoutError as e:
            result["error"] = f"Timeout: {e}"
            _log("fail", result["error"])
        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"
            _log("fail", result["error"])
        finally:
            _log("cleanup", "closing context and browser...")
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass
            _log("cleanup", "done")

    return result


def get_download_url(short_url: str, quality: str = "1080p") -> dict:
    res = asyncio.run(fetch_download_url(short_url, quality))
    if res["success"]:
        _log("url", res["download_url"])
    else:
        _log("fail", res["error"])
    return res


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    url = input("Enter a YouTube Shorts URL: ").strip()
    if url:
        get_download_url(url, "1080p")