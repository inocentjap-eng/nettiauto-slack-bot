import os
import json
import requests
from bs4 import BeautifulSoup

# ==========================================
# CONFIGURATION
# ==========================================
SLACK_WEBHOOK_URL = os.environ.get("https://hooks.slack.com/services/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")
GIST_TOKEN = os.environ.get("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")
GIST_ID = os.environ.get("https://gist.github.com/inocentjap-eng/99ed42460c9d40d4b8d3b43af4b31864")
NETTIAUTO_URL = os.environ.get("https://www.nettiauto.com/en/hakutulokset?haku=P2261268007&sortCol=dateCreated&ord=desc")

# ==========================================
# DATABASE HELPERS (GITHUB GIST)
# ==========================================
def get_seen_cars():
    headers = {"Authorization": f"token {GIST_TOKEN}"}
    r = requests.get(f"https://api.github.com/gists/{GIST_ID}", headers=headers)
    if r.status_code == 200:
        files = r.json().get("files", {})
        if "seen_cars.json" in files:
            content = files["seen_cars.json"]["content"]
            return set(json.loads(content))
    return set()

def save_seen_cars(seen_set):
    headers = {"Authorization": f"token {GIST_TOKEN}"}
    payload = {
        "files": {
            "seen_cars.json": {
                "content": json.dumps(list(seen_set))
            }
        }
    }
    requests.patch(f"https://api.github.com/gists/{GIST_ID}", headers=headers, json=payload)

# ==========================================
# SCRAPER & NOTIFIER
# ==========================================
def send_slack_notification(message):
    payload = {"text": message}
    requests.post(SLACK_WEBHOOK_URL, json=payload)

def main():
    seen_ids = get_seen_cars()
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
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

            # Message sent when a new car is found
            msg = f"🚨 *New Nettiauto Match!*\n*{title}*\n💰 *Price:* {price}\n🔗 <{link}|View Listing>"
            send_slack_notification(msg)
            print(f"Notification sent: {title} ({listing_id})")

    if new_found:
        save_seen_cars(seen_ids)
    else:
        # Message sent when no new cars are found
        send_slack_notification("ℹ️ *Nettiauto Check:* No new cars found in this run.")
        print("No new listings found in this run. Sent update to Slack.")

if __name__ == "__main__":
    main()
