"""
Amazon Jobs GraphQL Capture Tool
=================================
Run this on your home computer while manually applying for a job.
It will capture ALL GraphQL requests and save them to capture_log.json
so we can see exactly what API calls Amazon makes when you apply.

Usage:
    pip install playwright
    python -m playwright install chromium
    python capture.py
"""

import asyncio
import json
import os
from datetime import datetime
from playwright.async_api import async_playwright

COOKIES_FILE = "amazon_cookies.json"
OUTPUT_FILE  = "capture_log.json"

captured = []

async def main():
    print("🚀 Amazon GraphQL Capture Tool")
    print("=" * 50)
    print("1. Browser will open jobsatamazon.co.uk")
    print("2. Manually apply for a job as normal")
    print("3. Go through ALL steps including shift selection")
    print("4. All API calls will be captured automatically")
    print("5. Close browser when done")
    print("=" * 50)
    print()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,  # visible browser so you can interact
            args=["--no-sandbox"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="en-GB",
        )

        # Load cookies if available
        if os.path.exists(COOKIES_FILE):
            with open(COOKIES_FILE) as f:
                cookies = json.load(f)
                for c in cookies:
                    if c.get("sameSite") not in ["Strict", "Lax", "None"]:
                        c["sameSite"] = "Lax"
                await context.add_cookies(cookies)
            print(f"✅ Loaded cookies from {COOKIES_FILE}")

        page = await context.new_page()

        # ── Capture ALL GraphQL requests ──────────────────────────────────
        async def on_request(request):
            if "/graphql" in request.url:
                try:
                    body = request.post_data
                    if body:
                        entry = {
                            "type": "REQUEST",
                            "time": datetime.utcnow().isoformat(),
                            "url": request.url,
                            "method": request.method,
                            "headers": dict(request.headers),
                            "body": json.loads(body) if body else None,
                        }
                        captured.append(entry)

                        # Print operation name
                        try:
                            op = entry["body"].get("operationName", "unknown")
                            print(f"📤 REQUEST: {op}")
                        except:
                            pass
                except Exception as e:
                    print(f"⚠️ Request capture error: {e}")

        async def on_response(response):
            if "/graphql" in response.url and response.status == 200:
                try:
                    body = await response.json()
                    entry = {
                        "type": "RESPONSE",
                        "time": datetime.utcnow().isoformat(),
                        "url": response.url,
                        "status": response.status,
                        "body": body,
                    }
                    captured.append(entry)

                    # Print operation name from response
                    try:
                        ops = list(body.get("data", {}).keys())
                        print(f"📥 RESPONSE: {ops}")
                    except:
                        pass
                except Exception as e:
                    print(f"⚠️ Response capture error: {e}")

        page.on("request", on_request)
        page.on("response", on_response)

        # Open Amazon Jobs
        await page.goto(
            "https://www.jobsatamazon.co.uk/app#/jobSearch",
            wait_until="domcontentloaded"
        )

        print("\n🟢 Browser is open — apply for a job manually now!")
        print("📝 All API calls are being captured...")
        print("🔴 Close the browser window when done\n")

        # Wait for browser to close
        try:
            await page.wait_for_event("close", timeout=600000)  # 10 min timeout
        except:
            pass

        # Save cookies for future use
        cookies = await context.cookies()
        with open(COOKIES_FILE, "w") as f:
            json.dump(cookies, f, indent=2)
        print(f"\n✅ Saved {len(cookies)} cookies to {COOKIES_FILE}")

        await browser.close()

    # Save capture log
    with open(OUTPUT_FILE, "w") as f:
        json.dump(captured, f, indent=2)

    print(f"\n✅ Captured {len(captured)} GraphQL calls → saved to {OUTPUT_FILE}")
    print("\n📊 Summary of captured operations:")

    ops = set()
    for entry in captured:
        if entry["type"] == "REQUEST" and entry.get("body"):
            op = entry["body"].get("operationName", "unknown")
            ops.add(op)

    for op in sorted(ops):
        print(f"  → {op}")

    print(f"\n📁 Send {OUTPUT_FILE} to Claude to build the API-based apply flow!")

if __name__ == "__main__":
    asyncio.run(main())
