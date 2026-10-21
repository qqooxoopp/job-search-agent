"""
第 4 週：把結果寫進 Google Sheet。

Sheet 裡會有兩個分頁（沒有的話程式會自動建立）：
  - Jobs：通過篩選的職缺和 ATS 分析結果
  - Rejected：被篩選規則排除的職缺和原因

用 LinkedIn 連結去重：已經在 Sheet 裡的職缺不會再寫一次，
所以你在 Sheet 上手動填的「狀態」「備註」不會被覆蓋。
"""

import json
import os
from datetime import date

import gspread
from dotenv import load_dotenv

load_dotenv()
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SERVICE_ACCOUNT_FILE = "service_account.json"  # 從 Google Cloud 下載的金鑰

JOBS_TAB = "Jobs"
JOBS_HEADERS = [
    "日期", "職稱", "公司", "地點", "薪資", "工作型態", "ATS 分數", "建議", "理由",
    "硬傷", "缺少的關鍵字", "符合的關鍵字", "產業", "JD 來源", "LinkedIn 連結",
    "狀態", "備註",  # 留給你自己填，例如「已投遞」「面試中」「不投」
    "手動 JD",  # 「⚠️ 手動確認」的職缺，把 JD 貼在這格，下次執行就會分析
]
USER_COLUMNS = 3  # 最後 3 欄是你自己填的，程式不會覆蓋

REJECTED_TAB = "Rejected"
REJECTED_HEADERS = ["日期", "職稱", "公司", "地點", "薪資", "排除原因", "LinkedIn 連結"]

VERDICT_LABELS = {"apply": "🟢 投遞", "stretch": "🟡 可試", "skip": "🔴 略過", "manual": "⚠️ 手動確認"}


def connect():
    """登入 Google，打開你的 Sheet。"""
    if not GOOGLE_SHEET_ID:
        raise SystemExit("找不到 GOOGLE_SHEET_ID，請加到 .env 檔裡。")

    # 第 5 週在 GitHub Actions 上執行時，金鑰會放在環境變數裡，而不是檔案
    key_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if key_json:
        client = gspread.service_account_from_dict(json.loads(key_json))
    else:
        client = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
    return client.open_by_key(GOOGLE_SHEET_ID)


def get_tab(sheet, title, headers):
    """取得分頁；不存在就建立，並寫好標題列。"""
    try:
        tab = sheet.worksheet(title)
    except gspread.WorksheetNotFound:
        tab = sheet.add_worksheet(title=title, rows=1000, cols=len(headers))

    current = tab.row_values(1)
    if not current:
        tab.append_row(headers)
        tab.format("1:1", {"textFormat": {"bold": True}})
        tab.freeze(rows=1)
    elif current != headers and headers[: len(current)] == current:
        # 舊版的 Sheet 少了新加的欄位（例如「手動 JD」）→ 補上標題，原本的資料不動
        if tab.col_count < len(headers):
            tab.add_cols(len(headers) - tab.col_count)
        tab.update(values=[headers], range_name="A1")
    return tab


def read_sheet_state():
    """
    讀出 Jobs 分頁已經有的職缺，給分析步驟判斷哪些要跳過。
    回傳 {LinkedIn 連結: {"done": 是否已有完整分析, "manual_jd": 你貼的 JD}}
    """
    tab = get_tab(connect(), JOBS_TAB, JOBS_HEADERS)
    url_i = JOBS_HEADERS.index("LinkedIn 連結")
    verdict_i = JOBS_HEADERS.index("建議")
    jd_i = JOBS_HEADERS.index("手動 JD")

    state = {}
    for row in tab.get_all_values()[1:]:  # [1:] 跳過標題列
        row = row + [""] * (len(JOBS_HEADERS) - len(row))  # 後面空白的格子可能沒有回傳
        if row[url_i]:
            state[row[url_i]] = {
                "done": row[verdict_i] != VERDICT_LABELS["manual"],
                "manual_jd": row[jd_i].strip(),
            }
    return state


def existing_urls(tab, headers):
    """讀出這個分頁已經有的 LinkedIn 連結，用來去重。"""
    column = headers.index("LinkedIn 連結") + 1
    return set(tab.col_values(column)[1:])  # [1:] 跳過標題列


