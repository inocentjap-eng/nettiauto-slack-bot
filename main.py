import os
import sys
import json
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ==========================================
# CONFIGURATION
# ==========================================
SLACK_WEBHOOK_URL = (os.environ.get("SLACK_WEBHOOK_URL") or "").strip()
GIST_TOKEN = (os.environ.get("GIST_TOKEN") or "").strip()
GIST_ID = (os.environ.get("GIST_ID") or "").strip()

DEFAULT_URL = "https://www.nettiauto.com/en/hakutulokset?haku=P2261268007&sortCol=dateCreated&ord=desc"
NETTIAUTO_URL = os.environ.get("NETTIAUTO_URL") or DEFAULT_URL

BASE_URL = "https://www.nettiauto.com"
GIST_FILENAME = "seen_cars.json"
MAX_SEEN = 500          # how many listing IDs to remember
TIMEOUT = 15            # seconds for every HTTP request
DAILY_STATUS_HOUR_UTC = 8   # send a "still alive" message once a day; set to None to disable

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


# ==========================================
# DATABASE HELPERS (GITHUB GIST)
# ==========================================
def _gist_url():
    if not GIST_TOKEN or not GIST_ID:
        raise RuntimeError("Missing GIST_TOKEN or GIST_ID. Cannot track seen cars safely.")
    clean_id = GIST_ID.rstrip("/").split("/")[-1]
    return f"https://api.github.com/gists/{clean_id}"


def _gist_headers():
    return {"Authorization": f"token {GIST_TOKEN}", "Accept": "application/vnd.github+json"}


def get_seen_cars():
    """Return (ordered list of seen IDs, whether this is the first run).

    Raises on any failure so we never mistake an API error for an empty history.
    """
    r = requests.get(_gist_url(), headers=_gist_headers(), timeout=TIMEOUT)
        if r.status_code != 200:
        print(f"Token length: {len(GIST_TOKEN)}, starts with: {GIST_TOKEN[:4]!r}")
        raise RuntimeError(f"Gist fetch failed: HTTP {r.status_code} {r.text[:200]}")

    files = r.json().get("files", {})
    if GIST_FILENAME not in files:
        return [], True

    content = files[GIST_FILENAME].get("content", "").strip()
    if not content:
        return [], True

    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{GIST_FILENAME} is not valid JSON: {e}")

    if not isinstance(data, list):
        raise RuntimeError(f"{GIST_FILENAME} must contain a JSON list.")

    return [str(x) for x in data], len(data) == 0


def save_seen_cars(seen_list):
    trimmed = seen_list[-MAX_SEEN:]
    payload = {"files": {GIST_FILENAME: {"content": json.dumps(trimmed)}}}
    r = requests.patch(_gist_url(), headers=_gist_headers(), json=payload, timeout=TIMEOUT)
    r.raise_for_status()


# ==========================================
# SLACK
# ==========================================
def slack_escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_slack_notification(message):
    """Return True if the message was delivered (or logged because no webhook is set)."""
    if not SLACK_WEBHOOK_URL:
        print("SLACK_WEBHOOK_URL is not set! Logging message instead:")
        print(message)
        return True
    try:
        r = requests.post(SLACK_WEBHOOK_URL, json={"text": message}, timeout=TIMEOUT)
        if r.status_code != 200:
            print(f"Slack error: HTTP {r.status_code} {r.text[:200]}")
            return False
        return True
    except requests.RequestException as e:
        print(f"Slack request failed: {e}")
        return False


# ==========================================
# SCRAPER
# ==========================================
def fetch_listings():
    """Return a list of dicts: id, title, price, link (newest first, de-duplicated)."""
    response = requests.get(NETTIAUTO_URL, headers=HEADERS, timeout=TIMEOUT)
    if response.status_code != 200:
        raise RuntimeError(f"Failed to fetch page. HTTP status: {response.status_code}")

    soup = BeautifulSoup(response.content, "html.parser")
    results = []
    found_ids = set()

    for listing in soup.find_all("a", class_="data_box"):
        href = listing.get("href")
        if not href:
            continue

        link = urljoin(BASE_URL, href)
        listing_id = link.split("?")[0].split("#")[0].rstrip("/").split("/")[-1]
        if not listing_id or listing_id in found_ids:
            continue
        found_ids.add(listing_id)

        title_elem = listing.find("div", class_="listing_title")
        title = title_elem.get_text(strip=True) if title_elem else "Nettiauto Listing"

        price_elem = listing.find("span", class_="price_val")
        price = price_elem.get_text(strip=True) if price_elem else "N/A"

        results.append({"id": listing_id, "title": title, "price": price, "link": link})

    return results


# ==========================================
# MAIN
# ==========================================
def main():
    seen, first_run = get_seen_cars()
    seen_set = set(seen)

    print(f"Fetching listings from: {NETTIAUTO_URL}")
    listings = fetch_listings()

    if not listings:
        # Most likely the HTML changed or the site is blocking us.
        # Exiting non-zero makes GitHub mark the run as failed (and email you).
        send_slack_notification(
            "⚠️ *Nettiauto Check:* 0 listings parsed. The site layout may have changed or is blocking us."
        )
        print("No listings parsed from page.")
        sys.exit(1)

    new_listings = [l for l in listings if l["id"] not in seen_set]

    # First run: remember everything silently so you don't get a flood.
    if first_run:
        save_seen_cars([l["id"] for l in reversed(listings)])
        send_slack_notification(
            f"✅ *Nettiauto Alerts initialized.* Tracking {len(listings)} existing listings. "
            "You'll be notified about new ones from now on."
        )
        print(f"First run: seeded {len(listings)} listings.")
        return

    # Notify oldest-first so Slack reads chronologically.
    changed = False
    for item in reversed(new_listings):
        msg = (
            f"🚨 *New Nettiauto Match!*\n"
            f"*{slack_escape(item['title'])}*\n"
            f"💰 *Price:* {slack_escape(item['price'])}\n"
            f"🔗 <{item['link']}|View Listing>"
        )
        # Only mark as seen if Slack accepted it, otherwise retry next run.
        if send_slack_notification(msg):
            seen.append(item["id"])
            changed = True
            print(f"Notification sent: {item['title']} ({item['id']})")
        else:
            print(f"Slack failed for {item['id']}; will retry next run.")

    if changed:
        save_seen_cars(seen)
        return

    print("No new listings found in this run.")

    now = datetime.now(timezone.utc)
    if DAILY_STATUS_HOUR_UTC is not None and now.hour == DAILY_STATUS_HOUR_UTC and now.minute < 15:
        send_slack_notification(
            f"ℹ️ *Nettiauto Check:* still running, no new cars (page shows {len(listings)} listings)."
        )


if __name__ == "__main__":
    main()
