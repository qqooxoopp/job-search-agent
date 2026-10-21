"""
讀取 Gmail 裡 LinkedIn 寄來的職缺信，抽出每個職缺的職稱、公司、地點和標籤。

用法：
    python read_alerts.py        # 讀最近 1 天的信
    python read_alerts.py 7      # 讀最近 7 天的信
"""

import email
import imaplib
import os
import re
import sys
from datetime import datetime, timedelta
from email.header import decode_header
from html.parser import HTMLParser

from dotenv import load_dotenv

# ---------- 設定 ----------
load_dotenv()  # 從 .env 檔讀取帳號和 App Password，不寫死在程式裡
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

# LinkedIn 的職缺信可能來自不同寄件者（確認信、每日 alert、推薦職缺），
# 所以搜尋所有 linkedin.com 寄來的信，再只保留裡面有職缺連結的
LINKEDIN_SENDER = "linkedin.com"
SAMPLE_FILE = "sample_alert.html"  # 存一封信的原始 HTML，之後用來調整解析方式

# 職缺連結的樣子：.../jobs/view/1234567890/...
JOB_ID_PATTERN = re.compile(r"/jobs/view/(\d+)")

# 看到這些文字代表職缺區塊結束，後面是頁尾
STOP_TEXTS = ("See all jobs", "This email was intended for")
SALARY_PATTERN = re.compile(r"\$\s?\d")  # 例如 $100K、CA$95,000
SKIP_TAGS = {"style", "script", "head", "title"}  # 這些標籤裡的文字不是內容
MAX_DETAIL_LINES = 4  # 職稱後面最多讀幾行細節，避免讀到不相關的文字


# ---------- 工具函式 ----------
def decode_text(value):
    """把信件標題這類編碼過的文字轉成一般文字。"""
    if not value:
        return ""
    parts = []
    for text, charset in decode_header(value):
        if isinstance(text, bytes):
            parts.append(text.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(text)
    return " ".join("".join(parts).split())  # 把換行和多餘空白合併成單一空格


def get_html_body(msg):
    """從一封信裡找出 HTML 內容。"""
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            charset = part.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


class AlertParser(HTMLParser):
    """
    依照文字出現的順序掃過信件。LinkedIn 的格式是：
        [職缺連結] 職稱
        公司 · 地點
        標籤（Actively recruiting、Easy Apply、薪資等，選填）
    遇到下一個職缺連結就開始新職缺，遇到頁尾文字就停止。
    """

    def __init__(self):
        super().__init__()
        self.jobs = {}  # job_id -> 職缺資料，dict 會保留加入的順序
        self.link_job_id = None  # 目前所在的 <a> 如果是職缺連結，記住它的 id
        self.current = None  # 目前正在收集細節的職缺
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip_depth += 1
        if tag == "a":
            match = JOB_ID_PATTERN.search(dict(attrs).get("href", ""))
            self.link_job_id = match.group(1) if match else None

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        if tag == "a":
            self.link_job_id = None

    def handle_data(self, data):
        text = " ".join(data.split())
        if not text or self.skip_depth:
            return

        # 情況 1：職缺連結上的文字 → 職稱
        if self.link_job_id:
            job = self.jobs.setdefault(self.link_job_id, new_job(self.link_job_id))
            if not job["title"]:
                job["title"] = text
            self.current = job
            return

        # 情況 2：頁尾 → 停止收集
        if any(text.startswith(stop) for stop in STOP_TEXTS):
            self.current = None
            return

        # 情況 3：職稱後面的細節
        job = self.current
        if job is None or job["_detail_lines"] >= MAX_DETAIL_LINES:
            return
        job["_detail_lines"] += 1

        if not job["company"] and "·" in text:
            company, _, location = text.partition("·")
            job["company"] = company.strip()
            job["location"] = location.strip()
        elif SALARY_PATTERN.search(text):
            job["salary"] = text
        else:
            job["tags"].append(text)


def new_job(job_id):
    return {
        "id": job_id,
        "url": f"https://www.linkedin.com/jobs/view/{job_id}/",  # 去掉追蹤參數的乾淨網址
        "title": "",
        "company": "",
        "location": "",
        "salary": "",
        "tags": [],
        "_detail_lines": 0,  # 內部計數用，底線開頭代表不對外輸出
    }


def extract_jobs(html):
    """回傳這封信裡的職缺清單。"""
    parser = AlertParser()
    parser.feed(html)
    jobs = []
    for job in parser.jobs.values():
        job.pop("_detail_lines")
        jobs.append(job)
    return jobs


# ---------- 主流程 ----------
def fetch_linkedin_emails(days_back):
    """連上 Gmail，找出最近幾天 LinkedIn 寄來的信。"""
    since = (datetime.now() - timedelta(days=days_back)).strftime("%d-%b-%Y")

    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
    mail.select("INBOX")  # 注意：只讀收件匣，不含垃圾郵件匣

    status, data = mail.search(None, f'(FROM "{LINKEDIN_SENDER}" SINCE {since})')
    message_ids = data[0].split() if status == "OK" else []

    messages = []
    for msg_id in message_ids:
        status, msg_data = mail.fetch(msg_id, "(RFC822)")
        if status == "OK":
            messages.append(email.message_from_bytes(msg_data[0][1]))

    mail.logout()
    return messages


def collect_jobs(days_back):
    """讀信、抽職缺、去重，回傳 {job_id: job}。其他程式（例如篩選）會呼叫這個函式。"""
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        print("找不到帳號設定。請確認 .env 檔裡有 GMAIL_ADDRESS 和 GMAIL_APP_PASSWORD。")
        sys.exit(1)

    print(f"讀取最近 {days_back} 天 LinkedIn 寄來的信...\n")
    messages = fetch_linkedin_emails(days_back)

    all_jobs = {}
    sample_saved = False
    for msg in messages:
        html = get_html_body(msg)
        jobs = extract_jobs(html)
        if not jobs:
            continue  # 沒有職缺的 LinkedIn 信（通知、訊息等）直接跳過

        sender = decode_text(msg.get("From"))
        subject = decode_text(msg.get("Subject"))
        print(f"📧 {subject}\n   寄件者：{sender}｜{msg.get('Date')}｜{len(jobs)} 個職缺")

        # 把第一封有職缺的信存下來，之後需要時用來檢查格式
        if not sample_saved:
            with open(SAMPLE_FILE, "w", encoding="utf-8") as f:
                f.write(html)
            sample_saved = True

        for job in jobs:
            all_jobs[job["id"]] = job  # 用 job id 去重，同一職缺出現在多封信只算一次

    return all_jobs


def print_job(job):
    print(f"{job['title']}")
    print(f"   公司：{job['company'] or '—'}｜地點：{job['location'] or '—'}")
    if job["salary"]:
        print(f"   薪資：{job['salary']}")
    if job["tags"]:
        print(f"   標籤：{', '.join(job['tags'])}")
    print(f"   {job['url']}")


def main():
    days_back = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    all_jobs = collect_jobs(days_back)

    if not all_jobs:
        print("沒有找到含職缺的 LinkedIn 信。可以試試 `python read_alerts.py 7`，或檢查垃圾郵件匣。")
        return

    print(f"\n共 {len(all_jobs)} 個不重複的職缺：\n")
    for job in all_jobs.values():
        print_job(job)
        print()


if __name__ == "__main__":
    main()
