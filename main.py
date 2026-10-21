"""
完整流程：讀信 → 篩選 → ATS 分析 → 寫入 Google Sheet。
GitHub Actions 每天早上 8 點（多倫多時間）跑的就是這支程式。

用法：
    python main.py        # 讀最近 1 天的信
    python main.py 14     # 讀最近 14 天的信
"""

import sys

from ats_analyzer import analyze_jobs, load_cv, print_usage
from filter_jobs import filter_jobs, load_rules, log_rejected
from read_alerts import collect_jobs
from sheets_writer import read_sheet_state, write_to_sheet


def main():
    days_back = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    cv = load_cv()
    rules = load_rules()

    # 1. 讀信
    jobs = list(collect_jobs(days_back).values())

    # 2. 篩選
    kept, rejected = filter_jobs(jobs, rules)
    log_rejected(rejected)  # 本機也留一份備份（在雲端執行時這個檔案會被丟掉，沒關係）
    print(f"篩選後留下 {len(kept)} 個職缺，排除 {len(rejected)} 個\n")

    # 3. ATS 分析：先讀 Sheet，已經分析過的不重複花錢
    sheet_state = read_sheet_state()
    results = analyze_jobs(kept, cv, rules, sheet_state)

    # 4. 寫入 Google Sheet
    added_jobs, updated_jobs, added_rejected, url = write_to_sheet(results, rejected)
    print(
        f"\n📊 Google Sheet 新增 {added_jobs} 個職缺、更新 {updated_jobs} 個職缺、"
        f"新增 {added_rejected} 筆排除紀錄"
    )
    print(f"   {url}\n")

    print_usage()


if __name__ == "__main__":
    main()
