import os
import sqlite3
import hashlib
import requests
from bs4 import BeautifulSoup
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import pytz
import datetime

# --- Telegram Setup ---
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

def send_message(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, data=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print("Telegram error:", e)

# --- Title filter (lower-case substrings) ---
FILTER_TITLES = [
    "junior clinical fellow", "jcf", "fy2", "foundation year 2", "ctf",
    "clinical teaching fellow", "cdf", "clinical development fellow",
    "clinical research fellow", "clinical fellow", "sho", "senior house officer",
    "trust grade doctor", "trust doctor", "locally employed doctor", "led",
    "junior doctor", "ct1 equivalent", "fy2 equivalent", "lat", "las"
]
def title_allowed(title: str) -> bool:
    tl = title.lower()
    return any(ft in tl for ft in FILTER_TITLES)

# --- Database ---
conn = sqlite3.connect("jobs.db", check_same_thread=False)
c = conn.cursor()
c.execute("""
  CREATE TABLE IF NOT EXISTS posted_jobs (
    site    TEXT,
    job_id  TEXT,
    PRIMARY KEY(site, job_id)
  )
""")
conn.commit()
def is_new(site, job_id):
    c.execute("SELECT 1 FROM posted_jobs WHERE site=? AND job_id=?", (site, job_id))
    return c.fetchone() is None
def mark_posted(site, job_id):
    c.execute("INSERT OR IGNORE INTO posted_jobs(site, job_id) VALUES(?,?)", (site, job_id))
    conn.commit()

# --- Keywords for all sites ---
NHSE_KEYWORDS = [
    "Trust Grade FY2 Doctor", "SHO (Senior House Officer)", "Junior Clinical Fellow",
    "Clinical Fellow (ST1/ST2 Level)", "Clinical Fellow", "Junior Doctor",
    "Foundation Year 2 Equivalent", "Locally Employed Doctor (LED)",
    "Clinical Teaching Fellow", "Trust Doctor", "LAT", "LAS", "LED"
]
HJUK_KEYWORDS = [
    "SHO", "Senior House Officer", "Junior Clinical Fellow", "Clinical Fellow",
    "Clinical Research Fellow", "Junior Doctor", "Locally Employed Doctor",
    "Clinical Teaching Fellow", "Trust Grade Doctor", "Trust Doctor", "LAT", "LAS", "LED"
]
SCOTLAND_KEYWORDS = [
    "Clinical Dev Fellow (FHO1)", "Clinical Dev Fellow (FHO2)", "Clinical Development Fellow",
    "Clinical Fellow", "Clinical Teaching Fellow", "LAS-FY2", "SHO",
    "Clinical Development Fellow", "LAT", "LAS", "LED"
]
NHSJOBS_KEYWORDS = [
    "SHO", "Senior House Officer", "Junior Clinical Fellow", "Clinical Fellow",
    "Clinical Research Fellow", "Junior Doctor", "Locally Employed Doctor",
    "Clinical Teaching Fellow", "Trust Grade Doctor", "Trust Doctor", "LAT", "LAS", "LED"
]

HEADERS = {"User-Agent": "Mozilla/5.0"}

# --- Fetchers ---

def fetch_nhs_england():
    jobs = []
    seen_links = set()
    for keyword in NHSE_KEYWORDS:
        for page in range(1, 3):  # pages 1 and 2 only
            url = (
                "https://www.jobs.nhs.uk/candidate/search/results?"
                f"keyword={keyword.replace(' ', '%20')}&page={page}"
            )
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code != 200:
                break
            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select('li[data-test="search-result"]')
            if not cards:
                break
            for card in cards:
                a = card.select_one('a[data-test="search-result-job-title"]')
                loc_div = card.select_one('div[data-test="search-result-location"]')
                if not a:
                    continue
                title = a.get_text(strip=True)
                if not title_allowed(title):
                    continue
                href = a["href"]
                link = href if href.startswith("http") else "https://www.jobs.nhs.uk" + href
                # Deduplication (across keywords/pages)
                if link in seen_links:
                    continue
                seen_links.add(link)
                jid = hashlib.sha256(link.encode()).hexdigest()
                # Date posted
                date_li = card.select_one('li[data-test="search-result-publicationDate"] strong')
                date = date_li.get_text(strip=True) if date_li else ""
                jobs.append({
                    "site": "nhs_england",
                    "id": jid,
                    "title": title,
                    "location": loc_div.get_text(strip=True) if loc_div else "",
                    "pay": "",  # Not available on this search page
                    "link": link,
                    "date": date
                })
    return jobs

def fetch_healthjobsuk():
    jobs = []
    seen_links = set()
    for keyword in HJUK_KEYWORDS:
        for page in range(1, 3):  # pages 1 and 2 only
            url = (
                "https://www.healthjobsuk.com/job_list?"
                f"JobSearch_q={keyword.replace(' ', '+')}&JobSearch_d=&JobSearch_g=&"
                "JobSearch_re=_POST&JobSearch_re_0=1&JobSearch_re_1=1-_-_-&"
                "JobSearch_re_2=1-_-_--_-_-&JobSearch_Submit=Search&_tr=JobSearch&_ts=863"
                f"&_pg={page}&_pgid="
            )
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code != 200:
                break
            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select("a.clearfix")
            if not cards:
                break
            for a_tag in cards:
                td = a_tag.select_one("div.hj-jobtitle.hj-job-detail")
                if not td:
                    continue
                title = td.get_text(strip=True)
                if not title_allowed(title):
                    continue
                href = a_tag["href"]
                link = href if href.startswith("http") else "https://www.healthjobsuk.com" + href
                if link in seen_links:
                    continue
                seen_links.add(link)
                jid = hashlib.sha256(link.encode()).hexdigest()
                # Location
                loc_div = a_tag.select_one("div.hj-locationtown.hj-job-detail")
                loc = loc_div.get_text(strip=True) if loc_div else ""
                # Pay/Salary
                pay_div = a_tag.select_one("div.hj-salary.hj-job-detail")
                pay = pay_div.get_text(strip=True) if pay_div else ""
                # Date (if available)
                date_div = a_tag.select_one("div.hj-job-date")
                date = date_div.get_text(strip=True) if date_div else ""
                jobs.append({
                    "site": "healthjobsuk",
                    "id": jid,
                    "title": title,
                    "location": loc,
                    "pay": pay,
                    "link": link,
                    "date": date
                })
    return jobs

def fetch_nhs_scotland():
    jobs = []
    seen_links = set()
    for keyword in SCOTLAND_KEYWORDS:
        for page in range(1, 3):
            url = (
                "https://apply.jobs.scot.nhs.uk/Home/Job?"
                f"JobSearch_q={keyword.replace(' ', '+')}&page={page}"
            )
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code != 200:
                break
            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select("div.job-row.details")
            if not cards:
                break
            for card in cards:
                a = card.select_one("a.mb-15[data-testid^='a-job-detail']")
                if not a:
                    continue
                title = a.get_text(strip=True)
                if not title_allowed(title):
                    continue
                href = a["href"]
                link = href if href.startswith("http") else "https://apply.jobs.scot.nhs.uk" + href
                if link in seen_links:
                    continue
                seen_links.add(link)
                jid = hashlib.sha256(link.encode()).hexdigest()
                # Location (optional: check for div if exists, else blank)
                loc_div = card.select_one("div.hj-locationtown.hj-job-detail")
                loc = loc_div.get_text(strip=True) if loc_div else ""
                # Pay/Salary (optional: check for div if exists, else blank)
                pay_div = card.select_one("div.hj-salary.hj-job-detail")
                pay = pay_div.get_text(strip=True) if pay_div else ""
                # Date not directly available on main page, usually
                jobs.append({
                    "site": "nhs_scotland",
                    "id": jid,
                    "title": title,
                    "location": loc,
                    "pay": pay,
                    "link": link,
                    "date": ""
                })
    return jobs

def fetch_nhsjobs():
    jobs = []
    seen_links = set()
    for keyword in NHSJOBS_KEYWORDS:
        for page in range(1, 6):  # up to 5 pages for NHS Jobs
            url = (
                "https://www.nhsjobs.com/job_list?"
                f"JobSearch_q={keyword.replace(' ', '+')}&JobSearch_d=&JobSearch_g=&"
                "JobSearch_re=_POST&JobSearch_re_0=1&JobSearch_re_1=1-_-_-&"
                "JobSearch_re_2=1-_-_--_-_-&JobSearch_Submit=Search&_tr=JobSearch&_ts=300267"
                f"&_pg={page}&_pgid="
            )
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code != 200:
                break
            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select("a.clearfix")
            if not cards:
                break
            for a_tag in cards:
                td = a_tag.select_one("div.hj-jobtitle.hj-job-detail")
                if not td:
                    continue
                title = td.get_text(strip=True)
                if not title_allowed(title):
                    continue
                href = a_tag["href"]
                link = href if href.startswith("http") else "https://www.nhsjobs.com" + href
                if link in seen_links:
                    continue
                seen_links.add(link)
                jid = hashlib.sha256(link.encode()).hexdigest()
                # Location
                loc_div = a_tag.select_one("div.hj-locationtown.hj-job-detail")
                loc = loc_div.get_text(strip=True) if loc_div else ""
                # Pay/Salary
                pay_div = a_tag.select_one("div.hj-salary.hj-job-detail")
                pay = pay_div.get_text(strip=True) if pay_div else ""
                # Date (if available)
                date_div = a_tag.select_one("div.hj-job-date")
                date = date_div.get_text(strip=True) if date_div else ""
                jobs.append({
                    "site": "nhsjobs",
                    "id": jid,
                    "title": title,
                    "location": loc,
                    "pay": pay,
                    "link": link,
                    "date": date
                })
    return jobs

# --- FETCHERS LIST ---
FETCHERS = [
    fetch_nhs_england,
    fetch_healthjobsuk,
    fetch_nhs_scotland,
    fetch_nhsjobs
]

# --- Main loop ---
def check_and_post():
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] Starting check_and_post()")
    for fetch in FETCHERS:
        for job in fetch():
            # Filter by posting date (if date is available)
            if job["date"]:
                try:
                    post_date = datetime.datetime.strptime(job["date"], "%d %B %Y")
                    if (datetime.datetime.utcnow().date() - post_date.date()).days > 3:
                        continue
                except Exception:
                    continue
            # No duplicate alerts
            if is_new(job["site"], job["id"]):
                msg = (
                    f"*{job['title']}*\n"
                    f"Location: `{job.get('location','')}`\n"
                    f"Pay: `{job.get('pay','')}`\n"
                    f"Posted: `{job.get('date','')}`\n"
                    f"[Apply here]({job['link']})"
                )
                send_message(msg)
                mark_posted(job["site"], job["id"])
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] Finished check_and_post()\n")

# --- Scheduler & health endpoint ---
app = Flask(__name__)
@app.route("/", methods=["GET", "HEAD"])
def home():
    print("✅ Ping received from UptimeRobot or any client")
    return "✅ Bot is alive", 200

if __name__ == "__main__":
    check_and_post()  # initial run
    send_message("✅ Bot started successfully")
    sched = BackgroundScheduler(timezone=pytz.utc)
    sched.add_job(check_and_post, "interval", minutes=5)
    sched.start()
    app.run(host="0.0.0.0", port=3000)