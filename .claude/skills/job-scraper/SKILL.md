---
name: scrape
description: >
  Finds new job postings matching your profile via installed portal-search CLIs
  (LinkedIn, local job boards, and any skills added with /add-portal). Deduplicates
  across runs. Triggers on: job scrape, find jobs, search jobs, new jobs, job search,
  scrape jobs, /scrape
allowed-tools: Read, Write, Edit, Glob, Grep, Bash(bun --version), Bash(bun run .agents/skills/*/cli/src/cli.ts *), WebFetch, WebSearch, Agent, AskUserQuestion
---

# Job Scraper

---

## How It Works

This skill searches job portals using the **installed portal-search CLIs** in
`.agents/skills/` (plus WebSearch as a fallback), using queries from your profile.
It deduplicates against previously seen jobs and the application tracker, and
presents new matches with a quick fit assessment.

## Invocation

The user triggers this skill by saying things like:
- "Find new jobs"
- "Scrape for jobs"
- "Any new positions?"
- "/scrape"

Optional arguments:
- A focus area, e.g. "/scrape data science" or "/scrape geophysics"
- "broad" to run all search categories, e.g. "/scrape broad"
- "health" to run the portal health check only (Step 4.75), without searching, deduplicating, or presenting jobs - e.g. "/scrape health", or "/scrape health jobnet" to probe one portal even if disabled

---

## Execution Steps

### Step 0: Load State

1. `job_scraper/seen_jobs.json` is managed by `python tools/jobs_db.py` - **do not Read it whole** (it grows with every run and is never pruned automatically). The tool creates it on first write (`{"seen": {}}`) and prints only the entries each step needs.
2. `job_search_tracker.csv` is consulted by the same tool (`jobs_db.py list --exclude-tracker job_search_tracker.csv`) for the already-applied companies+roles - do not Read it here either.
3. Read `search-queries.md` (this directory) for the search strategy
4. `mkdir -p job_scraper/raw` - raw portal output is redirected there and never enters your context

### Step 1: Search

Use the search strategy from `search-queries.md` (already in context from Step 0 — do not read it again). By default, run the top 3 priority query categories. If the user said "broad", run all categories. If the user specified a focus area (e.g. "data science"), prioritize queries from that category.

**Use the installed CLI tools as the primary search mechanism.** Fall back to `WebSearch` only for portals that do not have a CLI skill, or if `bun` is unavailable on the system.

#### 1a. Check bun availability

```bash
bun --version
```

If this fails (bun not installed), skip to **1c (WebSearch fallback)** for all portals and note the fallback in the Step 5 output.

#### 1b. Run CLI tools (primary — run these in parallel where possible)

Discover the installed portal CLI skills with the manifest tool instead of reading every `SKILL.md`:

```bash
python tools/portals.py list
```

It parses each `.agents/skills/*/SKILL.md` frontmatter and prints name, `enabled`, version and the CLI path, so a disabled portal costs nothing to skip and a new portal added via `/add-portal` is picked up without changes to this file. Then Read the `SKILL.md` of each **enabled** portal only - each file documents that portal's exact CLI flags and usage examples. **Use each portal's own documented interface — do not guess flags.**

**Honor the `enabled` toggle.** A portal is enabled unless its `SKILL.md` frontmatter sets `enabled: false` (a missing key means enabled — the default); `portals.py` reports the value. Skip each disabled portal and record it for the Step 5 summary. A fork can thus keep a portal installed but sit out a run without deleting its directory.

For each **enabled** portal skill:

1. Read its `SKILL.md` to find the correct `bun run …` invocation and supported flags.
2. Translate the query terms from `search-queries.md` into that portal's flag format (e.g. `--key`, `--search-string`, `--query`, filter codes — whatever the portal's SKILL.md specifies).
3. Scope to the last 14 days using the portal's supported recency **filter** flag (`--jobage`, `--since <YYYY-MM-DD>`, etc. — as documented per portal). A portal with **no recency flag** (jobdanmark offers none) still gets scoped: every portal's search output carries a `date` field, so filter client-side — drop results whose `date` is older than 14 days after the call returns, and never invent a flag the portal's SKILL.md does not document (the CLIs reject unknown flags). `--order PublicationDate` is a sort, and a sort is not a filter — pairing it with a `--limit` is a defensible approximation on a portal that offers nothing better (jobnet), but apply the client-side date filter on top all the same.
4. Cap results to ~20 per call using the portal's limit flag.
5. Use `--format json` and **redirect stdout to a raw file** so the output never enters your context: `bun run … --format json > job_scraper/raw/<portal>-<n>.json`. Then register the results:
   ```bash
   python tools/jobs_db.py upsert --from job_scraper/raw/<portal>-<n>.json --portal <portal> --source cli
   ```
   `upsert` dedupes against `seen_jobs.json` by URL (or company+title), prints only the entries that are **new** (key, title, company, location, url, deadline) plus a count of the already-seen ones, and stores any `description` the portal's search output carries (freehire, ats-feeds) in the posting store - so do not pass `--no-description`; the text costs nothing here and saves `/rank` and `/apply` a fetch each. For a portal with no recency flag (item 3), pass `--since <YYYY-MM-DD, 14 days ago>` to `upsert` so results older than the window are dropped client-side before they are registered.

Run the CLI calls directly from this context as parallel Bash calls - **never spawn agents for searching**; each agent re-loads `CLAUDE.md` and the portal docs, and the raw output is handled by `jobs_db.py`, not by a model. The `added` arrays the tool prints are the pool for Step 2, each entry already tagged with its source portal (the `--portal` value, used for Step 2 `detail` lookups).

If a CLI tool exits with a non-zero code, log the error message and continue — do not abort the whole search.

#### 1b-ii. Aggregator MCP servers (before any WebSearch)

**Job-data MCP servers come before WebSearch.** When the session has a job-data MCP server attached (for example JobDataLake `search_jobs`/`get_job`, or the Indeed, Dice or ZipRecruiter `search_jobs` tools), query it with the `search-queries.md` terms, the configured location or country, and a 14-day window (`posted_within`/date filters where the tool offers them). These return structured JSON - title, company, location, date, URL and usually the full description - with no HTML parsing, no login and no ToS exposure, so for a market the installed portal CLIs do not cover they are the primary net, not a fallback. Write the normalized results (`title`, `company`, `location`, `date`, `url`, `description` when provided) to `job_scraper/raw/mcp-<server>.json` and register them with `jobs_db.py upsert --portal mcp-<server> --source cli` - MCP output is live tool output, the same provenance class as a CLI result. Keep volume bounded the same way as the CLIs (~20 per query).

#### 1c. WebSearch fallback

Use `WebSearch` for:
- Portals listed in `search-queries.md` that do **not** have a corresponding directory under `.agents/skills/`
- Any portal whose CLI fails at runtime
- When bun is unavailable (Step 1a failed)

Use the site-specific query strings from `search-queries.md` directly as WebSearch queries for these portals.

Tag each fallback result as WebSearch-sourced, keeping the portal tag when the fallback stands in for an installed portal whose CLI failed. Step 4 persists this as the entry's `source`, and Step 5 reports which portals ran on the fallback this run.

### Step 2: Fetch & Parse

For each promising result from Step 1:

**From CLI results:** Search output already includes title, company, location, date,
and URL. For jobs worth a deeper look, fetch full detail with that portal's `detail`
command (see its SKILL.md — do not guess flags), redirecting the plain-text output to a
raw file and storing it, so the full description lives on disk for `/rank` and `/apply`
and is never fetched again:

```bash
bun run … detail <id-or-url> --format plain > job_scraper/raw/detail-<n>.txt
python tools/jobs_db.py store-posting "<key>" --from job_scraper/raw/detail-<n>.txt --source cli-detail
```

`store-posting` prints a 300-character snippet and the **application deadline** it finds
- read only those; the **key requirements** are judged from the snippet and title for
Step 3's quick fit (the full text is for `/rank`). A portal whose search output already
carried the description (stored by `upsert` in Step 1b) needs no `detail` call at all -
`jobs_db.py list --status new` shows `posting_fetched` for those entries.

**From WebSearch results:** Use `WebFetch` on the posting URL and extract the same
fields manually. If it returns HTTP 403, retry with browser headers via curl per
`.claude/skills/job-application-assistant/09-web-research.md` before giving up — most
bank and corporate sites reject WebFetch's user agent while serving browsers normally.
Register the result with `upsert --source websearch` (a one-entry raw file), then save
the fetched text the same way (`store-posting … --source webfetch`).

**Store a URL that actually resolves to the posting.** A listing-page URL with a
`#fragment` appended (`.../jobs/ciso/#ikerian`) is not a posting: it fetches fine and
returns unrelated job titles, which makes every later `/rank` and `/apply` run fail on
that entry. When WebSearch only yields a listing page, search the employer's own careers
site for the role and store that URL instead, or drop the candidate rather than saving a
fragment link.

For every candidate:
- Skip if the URL or company+title combo already exists in `seen_jobs.json`
- Skip if the company+role already appears in `job_search_tracker.csv`

### Step 2.5: Mass-Posting Detection (within this run)

A distribution pattern worth flagging to the user as a caution signal, not as an accusation against the employer - it describes how a listing is being distributed, not a verdict on whether the company is legitimate. It alone proves nothing is wrong (companies do legitimately hire the same role across several cities); flag it so the user can factor it in when deciding whether to invest time, don't downgrade fit or silently exclude the result because of it.

If two or more results in this run's pool (from the same company, or sharing the same req/job ID visible in the URL or title) have substantially the same description and differ only in city/location/title, don't present them as separate rows. Consolidate into a single row and note the spread, e.g. "posted identically across 6 cities (BR, MX, GT)".

### Step 3: Quick Fit Assessment

For each new job, do a rapid fit check (NOT the full evaluation from `04-job-evaluation.md` - just a quick signal):

- **High match**: Role directly involves your core skills
- **Medium match**: Role is adjacent to your experience
- **Low match**: Role requires significant skills you lack

Record the levels in one call rather than editing the file: write `[{"key": "...", "fit": "high|medium|low"}]` (add `"status": "skipped"` for jobs you are not presenting) to `job_scraper/raw/fit-<YYYY-MM-DD>.json` and run `python tools/jobs_db.py set-fit --from job_scraper/raw/fit-<YYYY-MM-DD>.json`.

**Language override:** before assigning a match level, check the posting against `04-job-evaluation.md`'s Language Gate (a required language you haven't declared at all in your CLAUDE.md Languages table). A required language that's entirely undeclared overrides skill fit: mark it **Low** regardless of how well the skills align, and name it in the highlight bullets so it isn't buried under an otherwise-good-looking match. A **declared** language at a requirement that reads higher than your declared level is *not* an override — score fit normally, but add a red-flag bullet under that job's highlights (Step 5) quoting the posting's requirement next to your declared level, so the gap is visible without being auto-downgraded.

