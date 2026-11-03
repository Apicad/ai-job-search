---
name: ats-feeds-search
version: 1.0.0
description: >
  Use this skill to list the open roles at a personal watchlist of companies by
  reading the official, public JSON job-board APIs of the applicant-tracking
  systems they host their careers pages on - Greenhouse, Lever, Ashby,
  SmartRecruiters and Workable (experimental). Works for any country and any
  sector; filters by title keywords, location/remote and posting age. Not a
  search engine: it only sees companies named in companies.json. Trigger
  phrases: watchlist jobs, what is <company> hiring for, new roles at my target
  companies, greenhouse jobs, lever jobs, ashby jobs, smartrecruiters jobs,
  workable jobs, check my target companies, ATS feed, company career page feed.
context: fork
enabled: true  # set to false to keep this portal installed but have /scrape skip it
allowed-tools: Bash(bun run .agents/skills/ats-feeds-search/cli/src/cli.ts *)
---

# ATS Feeds Search Skill

List every open posting at the companies on your watchlist, straight from the
**official public job-board JSON APIs** that five applicant-tracking systems
publish for each customer's careers page: **Greenhouse**, **Lever**, **Ashby**,
**SmartRecruiters** and **Workable** (experimental). No authentication, no API
key, no browser, no HTML scraping, and **zero runtime dependencies** - it runs
with just `bun`.

> This is a country-agnostic worked example of the repo's job-portal-skill
> pattern, like `linkedin-search` and `freehire-search`. The difference is the
> data source: instead of a job board's search page, it reads the structured
> feeds that companies themselves publish through their ATS vendor.

## Why official JSON APIs

Each of the five vendors documents a public, unauthenticated, read-only endpoint
that returns a company's open postings as JSON - the same data the company's
careers page renders. Reading it is the vendor's intended integration path, so:

- **No Terms-of-Service exposure** - unlike the HTML-scraping portals, there is
  no `robots.txt` question and no personal-use-only warning.
- **No HTML parsing** - fields arrive structured (`title`, `location`,
  `updated_at`, ...), so this skill cannot rot when a page's markup changes.
  Descriptions arrive as HTML and are stripped to plain text with entities decoded.
- **Direct from the source** - postings appear here the moment the company
  publishes them, before aggregators and job boards pick them up.

## The watchlist: `companies.json`

The skill only knows the companies you name. Its watchlist lives at
`.agents/skills/ats-feeds-search/companies.json` (personal, git-ignored). Start
from the shipped example:

```bash
cp .agents/skills/ats-feeds-search/companies.example.json .agents/skills/ats-feeds-search/companies.json
```

Format - one entry per company board:

```json
{
  "companies": [
    { "name": "Cloudflare", "ats": "greenhouse", "slug": "cloudflare" },
    { "name": "Plaid",      "ats": "lever",      "slug": "plaid" },
    { "name": "Ramp",       "ats": "ashby",      "slug": "ramp" },
    { "name": "Visa",       "ats": "smartrecruiters", "slug": "Visa" },
    { "name": "Blueground", "ats": "workable",   "slug": "blueground" }
  ]
}
```

| Field | Meaning |
|-------|---------|
| `name` | Display name used as `company` in results. Optional (defaults to the slug). |
| `ats` | One of `greenhouse`, `lever`, `ashby`, `smartrecruiters`, `workable`. |
| `slug` | The board identifier in the company's hosted careers URL: `boards.greenhouse.io/<slug>`, `jobs.lever.co/<slug>`, `jobs.ashbyhq.com/<slug>`, `jobs.smartrecruiters.com/<slug>`, `apply.workable.com/<slug>`. Case matters for SmartRecruiters. |

