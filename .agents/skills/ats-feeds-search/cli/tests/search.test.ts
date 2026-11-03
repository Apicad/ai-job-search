import { afterEach, describe, expect, test } from "bun:test"
import { main } from "../src/cli"
import { search, runSearch, type SearchOpts } from "../src/commands/search"
import { WATCHLIST, captureStderr, captureStdout, restoreFetch, routeFetch, tempWatchlist, NOW, DEFAULT_ROUTES, GREENHOUSE_JOBS, LEVER_JOBS } from "./fixtures"

afterEach(() => restoreFetch())

function opts(path: string, overrides: Partial<SearchOpts> = {}): SearchOpts {
  return { companiesPath: path, page: 1, ats: [], includeDescription: true, format: "json", now: NOW, ...overrides }
}

describe("search normalises every provider into the portal contract", () => {
  test("one result per fixture posting, with the contract fields filled", async () => {
    routeFetch()
    const out = await search(opts(tempWatchlist()))
    expect(out.errors).toEqual([])
    // 2 greenhouse + 1 lever + 1 ashby (the unlisted one is dropped) + 1 smartrecruiters + 1 workable
    expect(out.count).toBe(6)
    expect(out.meta).toEqual({ count: 6, page: 1, companies: 5, failed: 0 })
    for (const r of out.results) {
      for (const key of ["title", "company", "location", "date", "url", "id", "ats", "description", "deadline"]) {
        expect(r).toHaveProperty(key)
      }
      expect(r.id).toMatch(/^(greenhouse|lever|ashby|smartrecruiters|workable):[A-Za-z0-9_.-]+:.+$/)
      expect(r.date).toMatch(/^\d{4}-\d{2}-\d{2}$/)
      expect(r.deadline).toBeNull()
    }
    expect(out.results.map((r) => r.ats)).not.toContain("hidden")
    expect(out.results.find((r) => r.title === "Hidden Role")).toBeUndefined()
  })

  test("greenhouse: escaped HTML content becomes plain text, id and url are built from the board", async () => {
    routeFetch()
    const out = await search(opts(tempWatchlist(), { ats: ["greenhouse"] }))
    const job = out.results.find((r) => r.id === "greenhouse:acme:4001")!
    expect(job.title).toBe("Senior Platform Engineer")
    expect(job.company).toBe("Acme")
    expect(job.location).toBe("Berlin, Germany")
    expect(job.url).toBe("https://boards.greenhouse.io/acme/jobs/4001")
    expect(job.description).toBe("Build the platform & ship it.\n\n• Go\n• Kubernetes")
  })

  test("lever: createdAt milliseconds become YYYY-MM-DD, lists are folded into the description", async () => {
    routeFetch()
    const out = await search(opts(tempWatchlist(), { ats: ["lever"] }))
    expect(out.results).toHaveLength(1)
    const job = out.results[0]
    expect(job.id).toBe("lever:betacorp:7d1e6c2a-1b2c-4d3e-9f10-aaaaaaaaaaaa")
    expect(job.date).toBe("2026-10-29")
    expect(job.location).toBe("Remote - Europe")
    expect(job.remote).toBe(true)
    expect(job.description).toContain("Own the data platform.")
    expect(job.description).toContain("Requirements")
    expect(job.description).toContain("• Python & SQL")
    expect(job.description).toContain("equal opportunity")
  })

  test("ashby: unlisted postings are dropped, isRemote is carried", async () => {
    routeFetch()
    const out = await search(opts(tempWatchlist(), { ats: ["ashby"] }))
    expect(out.results).toHaveLength(1)
    expect(out.results[0]).toMatchObject({
      id: "ashby:gamma:0a1b2c3d-4e5f-4a6b-8c7d-bbbbbbbbbbbb",
      title: "Staff Engineer, Infrastructure",
      location: "San Francisco",
      remote: true,
      description: "Run the infrastructure.",
    })
  })

  test("smartrecruiters: location is composed from city/region/country and the posting URL is hosted", async () => {
    routeFetch()
    const out = await search(opts(tempWatchlist(), { ats: ["smartrecruiters"] }))
    expect(out.results).toHaveLength(1)
    expect(out.results[0]).toMatchObject({
      id: "smartrecruiters:DeltaAG:743999900000001",
      company: "Delta AG",
      location: "Stuttgart, BW, de",
      url: "https://jobs.smartrecruiters.com/DeltaAG/743999900000001",
      description: null,
    })
  })

  test("workable: the v3 listing is POSTed with the documented body and normalised", async () => {
    const log = routeFetch()
    const out = await search(opts(tempWatchlist(), { ats: ["workable"] }))
    const req = log.find((r) => r.url.includes("/api/v3/accounts/epsilon/jobs"))!
    expect(req.method).toBe("POST")
    expect(JSON.parse(req.body!)).toEqual({ query: "", location: [], department: [], worktype: [], remote: [] })
    expect(out.results[0]).toMatchObject({
      id: "workable:epsilon:AB12CD",
      title: "Platform Engineer",
      location: "Athens, Greece",
      url: "https://apply.workable.com/epsilon/j/AB12CD/",
    })
  })

  test("every request carries the honest User-Agent", async () => {
    const log = routeFetch()
    await search(opts(tempWatchlist()))
    expect(log.length).toBeGreaterThan(0)
    for (const r of log) expect(r.headers["user-agent"]).toBe("Mozilla/5.0 (compatible; ats-feeds-search-cli/1.0)")
  })
})

