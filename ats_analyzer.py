"""
第 3 週：用 Claude API 做 ATS 分析。

流程（每個通過篩選的職缺）：
  步驟 1：找 JD。如果 jds/ 資料夾有手動存的 JD 就直接用；
          沒有的話，讓 Claude 用網路搜尋找到職缺並整理重點。
  步驟 2：分析。把你的履歷 (cv.txt) 和 JD 給 Claude，
          用固定的資料格式回傳 ATS 分數、符合和缺少的關鍵字、硬傷、建議。

分析過的職缺會存進 analysis_cache.json，下次不會重複分析（省錢）。

用法：
    python ats_analyzer.py        # 讀最近 1 天的信 → 篩選 → 分析
    python ats_analyzer.py 14     # 讀最近 14 天
"""

import json
import os
import sys
from datetime import date
from typing import Literal

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel

from filter_jobs import filter_jobs, load_rules, log_rejected
from read_alerts import collect_jobs

load_dotenv()  # .env 裡的 ANTHROPIC_API_KEY 會被 SDK 自動讀取

# ---------- 設定 ----------
CV_FILE = "cv.txt"  # 你的 master 履歷，純文字
MANUAL_JD_DIR = "jds"  # 手動 JD：存成 jds/<職缺ID>.txt，就會跳過網路搜尋
CACHE_FILE = "analysis_cache.json"

MODEL = "claude-sonnet-5-5"  # 速度、品質、價格比較平衡的模型
MAX_SEARCHES_PER_JOB = 3  # 每個職缺最多搜尋幾次，控制成本

# 用來估算花費（美元），價格來自 Anthropic 官方價目表
PRICE_INPUT_PER_MTOK = 2.00
PRICE_OUTPUT_PER_MTOK = 10.00
PRICE_PER_SEARCH = 0.01

JD_NOT_FOUND = "JD_NOT_FOUND"

client = anthropic.Anthropic()
usage_totals = {"input": 0, "output": 0, "searches": 0}


# ---------- 分析結果的資料格式 ----------
class ATSResult(BaseModel):
    """步驟 2 回傳的固定格式。Claude 的回答一定會符合這個結構。"""

    ats_score: int  # 0–100
    verdict: Literal["apply", "stretch", "skip"]
    reason: str  # 一兩句話說明建議，繁體中文
    matched_keywords: list[str]
    missing_keywords: list[str]
    dealbreakers: list[str]  # 不符合的硬性要求
    industry: str
    is_agency_or_advertising: bool
    salary: str  # JD 有寫才填，沒寫就空字串
    work_arrangement: str  # Remote / Hybrid（幾天）/ Onsite / 未知


# ---------- 工具函式 ----------
def track_usage(response):
    """累計 token 和搜尋次數，最後估算花費。"""
    usage_totals["input"] += response.usage.input_tokens
    usage_totals["output"] += response.usage.output_tokens
    server_tools = getattr(response.usage, "server_tool_use", None)
    if server_tools:
        usage_totals["searches"] += server_tools.web_search_requests or 0


def estimated_cost():
    return (
        usage_totals["input"] / 1_000_000 * PRICE_INPUT_PER_MTOK
        + usage_totals["output"] / 1_000_000 * PRICE_OUTPUT_PER_MTOK
        + usage_totals["searches"] * PRICE_PER_SEARCH
    )


def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