Finding a slug: open the company's careers page and look at the apply link's
domain. A company whose careers page is on its own domain often still embeds one
of these vendors (`gh_jid=` in the URL means Greenhouse; "Powered by Lever /
Ashby / Workable" in the footer). Companies that use a vendor outside these five
(Workday, SuccessFactors, iCIMS, Taleo, ...) cannot be watched by this skill.

The entries in `companies.example.json` are **examples to replace**, not a
recommendation - boards move between vendors, and a slug that stops working
shows up in the `errors` array of every search rather than failing silently.

Override the path with `--companies <path>` or the `ATS_FEEDS_COMPANIES` env var.

## When to use this skill

- "What is <company> hiring for right now?" for any company on the watchlist
- A recurring sweep of target companies (the `/scrape` run includes it automatically)
- Fetching the full description of one posting from its id or hosted URL

## Commands

### Search the watchlist

```bash
bun run .agents/skills/ats-feeds-search/cli/src/cli.ts search [flags]
```

Flags:
- `--companies <path>` - watchlist JSON. Default: `$ATS_FEEDS_COMPANIES`, else `companies.json` next to this file.
- `--query <text>` / `-q <text>` - title filter: **every word** must appear in the title (case-insensitive). The APIs have no server-side search; filtering is client-side.
- `--location <text>` / `-l <text>` - substring match on the posting's location. `-l remote` additionally matches postings the vendor flags as remote.
- `--days <n>` - posted/updated within the last N days (`--jobage <n>` is accepted as an alias for parity with the other portals). Undated postings are kept.
- `--limit <n>` / `-n <n>` - cap results emitted (client-side; results are sorted newest first before the cap).
- `--page <n>` - 1-indexed page of size `--limit`. Default 1.
- `--ats <list>` - only these providers, comma-separated (e.g. `--ats greenhouse,ashby`).
- `--no-description` - drop description bodies for a cheap discovery pass (same flag as `freehire-search`).
- `--format json|table|plain` - default `json`.

All watchlist companies are fetched concurrently (5 at a time). A company that
fails - unknown slug, 5xx after retries, timeout - is reported in `errors` and
**never aborts the run**; only when *every* company fails does the CLI exit 1.

### Fetch full job detail

```bash
bun run .agents/skills/ats-feeds-search/cli/src/cli.ts detail <id|url> [--companies <path>] [--format json|plain]
```

`id` is the `id` from a search result, of the form `<ats>:<slug>:<provider id>`
(e.g. `greenhouse:cloudflare:5922061`). A hosted job URL works too:
`boards.greenhouse.io/<slug>/jobs/<id>`, `jobs.lever.co/<slug>/<uuid>`,
`jobs.ashbyhq.com/<slug>/<uuid>`, `jobs.smartrecruiters.com/<slug>/<id>`,
`apply.workable.com/<slug>/j/<shortcode>`, and a Greenhouse `...?gh_jid=<id>`
link on a company's own domain (resolved through the watchlist's Greenhouse
boards). A board that is not on the watchlist still resolves; its slug is used
as the company name.

## Usage examples

```bash
# Everything new at the watchlist in the last 14 days, scannable
bun run .agents/skills/ats-feeds-search/cli/src/cli.ts search --days 14 --format table

# Platform/infra engineering roles, remote, without the bodies
bun run .agents/skills/ats-feeds-search/cli/src/cli.ts search -q "platform engineer" -l remote --no-description

# Only the Greenhouse and Ashby boards, 20 newest
bun run .agents/skills/ats-feeds-search/cli/src/cli.ts search --ats greenhouse,ashby --limit 20 --format table

# Data roles in Berlin
bun run .agents/skills/ats-feeds-search/cli/src/cli.ts search -q data -l berlin --format plain

# A different watchlist file
bun run .agents/skills/ats-feeds-search/cli/src/cli.ts search --companies ~/watchlists/fintech.json --days 7

# Full details for one posting (id from search, or its hosted URL)
bun run .agents/skills/ats-feeds-search/cli/src/cli.ts detail greenhouse:cloudflare:5922061 --format plain
bun run .agents/skills/ats-feeds-search/cli/src/cli.ts detail https://jobs.lever.co/plaid/0b5c7b1e-... --format plain
```

## Output contract

`search --format json` prints **one compact JSON object** on stdout:

