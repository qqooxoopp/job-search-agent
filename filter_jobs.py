"""
第 2 週：依照 filters.yaml 的規則篩選職缺。

用法：
    python filter_jobs.py        # 讀最近 1 天的信並篩選
    python filter_jobs.py 14     # 讀最近 14 天的信並篩選
"""

import csv
import os
import re
import sys
from datetime import date

import yaml

from read_alerts import collect_jobs, print_job

RULES_FILE = "filters.yaml"
REJECTED_LOG = "rejected_jobs.csv"  # 被排除職缺的紀錄，第 4 週會改寫進 Google Sheet
REJECTED_COLUMNS = ["date", "title", "company", "location", "salary", "reason", "url"]


def load_rules():
    with open(RULES_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)


def contains_any(text, keywords):
    """text 裡有沒有包含 keywords 其中一個（不分大小寫）。有的話回傳那個字。"""
    text = text.lower()
    for keyword in keywords:
        if keyword.lower() in text:
            return keyword
    return None


def parse_max_salary(salary_text):
    """
    從薪資文字抓出最高的年薪數字。
    例如 'CA$110K/yr - CA$130K/yr' → 130000，'$45/hr' → 93600（以一年 2080 小時換算）。
    抓不到數字就回傳 None。
    """
    numbers = []
    for amount, k in re.findall(r"\$\s?([\d,]+(?:\.\d+)?)\s*([Kk])?", salary_text):
        value = float(amount.replace(",", ""))
        if k:
            value *= 1000
        numbers.append(value)
    if not numbers:
        return None

    highest = max(numbers)
    if "/hr" in salary_text.lower() or "hour" in salary_text.lower():
        highest *= 2080
    return highest


def check_job(job, rules):
    """檢查一個職缺。符合就回傳 None，不符合就回傳排除原因。"""
    title = job["title"]

    # 規則 1：職稱要包含想要的關鍵字
    if not contains_any(title, rules["title_include"]):
        return "職稱不符"

    # 規則 2：職稱或公司不能包含排除的字
    hit = contains_any(f"{title} {job['company']}", rules["exclude_keywords"])
    if hit:
        return f"包含排除字：{hit}"

    # 規則 3：地點
    location_text = f"{job['location']} {' '.join(job['tags'])}"
    in_city = contains_any(job["location"], rules["locations"])
    remote_canada = (
        rules.get("allow_remote_canada")
        and "remote" in location_text.lower()
        and "canada" in location_text.lower()
    )
    if not (in_city or remote_canada):
        return f"地點不符：{job['location'] or '未知'}"

    # 規則 4：薪資（只在有列薪資時檢查）
    if job["salary"]:
        max_salary = parse_max_salary(job["salary"])
        if max_salary is not None and max_salary < rules["min_salary"]:
            return f"薪資太低：{job['salary']}"

    return None


def filter_jobs(jobs, rules):
    """把職缺分成留下和排除兩組。排除的會附上原因，方便檢查規則有沒有誤殺。"""
    kept, rejected = [], []
    for job in jobs:
        reason = check_job(job, rules)
        if reason:
            rejected.append((job, reason))
        else:
            kept.append(job)
    return kept, rejected


def log_rejected(rejected):
    """
    把被排除的職缺加到 rejected_jobs.csv 的最後面。
    已經記錄過的職缺（同一個網址）不會重複寫入。回傳這次新增了幾筆。
    """
    logged_urls = set()
    if os.path.exists(REJECTED_LOG):
        with open(REJECTED_LOG, encoding="utf-8-sig", newline="") as f:
            logged_urls = {row["url"] for row in csv.DictReader(f)}

    new_rows = [
        {
            "date": date.today().isoformat(),
            "title": job["title"],
            "company": job["company"],
            "location": job["location"],
            "salary": job["salary"],
            "reason": reason,
            "url": job["url"],
        }
        for job, reason in rejected
        if job["url"] not in logged_urls
    ]

    is_new_file = not os.path.exists(REJECTED_LOG)
    # utf-8-sig 讓 Excel 和 Numbers 打開時中文不會變亂碼
    with open(REJECTED_LOG, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=REJECTED_COLUMNS)
        if is_new_file:
            writer.writeheader()
        writer.writerows(new_rows)

    return len(new_rows)


def main():
    days_back = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    rules = load_rules()
    jobs = list(collect_jobs(days_back).values())

    kept, rejected = filter_jobs(jobs, rules)

    print(f"\n✅ 留下 {len(kept)} 個職缺：\n")
    for job in kept:
        print_job(job)
        print()

    print(f"❌ 排除 {len(rejected)} 個職缺：\n")
    for job, reason in rejected:
        print(f"{job['title']}｜{job['company']}　→ {reason}")
        print(f"   {job['url']}")

    added = log_rejected(rejected)
    print(f"\n已把 {added} 筆新的排除紀錄加到 {REJECTED_LOG}")


if __name__ == "__main__":
    main()
