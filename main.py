import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

import cloudscraper
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth


# Configuration
SLACK_WEBHOOK_URL = (os.environ.get("SLACK_WEBHOOK_URL") or "").strip()
GIST_TOKEN = (os.environ.get("GIST_TOKEN") or "").strip()
GIST_ID = (os.environ.get("GIST_ID") or "").strip()

DEFAULT_URL = (
    "https://www.nettiauto.com/en/hakutulokset"
    "?haku=P2261268007&sortCol=dateCreated&ord=desc"
)
NETTIAUTO_URL = (os.environ.get("NETTIAUTO_URL") or "").strip() or DEFAULT_URL

BASE_URL = "https://www.nettiauto.com"
GIST_FILENAME = "seen_cars.json"
MAX_SEEN = 500
TIMEOUT = 15
DAILY_STATUS_HOUR_UTC = 8

# Shared HTTP session for Gist + Slack calls
http = cloudscraper.create_scraper(
    browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False},
)


# GitHub Gist helpers
def gist_url():
    if not GIST_TOKEN or not GIST_ID:
        raise RuntimeError("Missing GIST_TOKEN or GIST_ID.")

    clean_gist_id = GIST_ID.rstrip("/").split("/")[-1]
    return f"https://api.github.com/gists/{clean_gist_id}"


def gist_headers():
    return {
        "Authorization": f"token {GIST_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "nettiauto-alert-bot",
    }


def get_seen_cars():
    """Return (ordered list of seen listing IDs, whether this is the first run)."""
    response = http.get(
        gist_url(),
        headers=gist_headers(),
        timeout=TIMEOUT,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Gist fetch failed: HTTP {response.status_code} "
            f"{response.text[:200]}"
        )

    files = response.json().get("files", {})
    gist_file = files.get(GIST_FILENAME)

    if not gist_file:
        return [], True

    content = (gist_file.get("content") or "").strip()
    if not content:
        return [], True

    try:
        data = json.loads(content)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"{GIST_FILENAME} contains invalid JSON: {error}"
        ) from error

    if not isinstance(data, list):
        raise RuntimeError(f"{GIST_FILENAME} must contain a JSON list.")

    seen_ids = [str(item) for item in data]
    return seen_ids, len(seen_ids) == 0


def save_seen_cars(seen_ids):
    trimmed_ids = seen_ids[-MAX_SEEN:]
    payload = {
        "files": {
            GIST_FILENAME: {
                "content": json.dumps(trimmed_ids)
            }
        }
    }

    response = http.patch(
        gist_url(),
        headers=gist_headers(),
        json=payload,
        timeout=TIMEOUT,
    )
    response.raise_for_status()


# Slack helpers
def slack_escape(value):
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def send_slack_notification(message):
    """Send a Slack message. Return False if delivery fails."""
    if not SLACK_WEBHOOK_URL:
        print("SLACK_WEBHOOK_URL is not set; message will only be logged:")
        print(message)
        return True

    try:
        response = http.post(
            SLACK_WEBHOOK_URL,
            json={"text": message},
            timeout=TIMEOUT,
        )
        if response.status_code != 200:
            print(
                f"Slack error: HTTP {response.status_code} "
                f"{response.text[:200]}"
            )
            return False
        return True
    except Exception as error:
        print(f"Slack request failed: {error}")
        return False


# Nettiauto scraper (Playwright + stealth)
def fetch_listings():
    """Fetch listings using a real browser with stealth to bypass Cloudflare."""
    with Stealth().use_sync(sync_playwright()) as p:
        browser = p.chromium.launch(
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-setuid-sandbox",
                "--window-size=1920,1080",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="Europe/Helsinki",
        )
        page = context.new_page()

        page.goto(NETTIAUTO_URL, wait_until="domcontentloaded", timeout=30000)

        # Wait for Cloudflare challenge to clear
        for attempt in range(15):
            title = page.title().lower()
            if "just a moment" not in title and "checking your browser" not in title:
                break
            time.sleep(1)

        try:
            page.wait_for_selector("a.data_box", timeout=20000)
        except Exception:
            print("Still blocked. Taking screenshot for debugging...")
            page.screenshot(path="debug_block.png")
            print(f"Page title: {page.title()}")
            print(f"Page URL: {page.url}")
            print(page.content()[:500])
            browser.close()
            raise RuntimeError("Cloudflare challenge did not clear.")

        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "html.parser")
    listings = []
    found_ids = set()

    for listing in soup.find_all("a", class_="data_box"):
        href = listing.get("href")
        if not href:
            continue

        link = urljoin(BASE_URL, href)
        listing_id = (
            link.split("?")[0]
            .split("#")[0]
            .rstrip("/")
            .split("/")[-1]
        )

        if not listing_id or listing_id in found_ids:
            continue

        found_ids.add(listing_id)

        title_element = listing.find("div", class_="listing_title")
        title = (
            title_element.get_text(strip=True)
            if title_element
            else "Nettiauto Listing"
        )

        price_element = listing.find("span", class_="price_val")
        price = (
            price_element.get_text(strip=True)
            if price_element
            else "N/A"
        )

        listings.append(
            {
                "id": listing_id,
                "title": title,
                "price": price,
                "link": link,
            }
        )

    return listings


# Main
def main():
    seen_ids, first_run = get_seen_cars()
    seen_set = set(seen_ids)

    print(f"Fetching listings from: {NETTIAUTO_URL}")
    listings = fetch_listings()

    if not listings:
        send_slack_notification(
            "⚠️ *Nettiauto Check:* 0 listings parsed. "
            "The site layout may have changed or may be blocking the request."
        )
        print("No listings parsed from page.")
        sys.exit(1)

    new_listings = [
        item for item in listings
        if item["id"] not in seen_set
    ]

    if first_run:
        save_seen_cars([item["id"] for item in reversed(listings)])
        send_slack_notification(
            f"✅ *Nettiauto Alerts initialized.* Tracking {len(listings)} "
            "existing listings. You'll be notified about new ones from now on."
        )
        print(f"First run: seeded {len(listings)} listings.")
        return

    changed = False

    for item in reversed(new_listings):
        message = (
            "🚨 *New Nettiauto Match!*\n"
            f"*{slack_escape(item['title'])}*\n"
            f"💰 *Price:* {slack_escape(item['price'])}\n"
            f"🔗 <{item['link']}|View Listing>"
        )

        if send_slack_notification(message):
            seen_ids.append(item["id"])
            changed = True
            print(f"Notification sent: {item['title']} ({item['id']})")
        else:
            print(f"Slack failed for {item['id']}; it will retry next run.")

    if changed:
        save_seen_cars(seen_ids)
        return

    if not new_listings:
        print("No new listings found in this run.")

        now = datetime.now(timezone.utc)
        if (
            DAILY_STATUS_HOUR_UTC is not None
            and now.hour == DAILY_STATUS_HOUR_UTC
            and now.minute < 15
        ):
            send_slack_notification(
                "ℹ️ *Nettiauto Check:* still running, no new cars "
                f"(page shows {len(listings)} listings)."
            )


if __name__ == "__main__":
    main()   
