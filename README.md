# Job Search Agent

A daily automated pipeline that reads my LinkedIn job alert emails, filters the roles, scores each one against my CV with Claude, and logs the results to a Google Sheet. Every morning I open one sheet and decide which roles are worth tailoring my resume for.

I'm a production manager moving from VFX into tech. I built this to cut down the time I spent reading job descriptions that were never a fit, and to get hands-on with AI agents, APIs and automation.

## How it works

```
LinkedIn job alert emails (Gmail)
        │
        ▼
1. Read alerts       Parse title, company and location from each email (IMAP)
        │
        ▼
2. Filter            Keyword, location and salary rules from filters.yaml
        │            → rejected roles are logged with the reason
        ▼
3. Check the Sheet   Skip roles that were already analyzed
        │
        ▼
4. Analyze           Claude finds the full job description with web search,
        │            then scores ATS fit against my CV (structured JSON output)
        ▼
5. Write results     Google Sheet: Jobs tab (scores, gaps, verdict) + Rejected tab
```

GitHub Actions runs it every day at 8:00 am Toronto time.

## Design decisions

- **No scraping.** LinkedIn prohibits scraping, so the agent reads the job alert emails LinkedIn already sends me.
- **Human in the loop.** Every rejected role is logged with its reason, so I can catch rules that filter out good roles. When the agent can't find a job description, it flags the row for manual review instead of guessing.
- **Google Sheet as the single source of truth.** Each cloud run starts on a fresh machine with no local state. Before calling the paid API, the agent checks the Sheet, so each role is only analyzed (and paid for) once.
- **Honest scoring.** The prompt tells Claude to credit only what my CV actually shows, and to flag hard requirements I don't meet.
- **Secrets stay secret.** API keys, the Gmail app password, the Google service account key and my CV live in a local `.env` file or GitHub Secrets, never in the code.

## Problems I hit and how I fixed them

| Problem | Cause | Fix |
|---|---|---|
| Good roles were filtered out | A recruiter typed "Project Manger" instead of "Manager" | Log every rejected role with its reason so I can review it and update the rules |
| Claude said my PMP was "expected" | The model's training data ends in June 2026, so it read my Sep 2026 certification as a future date | Inject today's date into the prompt |
| `cryptography` would not install | No prebuilt package for Python 3.14 on my Intel Mac | Rebuilt the virtual environment with Python 3.12 |
| The cloud run could not find the Google Sheet | The Sheet ID secret was pasted with extra characters | Made the code trim spaces and quotes, accept a full URL, and print a clearer error |
| A manual job description was ignored | Jobs that had already been processed were skipped, even if they had failed | Re-analyze a flagged role once a job description is provided |

## What I learned

This project taught me how to work with AI and AI agents to reach a goal: setting up the environment step by step on my own machine, building filters, debugging, adding features, and finally moving it to the cloud to run every day. I used Git to save a version at the end of each stage or day of work.

New ideas from testing went into a backlog. One of them is now a feature: when the agent can't find a job description, the Sheet marks the role "manual check". I paste the description into the "Manual JD" column, and the next scheduled run (or a manual run from GitHub) analyzes it and updates that row.

Early testing cost about US$0.14 per analyzed role, mostly from web search results.

## Backlog

- Flag alert emails that may have been cut off ("See all jobs"), so I know which alerts to open manually
- Add other legal job sources, such as public Greenhouse and Lever job boards
- Use the gap analysis to suggest resume bullet changes for high-scoring roles

## Tech stack

Python · Claude API (web search, structured outputs) · Gmail IMAP · Google Sheets API (gspread) · GitHub Actions · Git

## Setup

1. Create a virtual environment and install packages:
   ```
   python3.12 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and fill in your Gmail address, Gmail app password, Anthropic API key and Google Sheet ID.
3. Save your CV as `cv.txt` and your Google service account key as `service_account.json`. Share the Google Sheet with the service account email as an editor.
4. Edit `filters.yaml` for your own titles, locations and salary floor.
5. Run it locally: `python main.py 14`
6. To run it daily, add these GitHub Secrets: `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `ANTHROPIC_API_KEY`, `GOOGLE_SHEET_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `CV_TEXT`.
