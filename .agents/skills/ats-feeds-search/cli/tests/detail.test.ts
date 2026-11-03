import { afterEach, describe, expect, test } from "bun:test"
import { main } from "../src/cli"
import { fetchDetail } from "../src/commands/detail"
import { parseJobRef } from "../src/providers"
import { captureStderr, captureStdout, restoreFetch, routeFetch, tempWatchlist } from "./fixtures"

afterEach(() => restoreFetch())

describe("detail resolves ids and hosted URLs", () => {
  test("parseJobRef understands <ats>:<slug>:<id> and each vendor's hosted URL", () => {
    expect(parseJobRef("greenhouse:acme:4001")).toEqual({ ats: "greenhouse", slug: "acme", id: "4001" })
    expect(parseJobRef("https://boards.greenhouse.io/acme/jobs/4001?gh_src=x")).toEqual({ ats: "greenhouse", slug: "acme", id: "4001" })
    expect(parseJobRef("https://job-boards.greenhouse.io/acme/jobs/4001")).toEqual({ ats: "greenhouse", slug: "acme", id: "4001" })
    expect(parseJobRef("https://boards.greenhouse.io/embed/job_app?for=acme&token=4001")).toEqual({ ats: "greenhouse", slug: "acme", id: "4001" })
    expect(parseJobRef("https://jobs.lever.co/betacorp/7d1e6c2a-1b2c-4d3e-9f10-aaaaaaaaaaaa/apply")).toEqual({
      ats: "lever",
      slug: "betacorp",
      id: "7d1e6c2a-1b2c-4d3e-9f10-aaaaaaaaaaaa",
    })
    expect(parseJobRef("https://jobs.ashbyhq.com/gamma/0a1b2c3d-4e5f-4a6b-8c7d-bbbbbbbbbbbb")).toEqual({
      ats: "ashby",
      slug: "gamma",
      id: "0a1b2c3d-4e5f-4a6b-8c7d-bbbbbbbbbbbb",
    })
    expect(parseJobRef("https://jobs.smartrecruiters.com/DeltaAG/743999900000001-platform-engineer")).toEqual({
      ats: "smartrecruiters",
      slug: "DeltaAG",
      id: "743999900000001",
    })
    expect(parseJobRef("https://apply.workable.com/epsilon/j/AB12CD/")).toEqual({ ats: "workable", slug: "epsilon", id: "AB12CD" })
    expect(parseJobRef("https://example.com/careers/123")).toBeNull()
    expect(parseJobRef("bogus")).toBeNull()
  })

  test("greenhouse detail by id fetches the single-job endpoint", async () => {
    const log = routeFetch()
    const job = await fetchDetail({ ref: "greenhouse:acme:4001", companiesPath: tempWatchlist(), format: "json" })
    expect(log.map((r) => r.url)).toEqual(["https://boards-api.greenhouse.io/v1/boards/acme/jobs/4001"])
    expect(job).toMatchObject({
      id: "greenhouse:acme:4001",
      title: "Senior Platform Engineer",
      company: "Acme",
      apply_url: "https://boards.greenhouse.io/acme/jobs/4001",
      ats: "greenhouse",
    })
    expect(job!.description).toContain("Build the platform & ship it.")
  })

  test("a gh_jid link on a company domain is resolved through the watchlist's greenhouse boards", async () => {
    routeFetch()
    const job = await fetchDetail({
      ref: "https://www.acme.example/careers?gh_jid=4002",
      companiesPath: tempWatchlist(),
      format: "json",
    })
    expect(job?.id).toBe("greenhouse:acme:4002")
  })

  test("lever detail folds lists into the description and carries applyUrl", async () => {
    routeFetch()
    const job = await fetchDetail({
      ref: "https://jobs.lever.co/betacorp/7d1e6c2a-1b2c-4d3e-9f10-aaaaaaaaaaaa",
      companiesPath: tempWatchlist(),
      format: "json",
    })
    expect(job?.apply_url).toBe("https://jobs.lever.co/betacorp/7d1e6c2a-1b2c-4d3e-9f10-aaaaaaaaaaaa/apply")
    expect(job?.description).toContain("• Airflow")
  })

  test("ashby detail finds the job in the board payload", async () => {
    routeFetch()
    const job = await fetchDetail({ ref: "ashby:gamma:0a1b2c3d-4e5f-4a6b-8c7d-bbbbbbbbbbbb", companiesPath: tempWatchlist(), format: "json" })
    expect(job?.title).toBe("Staff Engineer, Infrastructure")
    expect(job?.apply_url).toContain("/application")
    const missing = await fetchDetail({ ref: "ashby:gamma:nope", companiesPath: tempWatchlist(), format: "json" })
    expect(missing).toBeNull()
  })

  test("smartrecruiters detail assembles the jobAd sections into the description", async () => {
    routeFetch()
    const job = await fetchDetail({ ref: "smartrecruiters:DeltaAG:743999900000001", companiesPath: tempWatchlist(), format: "json" })
    expect(job?.description).toBe("Job Description\nKeep the platform running.\n\nQualifications\n• Terraform")
    expect(job?.apply_url).toContain("apply=1")
    expect(job?.url).toBe("https://jobs.smartrecruiters.com/DeltaAG/743999900000001-platform-engineer")
  })

  test("workable detail (v2 job endpoint) joins description and requirements", async () => {
    const log = routeFetch()
    const job = await fetchDetail({ ref: "workable:epsilon:AB12CD", companiesPath: tempWatchlist(), format: "json" })
    expect(log[0].url).toBe("https://apply.workable.com/api/v2/accounts/epsilon/jobs/AB12CD")
    expect(job?.description).toBe("Platform work.\n\n• Linux")
    expect(job?.apply_url).toBe("https://apply.workable.com/epsilon/j/AB12CD/apply/")
  })

  test("a board outside the watchlist still resolves, with the slug as company name", async () => {
    routeFetch([{ match: "boards-api.greenhouse.io/v1/boards/other/jobs/77", body: { id: 77, title: "Thing", absolute_url: "https://boards.greenhouse.io/other/jobs/77", location: { name: "Oslo" } } }])
    const job = await fetchDetail({ ref: "greenhouse:other:77", companiesPath: tempWatchlist(), format: "json" })
    expect(job).toMatchObject({ company: "other", title: "Thing", date: null })
  })
})

