"""
hebpsy.net clinical-psychology job scanner.

Runs on a schedule (GitHub Actions). Each run it:
  1. Reads the "דרושים" boards for every region.
  2. Opens each *new* post and reads the full text.
  3. Keeps the ones that fit a clinical-psychology master's grad / intern
     (or an "open" psychologist post with no specialization named).
  4. Writes docs/jobs.json — the data your web page reads.
  5. (Optional, off by default) emails you the new ones.

You don't need to edit anything. Optional knobs are marked below.
"""

import json
import os
import re
import smtplib
import time
from datetime import date, datetime
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup

# --- optional knobs --------------------------------------------------------

# Region boards on hebpsy. Label -> typ code.
REGIONS = {
    "מרכז": "1",
    "דרום": "15",
    "צפון": "14",
    "ירושלים": "17",
    "כל הארץ": "20",
}
PAGES_PER_REGION = 2          # how many pages of each board to read
KEEP_DAYS = 45                # forget jobs we haven't seen on the board for this long

# --- fixed bits ------------------------------------------------------------

BASE = "https://www.hebpsy.net"
STATE_FILE = "state.json"
OUT_FILE = "docs/jobs.json"
TODAY = date.today().isoformat()
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"}

CLINICAL_PHRASES = [
    "פסיכולוגיה קלינית", "פסיכולוג קליני", "פסיכולוגית קלינית", "פסיכולוג/ית קליני",
    "פסיכולוגים קליניים", "פסיכולוגיות קליניות", "מתמחה בפסיכולוגיה קלינית",
    "מתמחים בפסיכולוגיה קלינית", "התמחות בפסיכולוגיה קלינית", "התמחות קלינית",
]
OTHER_TRACKS = ["חינוכי", "חינוכית", "שיקומי", "שיקומית", "התפתחותי", "התפתחותית", "רפואי", "רפואית"]
HIRING = ["דרוש", "דרושה", "דרושים", "דרושות", "מחפש", "מחפשים", "מחפשות", "מוזמנים", "מוזמנות"]


def is_relevant(title, snippet, body):
    full = " ".join([title or "", snippet or "", body or ""])
    clean = " ".join([title or "", snippet or ""])
    for phrase in CLINICAL_PHRASES:
        if phrase in full:
            return True, "מוזכר במפורש: " + phrase
    is_psych = "פסיכולוג" in clean
    clinical_word = re.search(r"קליני(ת|ים|ות)?\b", clean) is not None
    if is_psych and clinical_word:
        return True, "משרת פסיכולוג/ית הכוללת מסלול קליני"
    if any(w in clean for w in HIRING) and is_psych and not any(w in clean for w in OTHER_TRACKS):
        return True, "משרת פסיכולוג/ית ללא התמחות מוגדרת (פתוחה)"
    return False, ""


def place_tags(text):
    tags = []
    if "תל אביב" in text or "תל-אביב" in text or 'ת"א' in text:
        tags.append("תל אביב")
    if "באר שבע" in text or "באר-שבע" in text or "בן גוריון" in text or "בן-גוריון" in text:
        tags.append("באר שבע")
    return tags


