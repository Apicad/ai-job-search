# ATS job-board API reference

The public, unauthenticated endpoints this skill reads, their request shape, and
the response fields the adapters in `cli/src/providers.ts` map onto the portal
contract. Update this file (and the matching adapter) if a vendor changes its API.

All requests: `User-Agent: Mozilla/5.0 (compatible; ats-feeds-search-cli/1.0)`,
`Accept: application/json`, 15 s timeout, 4 tries with exponential backoff on
429/5xx, 404 treated as "board/posting does not exist".

| Vendor | List | Detail | Hosted job URL |
|--------|------|--------|----------------|
| Greenhouse | `GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` | `GET .../boards/{slug}/jobs/{id}` | `https://boards.greenhouse.io/{slug}/jobs/{id}` (also `job-boards.greenhouse.io`, `boards.eu.greenhouse.io`, `embed/job_app?for={slug}&token={id}`, and `?gh_jid={id}` on company domains) |
| Lever | `GET https://api.lever.co/v0/postings/{slug}?mode=json` | `GET .../postings/{slug}/{id}` | `https://jobs.lever.co/{slug}/{uuid}` (also `jobs.eu.lever.co`) |
| Ashby | `GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true` | none - the board payload is filtered by `id` | `https://jobs.ashbyhq.com/{slug}/{uuid}` |
| SmartRecruiters | `GET https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset=N` | `GET .../postings/{id}` | `https://jobs.smartrecruiters.com/{slug}/{id}-{title-slug}` |
| Workable (experimental) | `POST https://apply.workable.com/api/v3/accounts/{slug}/jobs` body `{"query":"","location":[],"department":[],"worktype":[],"remote":[]}` (+ `"token"` for the next page) | `GET https://apply.workable.com/api/v2/accounts/{slug}/jobs/{shortcode}` | `https://apply.workable.com/{slug}/j/{SHORTCODE}/` |

## Field mapping

| Contract field | Greenhouse | Lever | Ashby | SmartRecruiters | Workable |
|----------------|------------|-------|-------|-----------------|----------|
| `title` | `title` | `text` | `title` | `name` | `title` |
| `company` | watchlist `name` | watchlist `name` | watchlist `name` | `company.name`, else watchlist | watchlist `name` |
| `location` | `location.name`, else `offices[].name` | `categories.location`, else `categories.allLocations[]` | `location` + `secondaryLocations[].location` | `location.city, region, country` | `location.city, region, country` |
| `date` | `updated_at` (ISO) | `createdAt` (epoch ms) | `publishedAt` (ISO) | `releasedDate` (ISO) | `published` (ISO) |
| `url` | `absolute_url` | `hostedUrl` | `jobUrl` | `postingUrl`, else built | `url`, else built |
| `id` | `greenhouse:{slug}:{id}` | `lever:{slug}:{id}` | `ashby:{slug}:{id}` | `smartrecruiters:{slug}:{id}` | `workable:{slug}:{shortcode}` |
| `description` | `content` (HTML, **entity-escaped** - unescaped once, then stripped) | `descriptionPlain` + `lists[].text/content` + `additionalPlain` | `descriptionPlain`, else `descriptionHtml` | list: none; detail: `jobAd.sections.{companyDescription,jobDescription,qualifications,additionalInformation}.text` | list: none; detail: `description` + `requirements` + `benefits` |
| `remote` | `/remote/i` on location | `workplaceType == "remote"` | `isRemote` | `location.remote` | `remote`, else `location.workplaceType == "remote"` |
| `apply_url` (detail) | `absolute_url` | `applyUrl` | `applyUrl` | `applyUrl` | `applyUrl`, else `{url}apply/` |
| `deadline` | null | null | null | null | null |

## Response notes

- **Greenhouse** - `{ "jobs": [...], "meta": { "total": N } }`. `content` is present
  only with `content=true` and is HTML that has been entity-escaped (`&lt;p&gt;`),
  hence the extra unescape step. `updated_at` is the last edit, not the open date.
  `departments[]` / `offices[]` are available but not surfaced.
- **Lever** - a bare JSON array. `createdAt` is epoch milliseconds. `lists[]` holds
  the "Requirements"/"Responsibilities" blocks as `{ text, content }` where
  `content` is `<li>` HTML without a wrapping `<ul>`. `workplaceType` is one of
  `remote | hybrid | on-site | unspecified`.
- **Ashby** - `{ "jobs": [...] }`. Postings with `isListed: false` are skipped.
  There is no per-job endpoint, so `detail` re-reads the board. `compensation` is
  requested but not surfaced.
- **SmartRecruiters** - `{ "offset", "limit", "totalFound", "content": [...] }`,
  paginated 100 at a time (the API maximum); the adapter reads up to 5 pages.
  Only the detail endpoint carries the ad text. Company identifiers are
  case-sensitive (`Visa`, `BoschGroup`).
- **Workable** - `{ "results": [...], "nextPage": "<token>|null", "total": N }`;
  paginate by re-POSTing with `"token": nextPage`, capped at 5 pages. The v3 list
  endpoint is what `apply.workable.com` career pages call; marked experimental
  because it is less documented than the others. The v2 job endpoint returns
  `description`, `requirements`, `benefits` as HTML.

## Error semantics

- Board/posting 404 -> list: the company lands in `errors` with "not found
  (404)"; detail: `NOT_FOUND`, exit 1.
- 429 / 5xx -> retried (4 tries total); if still failing, the company lands in
  `errors` with the status.
- Any other non-2xx, connection failure, timeout, or non-JSON body -> `errors`.
- Every company failing -> `ALL_COMPANIES_FAILED`, exit 1, nothing on stdout.