describe("detail via main()", () => {
  test("json prints the full posting with apply_url", async () => {
    routeFetch()
    const out = captureStdout()
    try {
      expect(await main(["detail", "greenhouse:acme:4001", "--companies", tempWatchlist()])).toBe(0)
      const job = JSON.parse(out.get())
      for (const key of ["title", "company", "location", "date", "url", "description", "apply_url", "ats", "id"]) {
        expect(job).toHaveProperty(key)
      }
    } finally {
      out.restore()
    }
  })

  test("plain prints readable text", async () => {
    routeFetch()
    const out = captureStdout()
    try {
      expect(await main(["detail", "lever:betacorp:7d1e6c2a-1b2c-4d3e-9f10-aaaaaaaaaaaa", "--companies", tempWatchlist(), "--format", "plain"])).toBe(0)
      expect(out.get()).toContain("Platform Engineer, Data")
      expect(out.get()).toContain("Beta Corp")
      expect(out.get()).toContain("URL: https://jobs.lever.co/betacorp/")
    } finally {
      out.restore()
    }
  })

  test("a taken-down posting exits 1 with NOT_FOUND", async () => {
    routeFetch([])
    const err = captureStderr()
    const out = captureStdout()
    try {
      expect(await main(["detail", "greenhouse:acme:999", "--companies", tempWatchlist()])).toBe(1)
      expect(out.get()).toBe("")
      expect(JSON.parse(err.get()).code).toBe("NOT_FOUND")
    } finally {
      err.restore()
      out.restore()
    }
  })

  test("an unparseable reference exits 1 with BAD_ID", async () => {
    const log = routeFetch()
    const err = captureStderr()
    try {
      expect(await main(["detail", "https://example.com/jobs/1", "--companies", tempWatchlist()])).toBe(1)
      expect(JSON.parse(err.get()).code).toBe("BAD_ID")
      expect(log).toHaveLength(0)
    } finally {
      err.restore()
    }
  })
})