```json
{
  "meta": { "count": 2, "page": 1, "companies": 5, "failed": 1 },
  "results": [
    {
      "title": "Senior Platform Engineer",
      "company": "Cloudflare",
      "location": "Lisbon, Portugal",
      "date": "2026-10-29",
      "url": "https://boards.greenhouse.io/cloudflare/jobs/5922061",
      "id": "greenhouse:cloudflare:5922061",
      "ats": "greenhouse",
      "description": "plain text, HTML stripped, entities decoded (null with --no-description)",
      "deadline": null,
      "remote": false
    }
  ],
  "errors": [ { "company": "Old Co", "ats": "lever", "error": "lever board \"oldco\" not found (404)" } ],
  "count": 2
}
```

- `title`, `company`, `location`, `date`, `url` are the fields every portal CLI
  guarantees (`/scrape` Step 2); missing values are `null`, never omitted.
- `date` is `YYYY-MM-DD` (UTC): Greenhouse `updated_at`, Lever `createdAt`,
  Ashby `publishedAt`, SmartRecruiters `releasedDate`, Workable `published`.
- `deadline` is always `null` - none of the five APIs publishes one.
- SmartRecruiters and Workable list endpoints carry no description; those
  results have `description: null` in search and `detail` fills it in.
- `count` equals `meta.count` (the number of results emitted after filters,
  `--limit` and `--page`).

`detail --format json` prints one object with `title`, `company`, `location`,
`date`, `url`, `description`, `apply_url`, `ats`, `id` (plus `deadline` and
`remote` for symmetry with search).

| Format | Best for |
|--------|----------|
| `json` | Default - programmatic use, passing ids to `detail` |
| `table` | Quick human-readable scanning (ids are never truncated); errors listed in a footer |
| `plain` | Reading a single job's full detail (`detail` command) |

All errors are written to **stderr** as `{ "error": "...", "code": "..." }` and
the process exits with code `1`. Codes: `UNKNOWN_FLAG`, `BAD_ARG`, `NO_ID`,
`BAD_ID`, `BAD_CMD`, `NO_COMPANIES_FILE`, `BAD_COMPANIES_FILE`, `NO_COMPANIES`,
`ALL_COMPANIES_FAILED`, `NOT_FOUND`, `SEARCH_FAILED`, `DETAIL_FAILED`.

## How `/scrape` consumes it

`/scrape` discovers this skill like every other portal (its `SKILL.md` under
`.agents/skills/`), honours the `enabled` toggle above, and calls:

```bash
bun run .agents/skills/ats-feeds-search/cli/src/cli.ts search --days 14 --limit 20 --no-description
```

Then, for promising hits, `detail <id>` for the full description. There is no
query to translate from `search-queries.md`: the watchlist *is* the query. Pass
`-q` only to narrow a large watchlist to a role family. Search results are
tagged `ats` per provider; the `url` is the vendor-hosted posting, which is what
`seen_jobs.json` deduplicates on.

## Limits

- **Watchlist-only, not a search engine.** It cannot find companies you have not
  named, and a company on a vendor outside the five is invisible to it.
- **Client-side filtering.** Every search downloads each board in full (one
  request per company; SmartRecruiters and Workable paginate, capped at 500
  postings per company). Large watchlists cost one request per company per run,
  so keep the list to companies you would actually apply to.
- **Workable is experimental.** Its v3 listing endpoint is a documented POST used
  by the vendor's own careers pages, but it is less stable than the other four;
  a Workable company that fails is reported in `errors` like any other.
- **Dates are "last updated" on Greenhouse** - a posting edited yesterday shows
  as yesterday even if it was opened months ago. The other vendors report the
  publish date.
- No deadlines, no salary normalisation (Ashby compensation is fetched but not
  surfaced), no server-side search.

## Notes

- Retries 429/5xx with exponential backoff and jitter (4 tries), 15 s timeout
  per request, honest User-Agent `Mozilla/5.0 (compatible; ats-feeds-search-cli/1.0)`.
- A 404 on a board means the slug is wrong or the company moved vendors; fix
  `companies.json`.
- Ashby has no per-posting endpoint; `detail` re-reads the board and picks the id.
- Endpoint shapes and the parsing anchors live in `url-reference.md`.