def append_new_rows(tab, headers, rows):
    """只寫入還沒出現過的職缺，回傳新增了幾列。"""
    seen = existing_urls(tab, headers)
    url_index = headers.index("LinkedIn 連結")
    new_rows = []
    for row in rows:
        if row[url_index] not in seen:
            new_rows.append(row)
            seen.add(row[url_index])  # 同一批裡重複的也只寫一次
    if new_rows:
        # RAW：所有內容都當成純文字寫入，不會被當成公式執行。
        # 分析結果有一部分來自網路上的 JD，用 RAW 可以避免奇怪的內容變成公式。
        tab.append_rows(new_rows, value_input_option="RAW")
    return len(new_rows)


def update_resolved_rows(tab, results):
    """
    之前標成「手動確認」的職缺，如果這次有了完整分析，就原地更新那一列。
    最後三欄「狀態」「備註」「手動 JD」是你自己填的，不會被覆蓋。回傳更新了幾列。
    """
    urls = tab.col_values(JOBS_HEADERS.index("LinkedIn 連結") + 1)
    verdicts = tab.col_values(JOBS_HEADERS.index("建議") + 1)
    editable_columns = len(JOBS_HEADERS) - USER_COLUMNS
    last_column = gspread.utils.rowcol_to_a1(1, editable_columns).rstrip("1")

    updates = []
    for result in results:
        if not result.get("jd_found") or result["url"] not in urls:
            continue
        index = urls.index(result["url"])  # 從 0 開始，0 是標題列
        if index < len(verdicts) and verdicts[index] == VERDICT_LABELS["manual"]:
            row_number = index + 1
            updates.append({
                "range": f"A{row_number}:{last_column}{row_number}",
                "values": [job_row(result)[:editable_columns]],
            })

    if updates:
        tab.batch_update(updates, value_input_option="RAW")
    return len(updates)


def job_row(result):
    """把一筆分析結果轉成 Jobs 分頁的一列。"""
    today = date.today().isoformat()
    if not result.get("jd_found"):
        return [
            today, result["title"], result["company"], result["location"], "", "", "",
            VERDICT_LABELS["manual"], result["reason"], "", "", "", "", "",
            result["url"], *[""] * USER_COLUMNS,
        ]
    return [
        today,
        result["title"],
        result["company"],
        result["location"],
        result["salary"],
        result["work_arrangement"],
        result["ats_score"],
        VERDICT_LABELS[result["verdict"]],
        result["reason"],
        "; ".join(result["dealbreakers"]),
        ", ".join(result["missing_keywords"]),
        ", ".join(result["matched_keywords"]),
        result["industry"],
        result.get("jd_url") or result.get("jd_source", ""),  # 手動 JD 沒有網址，就寫「手動提供」
        result["url"],
        *[""] * USER_COLUMNS,  # 狀態、備註、手動 JD 留白給你填
    ]


def rejected_row(job, reason):
    return [
        date.today().isoformat(), job["title"], job["company"], job["location"],
        job["salary"], reason, job["url"],
    ]


def write_to_sheet(results, rejected):
    """把分析結果和排除紀錄寫進 Sheet。回傳 (新增的職缺數, 更新的職缺數, 新增的排除數, 網址)。"""
    sheet = connect()
    jobs_tab = get_tab(sheet, JOBS_TAB, JOBS_HEADERS)
    rejected_tab = get_tab(sheet, REJECTED_TAB, REJECTED_HEADERS)

    # 分數高的排前面，方便你一打開就看到最值得投的
    results = sorted(results, key=lambda r: r.get("ats_score") or -1, reverse=True)

    updated_jobs = update_resolved_rows(jobs_tab, results)
    added_jobs = append_new_rows(jobs_tab, JOBS_HEADERS, [job_row(r) for r in results])
    added_rejected = append_new_rows(
        rejected_tab, REJECTED_HEADERS, [rejected_row(j, why) for j, why in rejected]
    )
    return added_jobs, updated_jobs, added_rejected, sheet.url