describe("search filters", () => {
  test("--query: every word must match the title, case-insensitively", async () => {
    routeFetch()
    const out = await search(opts(tempWatchlist(), { query: "PLATFORM engineer" }))
    expect(out.results.map((r) => r.title).sort()).toEqual([
      "Platform Engineer",
      "Platform Engineer (m/f/d)",
      "Platform Engineer, Data",
      "Senior Platform Engineer",
    ])
    const none = await search(opts(tempWatchlist(), { query: "platform executive" }))
    expect(none.count).toBe(0)
  })

  test("--location: substring on location, and 'remote' also matches the remote flag", async () => {
    routeFetch()
    const berlin = await search(opts(tempWatchlist(), { location: "berlin" }))
    expect(berlin.results.map((r) => r.id)).toEqual(["greenhouse:acme:4001"])
    const remote = await search(opts(tempWatchlist(), { location: "remote" }))
    // Lever says "Remote - Europe" in its location; Ashby only sets isRemote.
    expect(remote.results.map((r) => r.ats).sort()).toEqual(["ashby", "lever"])
  })

  test("--days keeps only postings within the window", async () => {
    routeFetch()
    const week = await search(opts(tempWatchlist(), { days: 7 }))
    expect(week.results.map((r) => r.id)).not.toContain("greenhouse:acme:4002") // 40 days old
    expect(week.results.map((r) => r.id)).not.toContain("smartrecruiters:DeltaAG:743999900000001") // 10 days old
    expect(week.count).toBe(4)
    const all = await search(opts(tempWatchlist(), { days: 60 }))
    expect(all.count).toBe(6)
  })

  test("--days compares calendar days, so --days 1 keeps yesterday evening and --days 0 keeps today", async () => {
    // Regression: the date is truncated to a day; measuring that midnight
    // against the current instant dropped a posting updated 23 hours ago.
    const yesterdayEvening = new Date(NOW.getTime() - 13 * 3_600_000).toISOString() // 2026-11-02T23:00Z
    const today = new Date(NOW.getTime() - 2 * 3_600_000).toISOString() // 2026-11-03T10:00Z
    const jobs = {
      jobs: [
        { ...GREENHOUSE_JOBS.jobs[0], id: 1, updated_at: yesterdayEvening },
        { ...GREENHOUSE_JOBS.jobs[1], id: 2, updated_at: today },
      ],
    }
    routeFetch([{ match: "boards-api.greenhouse.io/v1/boards/acme/jobs?content=true", body: jobs }])
    const path = tempWatchlist({ companies: WATCHLIST.companies.slice(0, 1) })
    expect((await search(opts(path, { days: 1 }))).results.map((r) => r.id).sort()).toEqual(["greenhouse:acme:1", "greenhouse:acme:2"])
    expect((await search(opts(path, { days: 0 }))).results.map((r) => r.id)).toEqual(["greenhouse:acme:2"])
  })

  test("results are sorted newest first and --limit/--page slice client-side", async () => {
    routeFetch()
    const all = await search(opts(tempWatchlist()))
    const dates = all.results.map((r) => r.date!)
    expect(dates).toEqual([...dates].sort().reverse())
    const first = await search(opts(tempWatchlist(), { limit: 2 }))
    expect(first.results.map((r) => r.id)).toEqual(all.results.slice(0, 2).map((r) => r.id))
    const second = await search(opts(tempWatchlist(), { limit: 2, page: 2 }))
    expect(second.results.map((r) => r.id)).toEqual(all.results.slice(2, 4).map((r) => r.id))
    const zero = await search(opts(tempWatchlist(), { limit: 0 }))
    expect(zero.count).toBe(0)
  })

  test("--no-description nulls every description but keeps the other fields", async () => {
    routeFetch()
    const out = await search(opts(tempWatchlist(), { includeDescription: false }))
    expect(out.results.every((r) => r.description === null)).toBe(true)
    expect(out.results.every((r) => r.title && r.url)).toBe(true)
  })

  test("--ats restricts which watchlist companies are fetched", async () => {
    const log = routeFetch()
    await search(opts(tempWatchlist(), { ats: ["lever", "ashby"] }))
    expect(log.every((r) => r.url.includes("lever.co") || r.url.includes("ashbyhq.com"))).toBe(true)
  })
})