# ---------- 步驟 1：找 JD ----------
def find_jd_online(job):
    """讓 Claude 用網路搜尋找到職缺，整理成重點。找不到就回傳 JD_NOT_FOUND 開頭的文字。"""
    prompt = f"""Today's date is {date.today().strftime('%B %d, %Y')}.
Find the current job posting for this role and summarize it.

Title: {job['title']}
Company: {job['company']}
Location: {job['location']}
LinkedIn posting: {job['url']}

Search the company's careers page or job boards (Greenhouse, Lever, Ashby, Workday, Indeed, Glassdoor, etc.).
Prefer, in order: (1) the company's own careers site or ATS (Greenhouse, Lever, Ashby, Workday),
(2) a single job posting page on a job board. Do NOT use search-results or listing pages that show many jobs.
Make sure the posting you use matches the same company, title and location.

Then reply in this format:
SOURCE_URL: <only the URL, nothing else on this line>
SOURCE_TYPE: <company site / job board / reposting site>
NOTES: <anything uncertain, e.g. could not confirm it is the same posting, posting may be closed, details missing>
COMPANY & INDUSTRY: <what the company does>
LOCATION & WORK ARRANGEMENT: <remote / hybrid with days in office / onsite>
SALARY: <as stated, or "not stated">
RESPONSIBILITIES: <bullet list>
REQUIRED QUALIFICATIONS: <bullet list, keep years of experience, domains, certifications, tools exactly as stated>
PREFERRED QUALIFICATIONS: <bullet list>

Summarize faithfully. Do not invent requirements.
If you cannot find this exact posting with confidence, reply only: {JD_NOT_FOUND}: <one-line reason>"""

    messages = [{"role": "user", "content": prompt}]
    tools = [
        {
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": MAX_SEARCHES_PER_JOB,
            "user_location": {"type": "approximate", "country": "CA", "region": "Ontario"},
        }
    ]

    # 搜尋時間比較長時，API 可能先暫停（pause_turn），把回覆送回去就會繼續
    for _ in range(5):
        response = client.messages.create(
            model=MODEL, max_tokens=8000, messages=messages, tools=tools
        )
        track_usage(response)
        if response.stop_reason != "pause_turn":
            break
        messages = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": response.content},
        ]

    # 把最後回覆裡的文字段落接起來
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    # 只保留 SOURCE_URL 或 JD_NOT_FOUND 之後的部分，去掉「我來搜尋看看」這類開場白
    for marker in ("SOURCE_URL:", JD_NOT_FOUND):
        if marker in text:
            return text[text.index(marker):]
    return text


def manual_jd_path(job):
    return os.path.join(MANUAL_JD_DIR, f"{job['id']}.txt")


def get_jd(job):
    """先找手動提供的 JD（Sheet 的「手動 JD」欄，或 jds/ 資料夾），沒有再上網搜尋。回傳 (JD 文字, 來源說明)。"""
    if job.get("manual_jd"):
        return job["manual_jd"], "手動提供"
    manual_path = manual_jd_path(job)
    if os.path.exists(manual_path):
        with open(manual_path, encoding="utf-8") as f:
            return f.read(), "手動提供"
    return find_jd_online(job), "網路搜尋"


# ---------- 步驟 2：ATS 分析 ----------
def analyze(job, cv, jd_text, rules):
    """比對履歷和 JD，回傳 ATSResult。"""
    system = f"""You are an honest ATS (applicant tracking system) evaluator and career advisor.

Today's date is {date.today().strftime('%B %d, %Y')}. Any date in the CV on or before today is in the past:
a certification or degree dated on or before today has already been earned, not "expected".
The CV may contain LaTeX markup; ignore the markup and read the content.

Rules:
- Score only what the CV actually shows. Never credit skills, titles or domains the CV does not contain.
- ats_score (0-100) estimates keyword and requirement match the way an ATS and a recruiter screen would. Be strict.
- dealbreakers: hard requirements the candidate clearly does not meet (e.g. required years in a specific role,
  mandatory domain experience, required language, location/relocation, required certification).
- verdict: "apply" = realistic fit; "stretch" = possible but with real gaps; "skip" = dealbreakers or poor fit.
- The candidate does NOT want agency or advertising roles. Salary is acceptable if the TOP of the stated range
  is at least {rules['min_salary']:,} CAD; do not penalize a low bottom of the range. Factor these into the verdict.
- If the job description says details are missing, the source is unreliable, or the posting may be closed,
  mention that in "reason" so the candidate knows to double-check.
- Keywords stay in English as they appear in the JD. Write "reason" in Traditional Chinese, 1–2 sentences."""

    user = f"""<job>
Title: {job['title']}
Company: {job['company']}
Location: {job['location']}
</job>

<job_description>
{jd_text}
</job_description>

<candidate_cv>
{cv}
</candidate_cv>"""

    response = client.messages.parse(
        model=MODEL,
        max_tokens=4000,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_format=ATSResult,
    )
    track_usage(response)
    return response.parsed_output


def analyze_job(job, cv, rules):
    """一個職缺的完整流程，回傳一個可以存檔、之後寫入 Google Sheet 的 dict。"""
    jd_text, jd_source = get_jd(job)
    base = {
        "title": job["title"],
        "company": job["company"],
        "location": job["location"],
        "url": job["url"],
        "jd_source": jd_source,
    }

    if jd_text.startswith(JD_NOT_FOUND):
        reason = jd_text.removeprefix(JD_NOT_FOUND).lstrip(": ").strip()
        return {**base, "jd_found": False, "verdict": "manual",
                "reason": f"找不到 JD，請手動確認：{reason}"}

    result = analyze(job, cv, jd_text, rules)
    source_url = ""
    if jd_text.startswith("SOURCE_URL:"):
        # 只取第一個網址，就算 Claude 在同一行多寫了說明也不會混進來
        first_line = jd_text.splitlines()[0].removeprefix("SOURCE_URL:").strip()
        source_url = first_line.split()[0] if first_line else ""

    return {**base, "jd_found": True, "jd_url": source_url, **result.model_dump()}


