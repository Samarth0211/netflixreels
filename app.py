import os
import json
import re
from typing import List, Optional

import uvicorn
import httpx
from fastapi import FastAPI, Body, HTTPException
from pydantic import BaseModel, HttpUrl

# Playwright (headless Chromium) for tough cases / scrolling the Reels grid
from playwright.sync_api import sync_playwright

app = FastAPI(title="IG Reels Resolver", version="1.0")

# ---------- Models ----------
class ResolveIn(BaseModel):
    reel_url: HttpUrl
    cookie_header: Optional[str] = None  # e.g., "csrftoken=...; sessionid=...; ds_user_id=..."

class ResolveOut(BaseModel):
    reel_url: HttpUrl
    mp4_url: Optional[HttpUrl] = None
    filename: Optional[str] = None
    title: Optional[str] = None

class ReelsQuery(BaseModel):
    max: Optional[int] = None
    cookie_header: Optional[str] = None

class ReelsOut(BaseModel):
    username: str
    count: int
    reels: List[HttpUrl]

# ---------- Helpers ----------
UA = os.getenv("IG_UA", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36")

def _extract_og_video(html: str) -> Optional[str]:
    # Instagram pages usually expose <meta property="og:video" content="...mp4">
    m = re.search(r'<meta\s+property="og:video"\s+content="([^"]+)"', html, flags=re.I)
    return m.group(1) if m else None

def _http_get(url: str, cookie_header: Optional[str]) -> str:
    headers = {"User-Agent": UA}
    if cookie_header:
        headers["Cookie"] = cookie_header
    with httpx.Client(follow_redirects=True, headers=headers, timeout=30) as client:
        r = client.get(url)
        if r.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"Upstream status={r.status_code}")
        return r.text

def _playwright_fetch_html(url: str, cookie_header: Optional[str]) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA)
        # If cookies supplied, parse and add to context
        if cookie_header:
            cookies = []
            for part in cookie_header.split(";"):
                part = part.strip()
                if "=" not in part:
                    continue
                name, value = part.split("=", 1)
                # set cookie for .instagram.com root
                cookies.append({"name": name.strip(), "value": value.strip(), "domain": ".instagram.com", "path": "/"})
            if cookies:
                context.add_cookies(cookies)

        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        # Wait a bit; sometimes meta tags set after client-side
        page.wait_for_timeout(1500)
        html = page.content()
        context.close()
        browser.close()
        return html

def _playwright_collect_reels(username: str, max_count: Optional[int], cookie_header: Optional[str]) -> List[str]:
    reels_url = f"https://www.instagram.com/{username.strip().lstrip('@')}/reels/"
    seen = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA)
        if cookie_header:
            cookies = []
            for part in cookie_header.split(";"):
                part = part.strip()
                if "=" not in part:
                    continue
                name, value = part.split("=", 1)
                cookies.append({"name": name.strip(), "value": value.strip(), "domain": ".instagram.com", "path": "/"})
            if cookies:
                context.add_cookies(cookies)

        page = context.new_page()
        page.goto(reels_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1200)

        idle_rounds, idle_limit = 0, 15
        while True:
            hrefs = page.eval_on_selector_all(
                "a[href*='/reel/']",
                "els => els.map(e => e.href)"
            )
            new = 0
            for href in hrefs:
                if "/reel/" not in href:
                    continue
                if not href.endswith("/"):
                    href = href + "/"
                if href not in seen:
                    seen.add(href); new += 1
                    if max_count and len(seen) >= max_count:
                        break
            if max_count and len(seen) >= max_count:
                break
            if new == 0:
                idle_rounds += 1
            else:
                idle_rounds = 0
            if idle_rounds >= idle_limit:
                break
            page.evaluate("window.scrollBy(0, document.body.scrollHeight);")
            page.wait_for_timeout(1200)

        context.close()
        browser.close()
    return sorted(seen)

# ---------- Routes ----------
@app.get("/", tags=["health"])
def health():
    return {"ok": True, "service": "ig-reels-resolver", "version": "1.0"}

@app.post("/resolve", response_model=ResolveOut, tags=["resolve"])
def resolve_direct_mp4(payload: ResolveIn):
    reel_url = str(payload.reel_url)
    # 1) Try simple HTTP GET + og:video
    try:
        html = _http_get(reel_url, payload.cookie_header)
        mp4 = _extract_og_video(html)
        if mp4:
            # Build a simple filename suggestion from URL id
            vid_id = mp4.split("/")[-1].split("?")[0]
            return ResolveOut(reel_url=reel_url, mp4_url=mp4, filename=f"{vid_id}.mp4", title=None)
    except HTTPException:
        # fallthrough to Playwright
        pass
    except Exception:
        pass

    # 2) Fallback to Playwright-rendered page & read og:video
    html2 = _playwright_fetch_html(reel_url, payload.cookie_header)
    mp42 = _extract_og_video(html2)
    if not mp42:
        raise HTTPException(status_code=502, detail="Could not resolve mp4_url from reel page")
    vid_id = mp42.split("/")[-1].split("?")[0]
    return ResolveOut(reel_url=reel_url, mp4_url=mp42, filename=f"{vid_id}.mp4", title=None)

@app.post("/reels/{username}", response_model=ReelsOut, tags=["list"])
def list_reels(username: str, q: ReelsQuery = Body(default=ReelsQuery())):
    try:
        urls = _playwright_collect_reels(username, q.max, q.cookie_header)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to collect reels: {e}")
    return ReelsOut(username=username, count=len(urls), reels=urls)

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