describe("error isolation", () => {
  test("a company that answers 500 lands in errors; the others still return", async () => {
    const routes = [{ match: "api.lever.co", status: 500, body: "boom" }, ...DEFAULT_ROUTES]
    routeFetch(routes)
    const out = await search(opts(tempWatchlist()))
    expect(out.errors).toHaveLength(1)
    expect(out.errors[0]).toMatchObject({ company: "Beta Corp", ats: "lever" })
    expect(out.errors[0].error).toMatch(/500/)
    expect(out.meta.failed).toBe(1)
    expect(out.count).toBe(5)
  })

  test("errors follow watchlist order, not completion order (same output on every run)", async () => {
    // Regression: errors were pushed as each request finished, so a slow
    // failing board moved around in the output from one run to the next.
    routeFetch([
      { match: "boards-api.greenhouse.io", status: 500, delayMs: 60 },
      { match: "api.lever.co", status: 500 },
      ...DEFAULT_ROUTES,
    ])
    const out = await search(opts(tempWatchlist()))
    expect(out.errors.map((e) => e.company)).toEqual(["Acme", "Beta Corp"])
    expect(out.count).toBe(3)
  })

  test("a watchlist entry repeated twice is fetched once and its postings appear once", async () => {
    const log = routeFetch()
    const path = tempWatchlist({
      companies: [
        { name: "Acme", ats: "greenhouse", slug: "acme" },
        { name: "Acme again", ats: "Greenhouse", slug: "acme" },
        { name: "Beta Corp", ats: "lever", slug: "betacorp" },
      ],
    })
    const out = await search(opts(path))
    expect(log.filter((r) => r.url.includes("boards/acme/jobs"))).toHaveLength(1)
    expect(out.meta.companies).toBe(2)
    expect(out.results.filter((r) => r.id === "greenhouse:acme:4001")).toHaveLength(1)
    expect(out.results.find((r) => r.id === "greenhouse:acme:4001")?.company).toBe("Acme")
  })

  test("lever: workplaceType 'unspecified' falls back to the location text; an explicit value wins", async () => {
    const base = LEVER_JOBS[0]
    routeFetch([
      {
        match: "api.lever.co/v0/postings/betacorp?mode=json",
        body: [
          { ...base, id: "aaaaaaaa-0000-0000-0000-000000000001", workplaceType: "unspecified" }, // location "Remote - Europe"
          { ...base, id: "aaaaaaaa-0000-0000-0000-000000000002", workplaceType: "on-site" },
          { ...base, id: "aaaaaaaa-0000-0000-0000-000000000003", workplaceType: "unspecified", categories: { location: "Berlin" } },
          { ...base, id: "aaaaaaaa-0000-0000-0000-000000000004", workplaceType: undefined, categories: { location: "Berlin" } }, // no workplaceType at all
        ],
      },
    ])
    const out = await search(opts(tempWatchlist(), { ats: ["lever"] }))
    const remote = Object.fromEntries(out.results.map((r) => [r.id.split(":")[2].slice(-1), r.remote]))
    expect(remote).toEqual({ "1": true, "2": false, "3": null, "4": null })
    // -l remote: 1 via the remote flag and the location text, 2 via the location text only.
    expect((await search(opts(tempWatchlist(), { ats: ["lever"], location: "remote" }))).results.map((r) => r.id.slice(-1)).sort()).toEqual(["1", "2"])
  })

  test("an unknown board slug (404) is reported as an error, not an empty success", async () => {
    routeFetch(DEFAULT_ROUTES.filter((r) => !String(r.match).includes("ashbyhq")))
    const out = await search(opts(tempWatchlist()))
    expect(out.errors.map((e) => e.ats)).toEqual(["ashby"])
    expect(out.errors[0].error).toMatch(/404/)
  })

  test("all companies failing exits 1 with ALL_COMPANIES_FAILED on stderr and nothing on stdout", async () => {
    routeFetch([])
    const out = captureStdout()
    const err = captureStderr()
    try {
      const code = await runSearch(opts(tempWatchlist()))
      expect(code).toBe(1)
      expect(out.get()).toBe("")
      expect(JSON.parse(err.get()).code).toBe("ALL_COMPANIES_FAILED")
    } finally {
      out.restore()
      err.restore()
    }
  })
})