def print_result(r):
    if not r["jd_found"]:
        print(f"⚠️  {r['title']}｜{r['company']}\n   {r['reason']}\n   {r['url']}\n")
        return
    icon = {"apply": "🟢", "stretch": "🟡", "skip": "🔴"}[r["verdict"]]
    print(f"{icon} {r['ats_score']}%  {r['title']}｜{r['company']}（{r['verdict']}）")
    print(f"   {r['reason']}")
    print(f"   產業：{r['industry']}｜工作型態：{r['work_arrangement'] or '未知'}｜薪資：{r['salary'] or '未列'}")
    if r["dealbreakers"]:
        print(f"   硬傷：{'; '.join(r['dealbreakers'])}")
    if r["missing_keywords"]:
        print(f"   缺少：{', '.join(r['missing_keywords'][:8])}")
    print(f"   JD 來源（{r['jd_source']}）：{r.get('jd_url') or '—'}")
    print(f"   {r['url']}\n")


# ---------- 主流程 ----------
def load_cv():
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("找不到 ANTHROPIC_API_KEY，請加到 .env 檔裡。")
        sys.exit(1)
    # 在 GitHub Actions 上，履歷存在 CV_TEXT 這個 Secret 裡，而不是檔案
    if os.getenv("CV_TEXT"):
        return os.environ["CV_TEXT"]
    if not os.path.exists(CV_FILE):
        print(f"找不到 {CV_FILE}，請把你的 master 履歷存成純文字檔。")
        sys.exit(1)
    with open(CV_FILE, encoding="utf-8") as f:
        return f.read()


def analyze_jobs(kept, cv, rules, sheet_state=None):
    """
    分析留下的職缺，回傳結果清單。
    sheet_state 是 Google Sheet 裡已經有的職缺：{LinkedIn 連結: {"done": 是否已分析, "manual_jd": 手動 JD}}。
    在雲端執行時沒有本機快取，就靠 Sheet 判斷哪些分析過了，避免重複花錢。
    """
    sheet_state = sheet_state or {}
    cache = load_cache()

    for job in kept:
        job["manual_jd"] = sheet_state.get(job["url"], {}).get("manual_jd", "")

    def needs_analysis(job):
        in_sheet = sheet_state.get(job["url"])
        cached = cache.get(job["url"])
        if in_sheet and in_sheet["done"]:
            return False  # Sheet 裡已經有完整分析
        if cached and cached.get("jd_found"):
            return False  # 本機快取有完整分析
        if in_sheet or cached:
            # 之前找不到 JD：有手動 JD（Sheet 或 jds/ 資料夾）才重新分析
            return bool(job["manual_jd"]) or os.path.exists(manual_jd_path(job))
        return True  # 全新的職缺

    to_analyze = [job for job in kept if needs_analysis(job)]
    print(f"其中 {len(kept) - len(to_analyze)} 個之前分析過，這次分析 {len(to_analyze)} 個\n")

    for i, job in enumerate(to_analyze, 1):
        print(f"[{i}/{len(to_analyze)}] 分析中：{job['title']}｜{job['company']}...")
        try:
            result = analyze_job(job, cv, rules)
        except anthropic.APIError as error:
            print(f"   API 錯誤，跳過這個職缺：{error}\n")
            continue
        cache[job["url"]] = result
        save_cache(cache)  # 每分析完一個就存，中途出錯也不會白做
        print_result(result)

    return [cache[job["url"]] for job in kept if job["url"] in cache]


def print_usage():
    print(
        f"本次用量：輸入 {usage_totals['input']:,} tokens｜輸出 {usage_totals['output']:,} tokens｜"
        f"搜尋 {usage_totals['searches']} 次｜估計花費約 US${estimated_cost():.2f}"
    )


def main():
    cv = load_cv()
    days_back = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    rules = load_rules()
    jobs = list(collect_jobs(days_back).values())
    kept, rejected = filter_jobs(jobs, rules)
    log_rejected(rejected)
    print(f"篩選後留下 {len(kept)} 個職缺，排除 {len(rejected)} 個（記錄在 rejected_jobs.csv）\n")
    analyze_jobs(kept, cv, rules)
    print_usage()


if __name__ == "__main__":
    main()
