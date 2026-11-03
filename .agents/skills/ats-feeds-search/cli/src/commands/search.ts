import {
  CONCURRENCY,
  loadCompanies,
  mapConcurrent,
  writeError,
  WatchlistError,
  type Ats,
  type Company,
  type JobResult,
} from "../helpers.js"
import { PROVIDERS } from "../providers.js"

export interface SearchOpts {
  /** Path to the watchlist JSON (already resolved by the CLI). */
  companiesPath: string
  query?: string
  location?: string
  /** Posted/updated within N days; undefined = no recency filter. */
  days?: number
  /** Client-side cap on results emitted (after filtering and sorting). */
  limit?: number
  /** 1-indexed page over the filtered list; only meaningful with --limit. */
  page: number
  /** Restrict the watchlist to these providers; empty = all. */
  ats: Ats[]
  includeDescription: boolean
  format: "json" | "table" | "plain"
  /** Override "now" (tests). */
  now?: Date
}

export interface CompanyError {
  company: string
  ats: Ats
  error: string
}

export interface SearchOutput {
  meta: { count: number; page: number; companies: number; failed: number }
  results: JobResult[]
  errors: CompanyError[]
  count: number
}

/** Every whitespace-separated word of `query` must appear in the title (case-insensitive). */
export function matchesQuery(title: string, query: string | undefined): boolean {
  if (!query) return true
  const haystack = title.toLowerCase()
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((word) => haystack.includes(word))
}

/** Substring match on location; "remote" additionally matches the provider's remote flag. */
export function matchesLocation(job: JobResult, location: string | undefined): boolean {
  if (!location) return true
  const needle = location.trim().toLowerCase()
  if (!needle) return true
  const loc = (job.location ?? "").toLowerCase()
  if (loc.includes(needle)) return true
  return needle === "remote" && job.remote === true
}

/**
 * Posted within `days` of `now`; undated jobs are kept (unknown is not "old").
 * Compared at UTC calendar-day granularity: `date` is already truncated to a
 * day, so measuring it against the current instant would drop a posting from
 * yesterday evening under `--days 1` (and everything under `--days 0`).
 */
export function withinDays(job: JobResult, days: number | undefined, now: Date): boolean {
  if (days === undefined) return true
  if (!job.date) return true
  const posted = Date.parse(`${job.date}T00:00:00Z`)
  if (!Number.isFinite(posted)) return true
  const today = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate())
  return today - posted <= days * 86_400_000
}

/** Newest first, then company, then title - deterministic across runs. */
export function sortResults(results: JobResult[]): JobResult[] {
  return [...results].sort(
    (a, b) =>
      (b.date ?? "").localeCompare(a.date ?? "") ||
      (a.company ?? "").localeCompare(b.company ?? "") ||
      a.title.localeCompare(b.title),
  )
}

/** Fetch every company's board, isolating failures into `errors`. */
export async function collect(
  companies: Company[],
  includeDescription: boolean,
): Promise<{ results: JobResult[]; errors: CompanyError[] }> {
  // Each worker returns its own outcome and the pieces are assembled in
  // watchlist order afterwards, so `errors` (like `results`) does not depend
  // on which request happened to finish first - two runs print the same thing.
  const batches = await mapConcurrent(companies, CONCURRENCY, async (company) => {
    try {
      const jobs = await PROVIDERS[company.ats].list(company)
      return { jobs: includeDescription ? jobs : jobs.map((j) => ({ ...j, description: null })), error: null }
    } catch (e) {
      const error: CompanyError = { company: company.name, ats: company.ats, error: e instanceof Error ? e.message : String(e) }
      return { jobs: [] as JobResult[], error }
    }
  })
  return {
    results: batches.flatMap((b) => b.jobs),
    errors: batches.flatMap((b) => (b.error ? [b.error] : [])),
  }
}

