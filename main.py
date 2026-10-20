import os
import json
import requests
from bs4 import BeautifulSoup

# ==========================================
# CONFIGURATION
# ==========================================
# Get values from GitHub Secrets using the SECRET NAMES
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_ID = os.environ.get("GIST_ID")

# Fallback URL if GitHub Secret is not loaded
DEFAULT_URL = "https://www.nettiauto.com/en/hakutulokset?haku=P2261268007&sortCol=dateCreated&ord=desc"
NETTIAUTO_URL = os.environ.get("NETTIAUTO_URL") or DEFAULT_URL

# ==========================================
# DATABASE HELPERS (GITHUB GIST)
# ==========================================
def get_seen_cars():
    if not GIST_TOKEN or not GIST_ID:
        print("Missing Gist credentials. Skipping Gist check.")
        return set()
    
    # Extract Gist ID if full URL was accidentally passed
    clean_gist_id = GIST_ID.rstrip("/").split("/")[-1]
    
    headers = {"Authorization": f"token {GIST_TOKEN}"}
    r = requests.get(f"https://api.github.com/gists/{clean_gist_id}", headers=headers)
    
    if r.status_code == 200:
        files = r.json().get("files", {})
        if "seen_cars.json" in files:
            content = files["seen_cars.json"]["content"]
            try:
                return set(json.loads(content))
            except Exception:
                return set()
    else:
        print(f"Gist Fetch Error: HTTP {r.status_code}")
    return set()

def save_seen_cars(seen_set):
    if not GIST_TOKEN or not GIST_ID:
        return
    
    clean_gist_id = GIST_ID.rstrip("/").split("/")[-1]
    headers = {"Authorization": f"token {GIST_TOKEN}"}
    payload = {
        "files": {
            "seen_cars.json": {
                "content": json.dumps(list(seen_set))
            }
        }
    }
    requests.patch(f"https://api.github.com/gists/{clean_gist_id}", headers=headers, json=payload)

# ==========================================
# SCRAPER & NOTIFIER
# ==========================================
def send_slack_notification(message):
    if not SLACK_WEBHOOK_URL:
        print("SLACK_WEBHOOK_URL is not set! Log message instead:")
        print(message)
        return
    payload = {"text": message}
    requests.post(SLACK_WEBHOOK_URL, json=payload)

def main():
    seen_ids = get_seen_cars()
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    print(f"Fetching listings from: {NETTIAUTO_URL}")
    response = requests.get(NETTIAUTO_URL, headers=headers)
    if response.status_code != 200:
        print(f"Failed to fetch page. HTTP status: {response.status_code}")
        return

    soup = BeautifulSoup(response.content, "html.parser")
    listings = soup.find_all("a", class_="data_box")
    
    new_found = False

    for listing in listings:
        link = listing.get("href")
        if not link:
            continue

        listing_id = link.rstrip("/").split("/")[-1]

        if listing_id not in seen_ids:
            seen_ids.add(listing_id)
            new_found = True

            title_elem = listing.find("div", class_="listing_title")
            title = title_elem.get_text(strip=True) if title_elem else "Nettiauto Listing"

            price_elem = listing.find("span", class_="price_val")
            price = price_elem.get_text(strip=True) if price_elem else "N/A"

            msg = f"🚨 *New Nettiauto Match!*\n*{title}*\n💰 *Price:* {price}\n🔗 <{link}|View Listing>"
            send_slack_notification(msg)
            print(f"Notification sent: {title} ({listing_id})")

    if new_found:
        save_seen_cars(seen_ids)
    else:
        send_slack_notification("ℹ️ *Nettiauto Check:* No new cars found in this run.")
        print("No new listings found in this run. Sent update to Slack.")

if __name__ == "__main__":
    main()