### Step 4: Deduplicate & Store

1. `jobs_db.py upsert` (Step 1b) already added ALL fetched jobs (new and skipped) to `seen_jobs.json`, and `set-fit` (Step 3) recorded the fit level - never rewrite the file from this context. The structure it maintains:
```json
{
  "seen": {
    "<url_or_company_title_key>": {
      "title": "...",
      "company": "...",
      "url": "...",
      "first_seen": "YYYY-MM-DD",
      "deadline": "YYYY-MM-DD" | null,
      "fit": "high/medium/low",
      "status": "new/skipped/ranked/expired",
      "portal": "<source portal skill, e.g. jobindex-search>",
      "source": "cli/websearch"
    }
  }
}
```

The `portal` field records which CLI skill produced the job (results are already tagged per portal in Step 1b - persist that tag here). Entries written before this field existed lack it; the health check (Step 4.75) attributes those by matching the URL's domain against each portal's base URL, so do not backfill.

The `source` field records which mechanism produced the entry: `cli` for Step 1b portal-CLI output, `websearch` for the Step 1c fallback. This is what keeps a ghost-job report diagnosable after the run's summary is gone: a stored entry whose URL later resolves to nothing (or to a different job) reads very differently depending on whether it came from live CLI output or from a search index that can be weeks stale - and a presented job with no entry here at all points at fabrication, which Rule 1 forbids. Entries written before this field existed lack it; never backfill it - the mechanism was not recorded. A third value, `email`, marks entries registered by `/gmail-sync` Step 5b from job-alert digests (LinkedIn, Indeed, Glassdoor, ZipRecruiter): live mail, but a board's one-line summary rather than a fetched posting, which is why `/rank` always fetches before scoring.

