# ats-feeds-cli

CLI that lists open postings at a **watchlist of companies** by reading the
official public JSON job-board APIs of five applicant-tracking systems:
Greenhouse, Lever, Ashby, SmartRecruiters and Workable (experimental).

**Data source**: vendor job-board APIs (see `../url-reference.md`).
**Authentication**: None - the endpoints are public and read-only.
**Dependencies**: None (plain `bun` + `fetch`). `bun install` is optional and only pulls dev type defs.
**No scraping**: structured JSON only; descriptions are HTML stripped to text.

## Installation

```bash
cd .agents/skills/ats-feeds-search/cli
bun install   # optional - only installs TypeScript dev types
cp ../companies.example.json ../companies.json   # then edit: your companies, their ATS, their board slug
```

`companies.json` is personal and git-ignored. Override its location with
`--companies <path>` or `ATS_FEEDS_COMPANIES=<path>`.

## Commands

| Command | Description |
|---------|-------------|
| `search` | List (and filter) every open posting across the watchlist |
| `detail <id\|url>` | Fetch one posting with its full description and apply URL |

`search` accepts `--format json|table|plain` (default `json`); `detail` accepts `--format json|plain`.
All errors are written to **stderr** as `{ "error": "...", "code": "..." }` with exit code `1`.

## Quick examples

```bash
bun run src/cli.ts search --days 14 --format table
bun run src/cli.ts search -q "platform engineer" -l remote --no-description
bun run src/cli.ts search --ats greenhouse,ashby --limit 20
bun run src/cli.ts detail greenhouse:cloudflare:5922061 --format plain
```

## Search flags

| Flag | Alias | Description |
|------|-------|-------------|
| `--companies` | | Watchlist JSON path. |
| `--query` | `-q` | Title filter; every word must match (case-insensitive). |
| `--location` | `-l` | Location substring; `remote` also matches remote-flagged jobs. |
| `--days` | `--jobage` | Posted/updated within N days. |
| `--limit` | `-n` | Cap results (newest first). |
| `--page` | | 1-indexed page of size `--limit`. |
| `--ats` | | Comma-separated providers to include. |
| `--no-description` | | Omit description bodies. |
| `--format` | | `json` \| `table` \| `plain`. |

## Development

```bash
bun run typecheck   # tsc --noEmit
bun test            # offline: every provider is exercised against fixtures via an injected fetch
```

The fetch function is injectable (`setFetch` / `setSleep` in `src/helpers.ts`),
which is how the tests cover retries, timeouts and per-provider normalisation
without network access.

See `../SKILL.md` for the full contract and `../url-reference.md` for the endpoints.