/** The whole search as data, so tests and other callers can use it without stdout. */
export async function search(opts: SearchOpts): Promise<SearchOutput> {
  let companies = loadCompanies(opts.companiesPath)
  if (opts.ats.length) {
    companies = companies.filter((c) => opts.ats.includes(c.ats))
    if (companies.length === 0) {
      throw new WatchlistError(`no watchlist company uses --ats ${opts.ats.join(",")}`, "NO_COMPANIES")
    }
  }
  const now = opts.now ?? new Date()
  const { results, errors } = await collect(companies, opts.includeDescription)
  let rows = sortResults(
    results.filter(
      (j) => matchesQuery(j.title, opts.query) && matchesLocation(j, opts.location) && withinDays(j, opts.days, now),
    ),
  )
  if (opts.limit !== undefined && opts.limit >= 0) {
    const start = (Math.max(1, opts.page) - 1) * opts.limit
    rows = rows.slice(start, start + opts.limit)
  } else if (opts.page > 1) {
    rows = []
  }
  return {
    meta: { count: rows.length, page: opts.page, companies: companies.length, failed: errors.length },
    results: rows,
    errors,
    count: rows.length,
  }
}

interface Column {
  header: string
  width: number
  cell: (r: JobResult) => string
}

function renderTable(rows: JobResult[]): string {
  if (rows.length === 0) return "No results."
  const columns: Column[] = [
    // The ID column is sized to the longest id so it is never truncated - a
    // cut id cannot be passed to `detail`.
    { header: "ID", width: Math.max(2, ...rows.map((r) => r.id.length)), cell: (r) => r.id },
    { header: "TITLE", width: 38, cell: (r) => r.title },
    { header: "COMPANY", width: 20, cell: (r) => r.company ?? "—" },
    { header: "LOCATION", width: 22, cell: (r) => r.location ?? "—" },
    { header: "DATE", width: 10, cell: (r) => r.date ?? "—" },
  ]
  const row = (cells: string[]) => cells.map((c, i) => c.slice(0, columns[i].width).padEnd(columns[i].width)).join("  ")
  const header = row(columns.map((c) => c.header))
  return [header, "-".repeat(header.length), ...rows.map((r) => row(columns.map((c) => c.cell(r))))].join("\n")
}

function renderPlain(rows: JobResult[]): string {
  if (rows.length === 0) return "No results."
  return rows
    .map((r) =>
      [r.title, `  ${r.company ?? "—"} · ${r.location ?? "—"} · ${r.date ?? "—"}`, `  id: ${r.id}`, `  ${r.url}`].join("\n"),
    )
    .join("\n\n")
}

function renderErrors(errors: CompanyError[]): string {
  if (errors.length === 0) return ""
  return "\n\nerrors:\n" + errors.map((e) => `  ${e.company} (${e.ats}): ${e.error}`).join("\n")
}

export async function runSearch(opts: SearchOpts): Promise<number> {
  let out: SearchOutput
  try {
    out = await search(opts)
  } catch (e) {
    if (e instanceof WatchlistError) {
      writeError(e.message, e.code)
    } else {
      writeError(e instanceof Error ? e.message : String(e), "SEARCH_FAILED")
    }
    return 1
  }

  // Every company failing is a run that produced nothing to look at (a
  // network outage, a bad watchlist): say so on stderr and exit 1 instead of
  // printing a plausible-looking empty result set.
  if (out.meta.companies > 0 && out.errors.length === out.meta.companies) {
    const first = out.errors[0]
    writeError(
      `all ${out.meta.companies} watchlist companies failed (first: ${first.company} [${first.ats}]: ${first.error})`,
      "ALL_COMPANIES_FAILED",
    )
    return 1
  }

  if (opts.format === "table") {
    process.stdout.write(renderTable(out.results) + renderErrors(out.errors) + "\n")
  } else if (opts.format === "plain") {
    process.stdout.write(renderPlain(out.results) + renderErrors(out.errors) + "\n")
  } else {
    process.stdout.write(JSON.stringify(out) + "\n")
  }
  return 0
}