def get(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.encoding = r.apparent_encoding or "utf-8"
    r.raise_for_status()
    return r.text


def list_posts():
    """id -> {title, snippet, date, areas:set}."""
    posts = {}
    for area, typ in REGIONS.items():
        for page in range(1, PAGES_PER_REGION + 1):
            url = f"{BASE}/bulletinBoard_list.asp?cat=folder&typ={typ}&page={page}"
            try:
                soup = BeautifulSoup(get(url), "html.parser")
            except Exception as e:
                print(f"דילוג על {area} עמ' {page}: {e}")
                continue
            for a in soup.select("a[href*='bulletinBoard.asp?id=']"):
                m = re.search(r"bulletinBoard\.asp\?id=(\d+)", a.get("href", ""))
                if not m:
                    continue
                pid = m.group(1)
                title = a.get_text(strip=True)
                if not title:
                    continue
                container = a.find_parent(["div", "td", "li", "article"]) or a.parent
                ctext = container.get_text(" ", strip=True) if container else title
                snippet = ctext.replace(title, "", 1).strip()
                snippet = re.sub(r"\s+", " ", snippet)[:220]
                dm = re.search(r"\b(\d{1,2}/\d{1,2}/\d{4})\b", ctext)
                p = posts.setdefault(pid, {"title": title, "snippet": snippet,
                                           "date": dm.group(1) if dm else "", "areas": set()})
                p["areas"].add(area)
            time.sleep(1)
    return posts


def post_body(url):
    soup = BeautifulSoup(get(url), "html.parser")
    for tag in soup(["nav", "header", "footer", "script", "style", "aside"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True)


def maybe_email(new_jobs):
    """Sends only if EMAIL_TO + SMTP_USER + SMTP_PASS secrets exist. Off otherwise."""
    to = os.environ.get("EMAIL_TO")
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    if not (to and user and pw and new_jobs):
        return
    lines = [f"{j['title']}\n{', '.join(j['areas'])} | {j['url']}\n" for j in new_jobs]
    msg = MIMEText(f"{len(new_jobs)} משרות חדשות:\n\n" + "\n".join(lines), _charset="utf-8")
    msg["Subject"] = f"[jobtracker] {len(new_jobs)} משרות חדשות"
    msg["From"], msg["To"] = user, to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.sendmail(user, [to], msg.as_string())
    print(f"נשלח מייל עם {len(new_jobs)} משרות.")


def main():
    first_run = not os.path.exists(STATE_FILE)
    state = {}
    if not first_run:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    seen = state.get("seen", {})   # id -> {relevant, reason, first_seen}

    board = list_posts()
    print(f"נמצאו {len(board)} מודעות בכל הלוחות.")

    # classify any post we haven't fetched before
    for pid, info in board.items():
        if pid in seen:
            continue
        try:
            body = post_body(f"{BASE}/bulletinBoard.asp?id={pid}")
        except Exception as e:
            print(f"דילוג על {pid}: {e}")
            continue
        ok, reason = is_relevant(info["title"], info["snippet"], body)
        seen[pid] = {"relevant": ok, "reason": reason, "first_seen": TODAY}
        time.sleep(1)

    # build the display list = relevant jobs currently on the board
    jobs, new_jobs = [], []
    for pid, info in board.items():
        rec = seen.get(pid)
        if not rec or not rec["relevant"]:
            continue
        seen[pid]["last_seen"] = TODAY
        job = {
            "id": pid,
            "title": info["title"],
            "snippet": info["snippet"],
            "date": info["date"],
            "url": f"{BASE}/bulletinBoard.asp?id={pid}",
            "areas": sorted(info["areas"]),
            "tags": place_tags(info["title"] + " " + info["snippet"]),
            "reason": rec["reason"],
            "first_seen": rec["first_seen"],
        }
        jobs.append(job)
        if rec["first_seen"] == TODAY and not first_run:
            new_jobs.append(job)

    # forget very old entries so state.json stays small
    for pid in list(seen):
        last = seen[pid].get("last_seen", seen[pid]["first_seen"])
        age = (date.today() - date.fromisoformat(last)).days
        if age > KEEP_DAYS:
            del seen[pid]

    jobs.sort(key=lambda j: (j["first_seen"], j["date"]), reverse=True)

    os.makedirs("docs", exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated": datetime.now().isoformat(timespec="minutes"),
                   "count": len(jobs), "jobs": jobs}, f, ensure_ascii=False, indent=1)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"seen": seen}, f, ensure_ascii=False, indent=0)

    print(f"סה\"כ תואמות בלוח: {len(jobs)} | חדשות היום: {len(new_jobs)}")
    try:
        maybe_email(new_jobs)
    except Exception as e:
        print(f"שליחת מייל נכשלה (לא קריטי): {e}")


if __name__ == "__main__":
    main()
