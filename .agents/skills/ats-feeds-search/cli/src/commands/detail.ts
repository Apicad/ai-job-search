import { loadCompanies, writeError, WatchlistError, type Company, type JobDetail } from "../helpers.js"
import { parseJobRef, PROVIDERS } from "../providers.js"

export interface DetailOpts {
  /** `<ats>:<slug>:<id>` or a hosted job URL from a search result. */
  ref: string
  companiesPath: string
  format: "json" | "plain"
}

/**
 * Resolve the reference to a posting. The watchlist is consulted for the
 * display name (and is the only way to resolve a Greenhouse `gh_jid=` link on
 * a company's own domain, which names no board slug); a reference that names
 * a board outside the watchlist still works, with the slug as company name.
 */
export async function fetchDetail(opts: DetailOpts): Promise<JobDetail | null> {
  let watchlist: Company[] = []
  try {
    watchlist = loadCompanies(opts.companiesPath)
  } catch (e) {
    // A missing watchlist only matters for the gh_jid fallback below.
    if (!(e instanceof WatchlistError)) throw e
  }

  const ref = parseJobRef(opts.ref)
  if (ref) {
    const known = watchlist.find((c) => c.ats === ref.ats && c.slug.toLowerCase() === ref.slug.toLowerCase())
    const company: Company = known ?? { name: ref.slug, ats: ref.ats, slug: ref.slug }
    return PROVIDERS[ref.ats].detail(company, ref.id)
  }

  // Greenhouse boards embedded on a company domain link as ...?gh_jid=<id>.
  const ghJid = opts.ref.match(/[?&]gh_jid=(\d+)/)
  if (ghJid) {
    for (const company of watchlist.filter((c) => c.ats === "greenhouse")) {
      const job = await PROVIDERS.greenhouse.detail(company, ghJid[1])
      if (job) return job
    }
    return null
  }

  throw new WatchlistError(
    `could not resolve "${opts.ref}" - pass an id of the form <ats>:<slug>:<id> (as emitted by search) or a hosted job URL (boards.greenhouse.io, jobs.lever.co, jobs.ashbyhq.com, jobs.smartrecruiters.com, apply.workable.com)`,
    "BAD_ID",
  )
}

export async function runDetail(opts: DetailOpts): Promise<number> {
  let job: JobDetail | null
  try {
    job = await fetchDetail(opts)
  } catch (e) {
    if (e instanceof WatchlistError) {
      writeError(e.message, e.code)
    } else {
      writeError(e instanceof Error ? e.message : String(e), "DETAIL_FAILED")
    }
    return 1
  }
  if (!job) {
    writeError(`job not found: ${opts.ref}`, "NOT_FOUND")
    return 1
  }

  if (opts.format === "plain") {
    const lines = [
      job.title,
      `${job.company ?? "—"} · ${job.location ?? "—"} · ${job.date ?? "—"}`,
      `id: ${job.id}`,
      "",
      job.description ?? "(no description)",
      "",
      `URL: ${job.url}`,
      job.apply_url ? `Apply: ${job.apply_url}` : "",
    ].filter((l, i, all) => l !== "" || (i > 0 && all[i - 1] !== ""))
    process.stdout.write(lines.join("\n") + "\n")
  } else {
    process.stdout.write(JSON.stringify(job) + "\n")
  }
  return 0
}