describe("the CLI entry point (main) - the real code path", () => {
  test("search --format json prints one compact JSON object with results, errors and count", async () => {
    routeFetch()
    const out = captureStdout()
    try {
      const code = await main(["search", "--companies", tempWatchlist(), "-q", "platform", "--days", "30", "--limit", "3"])
      expect(code).toBe(0)
      const lines = out.get().trim().split("\n")
      expect(lines).toHaveLength(1)
      const parsed = JSON.parse(lines[0])
      expect(parsed.count).toBe(3)
      expect(parsed.errors).toEqual([])
      expect(parsed.results[0]).toMatchObject({ title: expect.stringMatching(/platform/i) })
      expect(parsed.results.every((r: { date: string }) => r.date >= "2026-10-04")).toBe(true)
    } finally {
      out.restore()
    }
  })

  test("--jobage is accepted as an alias of --days", async () => {
    routeFetch()
    const out = captureStdout()
    try {
      expect(await main(["search", "--companies", tempWatchlist(), "--jobage", "7"])).toBe(0)
      expect(JSON.parse(out.get()).count).toBe(4)
    } finally {
      out.restore()
    }
  })

  test("--format table lists the untruncated id and the errors footer", async () => {
    routeFetch([{ match: "apply.workable.com", status: 503 }, ...DEFAULT_ROUTES])
    const out = captureStdout()
    try {
      expect(await main(["search", "--companies", tempWatchlist(), "--format", "table"])).toBe(0)
      const text = out.get()
      expect(text).toContain("greenhouse:acme:4001")
      expect(text).toContain("ID")
      expect(text).toMatch(/errors:\n\s+Epsilon \(workable\): .*503/)
    } finally {
      out.restore()
    }
  })

  test("ATS_FEEDS_COMPANIES env var picks the watchlist when --companies is absent", async () => {
    routeFetch()
    const out = captureStdout()
    const previous = process.env.ATS_FEEDS_COMPANIES
    process.env.ATS_FEEDS_COMPANIES = tempWatchlist({ companies: WATCHLIST.companies.slice(0, 1) })
    try {
      expect(await main(["search", "--no-description"])).toBe(0)
      const parsed = JSON.parse(out.get())
      expect(parsed.meta.companies).toBe(1)
      expect(parsed.results.every((r: { ats: string }) => r.ats === "greenhouse")).toBe(true)
    } finally {
      out.restore()
      if (previous === undefined) delete process.env.ATS_FEEDS_COMPANIES
      else process.env.ATS_FEEDS_COMPANIES = previous
    }
  })
})
