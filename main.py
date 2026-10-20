import json
import os
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin

from curl_cffi import requests
from bs4 import BeautifulSoup


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
DAILY_STATUS_HOUR_UTC = 8  # Set to None to disable the daily status message.


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
    response = requests.get(
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

    response = requests.patch(
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
        response = requests.post(
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
    except requests.RequestException as error:
        print(f"Slack request failed: {error}")
        return False


# Nettiauto scraper
def fetch_listings():
    """Fetch listings and return unique results, newest first."""
    response = requests.get(
        NETTIAUTO_URL,
        impersonate="chrome",
        timeout=TIMEOUT,
    )

    if response.status_code != 200:
        print(
            f"HTTP {response.status_code}, "
            f"server={response.headers.get('server')}, "
            f"cf-ray={response.headers.get('cf-ray')}"
        )
        print(response.text[:300].replace("\n", " "))
        raise RuntimeError(
            f"Failed to fetch page. HTTP status: {response.status_code}"
        )

    soup = BeautifulSoup(response.content, "html.parser")
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

    # Seed existing listings on the first run without sending a flood of alerts.
    if first_run:
        save_seen_cars([item["id"] for item in reversed(listings)])
        send_slack_notification(
            f"✅ *Nettiauto Alerts initialized.* Tracking {len(listings)} "
            "existing listings. You'll be notified about new ones from now on."
        )
        print(f"First run: seeded {len(listings)} listings.")
        return

    changed = False

    # Send oldest first so Slack messages appear in chronological order.
    for item in reversed(new_listings):
        message = (
            "🚨 *New Nettiauto Match!*\n"
            f"*{slack_escape(item['title'])}*\n"
            f"💰 *Price:* {slack_escape(item['price'])}\n"
            f"🔗 <{item['link']}|View Listing>"
        )

        # Save only successful notifications so failed sends can be retried.
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