`/rank` extends this schema additively: ranked entries also carry `rank_score` (0–100 overall score), `rank_verdict` (fit band, e.g. "strong fit"), `rank_date` (ISO date of ranking), the veto fields `location_verdict` and `language_gate` (both PASS/FAIL/FLAG) with `language_note` (the quoted requirement explaining a non-PASS), and `strengths`/`gaps` (1-3 verbatim bullets each, copied from the scoring agent's findings). The `status` field is set to `"ranked"`. Do not drop any of these fields when re-writing entries. Entries ranked before `strengths`/`gaps` existed simply lack them; readers tolerate their absence and never backfill by guessing. Entries ranked before the verdict rename may carry a legacy PASS/FAIL/FLAG string in `location` - read that as the verdict when `location_verdict` is absent; in fresh entries `location` is always a place, never a verdict.

`posting_file` and `posting_fetched` are additive fields written by `jobs_db.py` when the posting text is stored (`job_scraper/postings/<hash>.md`, personal data, gitignored): the path and the fetch date. `/rank` reuses the text while it is at most 14 days old and `/apply` while it is at most 7, refetching otherwise - the stored copy is the CLI's own output, so Rule 1 is satisfied by it. Entries without the fields simply have no stored text.

`deadline` is a base field rather than a `/rank` extension: Step 2's detail fetch already extracts the application deadline, so it is written when the job is first seen and refreshed by `/rank` Step 4 when a scoring agent returns a different value. `null` means the posting states no deadline; a missing key means the entry predates this field - **never infer a deadline** from either, and never backfill by guessing.

2. Only present jobs NOT already in the seen list or tracker.

### Step 4.5: Generate Referral Contact Links (High & Medium Fit Only)

For every job from this run with `fit` of **high** or **medium** (skip low-fit jobs),
build two LinkedIn people-search URLs so the user can find a recruiter or team member to
reach out to for a referral or a warm intro. This is deliberately a link-generation step,
not an automated lookup: no scraping, no third-party API, zero runtime dependencies or
credentials required.

**A. Recruiters / Talent Acquisition (the referral path)**
```
https://www.linkedin.com/search/results/people/?keywords=<url-encoded "<Company Name> recruiter">&origin=GLOBAL_SEARCH_HEADER
```

**B. Role/team peers (informational-outreach / warm-intro path)**
```
https://www.linkedin.com/search/results/people/?keywords=<url-encoded "<Company Name> <role keyword>">&origin=GLOBAL_SEARCH_HEADER
```
Use a short keyword drawn from the posting's title for `<role keyword>` - e.g. a posting
titled "AI Program Manager" becomes `"<Company Name> AI Program Manager"`.

Both links are for the user to open and browse themselves - never fetch or scrape the
LinkedIn people-search result pages programmatically. Never fabricate contacts or claim a
specific person was found; these are search links, not results.

### Step 4.75: Portal Health Check

Scraper-based portal CLIs rot silently: when a portal changes its markup, the parser usually exits 0 with zero results or with null/garbled fields, and the Step 1c fallback never fires because it only triggers on hard failure. This step catches that from evidence the run already holds.

**Free pass (no extra requests).** For each enabled portal that ran in Step 1b:

- **Degraded scan:** inspect the results it returned this run. Flags: `company` null or empty on every result, empty titles, undecoded entities (`&amp;`) or HTML fragments in titles, URLs that do not point at the portal. Any of these means the parser is half-working and `/scrape` is silently collecting junk.
- **Yield history:** if the portal returned zero results across all of this run's queries, check whether `seen_jobs.json` holds prior entries from it (via the `portal` field, or by matching URL domains for entries predating the field). A portal that produced jobs on earlier runs and produces nothing now is suspect - the same queries worked before.

**Escalation (bounded, on suspicion only).** A suspect portal gets **one** sentinel probe: run its documented `search` with the example query from its own SKILL.md (that query provably worked when the skill was registered), the portal's limit flag capped at 3, `--format json`. If that returns nothing, retry **once** with a single common word. Only then is the verdict **broken**. A 429 or block page is **never** evidence of breakage - record the portal as **inconclusive (rate-limited)**, back off, and do not retry.

**Verdicts.** Healthy portals get silence - no table, no line. Anything else surfaces in the Step 5 summary as a health line.

**Probe-only mode (`/scrape health`).** Skip Steps 1-4 and this step's free pass (there is no fresh run to scan); instead probe every installed portal directly - enabled ones by default, a disabled one only when named explicitly (e.g. `/scrape health jobnet`). Each portal gets the sentinel probe above, the degraded criteria applied to whatever it returns, and - since the user explicitly asked for diagnosis - one `detail` fetch on the first result of each healthy portal (description must be readable decoded text; a failure downgrades to degraded). Report all statuses in this mode, including healthy. Volume stays bounded: one search, at most one retry, at most one detail per portal.

### Step 5: Present Results

Present new jobs in a table sorted by fit (high first). When Step 1b skipped
portals (`enabled: false`), report them with the `skipped (disabled):` line below
so opting one out stays visible rather than silent; omit the line when nothing
was skipped. When any portal's results came from the Step 1c fallback this run
(bun unavailable, or its CLI failed at runtime), report it with the
`fallback (websearch):` line - fallback results come from a search index that
can be stale, so the reader should know which rows carry that caveat; omit the
line when every portal ran its CLI. When Step 4.75 found a portal degraded, broken, or inconclusive,
add one `health:` line per suspect portal (healthy portals get no line); after
the report, offer to set that portal's `enabled: false` so `/scrape` stops
running it (and covers it via the Step 1c fallback) until it is fixed - only
edit the toggle with the user's confirmation, and never edit anything else in
the skill.

```
## New Job Matches - YYYY-MM-DD

Found X new positions (Y high, Z medium, W low match).

skipped (disabled): <portal-name>, <portal-name>

fallback (websearch): <portal-name>, <portal-name>

health: <portal-name> - degraded (company null on all 12 results); parsing anchors in .agents/skills/<portal-name>/url-reference.md
health: <portal-name> - broken (0 results for the SKILL.md test query and a broader retry); parsing anchors in .agents/skills/<portal-name>/url-reference.md

| # | Fit | Title | Company | Location | Deadline | URL |
|---|-----|-------|---------|----------|----------|-----|
| 1 | High | ... | ... | ... | ... | [Link](...) |

If Step 2.5 flagged a mass-posting pattern, note it in the Title cell (e.g. "Frontend Developer (posted in 6 cities)") rather than burying it. Do the same for a declared-language-insufficient-level flag from the Language Gate (e.g. "Backend Engineer ⚠ fluent English required") - both are signals the user should see at a glance, not just in the detail highlights below.

### High-Match Highlights
For each high-match job, add 2-3 bullet points:
- Why it matches your profile
- Key requirements to check
- Any red flags (including mass-posting signals from Step 2.5)

### Contacts
For each high/medium-fit job from Step 4.5, add a short contacts block with the two
LinkedIn search links:
- Recruiters/TA search link, for the referral path
- Role/team-peer search link, for the warm-intro / informational-outreach path
```

After presenting, ask:
> "Want me to evaluate any of these in detail? Just give me the number(s)."

If the user picks a number, invoke the **job-application-assistant** skill workflow (fit evaluation first, then CV + cover letter if approved).

If the run found many new jobs (roughly 8+), also suggest `/rank` - it batch-scores all new postings against the full fit framework and returns a ranked shortlist, which beats eyeballing a long table. (`/rank` sets the `ranked` and `expired` status values in `seen_jobs.json`; treat both as already-seen for dedup purposes.)

### Step 6: Update Tracker (Optional)

If the user decides to apply to any job, the tracker row is written by **job-application-assistant Step 3b**, which Step 5 already routes into - do not add a second row here. Only when the user says they applied to something outside that path, add a row using the header and the match-then-update rule in `/outcome` Step 1.

---

## Important Rules

1. **Never fabricate job postings.** Only present jobs from actual CLI search/detail output or WebSearch/WebFetch results.
2. **Respect deduplication.** Always check seen_jobs.json AND job_search_tracker.csv before presenting - `jobs_db.py upsert` does the first and `jobs_db.py list --exclude-tracker` the second, so the check costs no file reads.
3. **Focus on configured geographic area.** Skip jobs that require relocation or are clearly outside commute range.
4. **Only open positions.** Skip postings with expired deadlines or those marked as closed.
5. **Be efficient with detail fetches.** Don't run `detail` or WebFetch on every search hit — pre-filter by title/snippet, then fetch only promising matches.
6. **Parallel searches, no search agents.** Run portal CLI searches as parallel Bash calls from this context with their output redirected to `job_scraper/raw/`; never spawn an agent to run a search. Use attached job-data MCP servers for markets the CLIs don't cover, and WebSearch only for the gaps that remain.
7. **No automated people lookups.** Referral contacts (Step 4.5) are LinkedIn search links only - never fetch or scrape LinkedIn people-search result pages programmatically.
8. **Health checks are bounded and honest.** Step 4.75 spends at most one probe, one retry, and (in `health` mode) one detail fetch per portal - a diagnosis, not a crawl. A rate-limit is never evidence of breakage. Health verdicts come only from observed CLI output; a portal that could not be tested is reported as inconclusive, never guessed. The `enabled` toggle is the only thing the health check may edit, and only with confirmation.
9. **Flag distribution patterns, never accuse.** The mass-posting signal (Step 2.5) describes how a listing is being distributed, not a claim that the employer is a scam. Never name a company as fraudulent or untrustworthy - present the observation and let the user decide.
