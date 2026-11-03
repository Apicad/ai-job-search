// Canned API responses, one per provider, shaped like the live endpoints
// (field names verified against each vendor's public job-board API docs).
// `routeFetch` turns them into an injectable fetch that answers by URL and
// records every request, so the suite never touches the network.

import { mkdtempSync, writeFileSync } from "fs"
import { tmpdir } from "os"
import { join } from "path"
import { setFetch, setSleep, type FetchFn } from "../src/helpers"

export const WATCHLIST = {
  companies: [
    { name: "Acme", ats: "greenhouse", slug: "acme" },
    { name: "Beta Corp", ats: "lever", slug: "betacorp" },
    { name: "Gamma", ats: "ashby", slug: "gamma" },
    { name: "Delta AG", ats: "smartrecruiters", slug: "DeltaAG" },
    { name: "Epsilon", ats: "workable", slug: "epsilon" },
  ],
}

/** Write a watchlist to a fresh temp dir and return its path (never the repo tree). */
export function tempWatchlist(data: unknown = WATCHLIST): string {
  const dir = mkdtempSync(join(tmpdir(), "ats-feeds-test-"))
  const path = join(dir, "companies.json")
  writeFileSync(path, JSON.stringify(data))
  return path
}

// Dates are relative to this fixed "now" so --days assertions never rot.
export const NOW = new Date("2026-11-03T12:00:00Z")
const daysAgo = (n: number) => new Date(NOW.getTime() - n * 86_400_000)

export const GREENHOUSE_JOBS = {
  jobs: [
    {
      id: 4001,
      title: "Senior Platform Engineer",
      absolute_url: "https://boards.greenhouse.io/acme/jobs/4001",
      updated_at: daysAgo(2).toISOString(),
      location: { name: "Berlin, Germany" },
      // Greenhouse escapes the HTML inside `content`.
      content: "&lt;p&gt;Build the &lt;strong&gt;platform&lt;/strong&gt; &amp;amp; ship it.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Go&lt;/li&gt;&lt;li&gt;Kubernetes&lt;/li&gt;&lt;/ul&gt;",
      departments: [{ name: "Engineering" }],
      offices: [{ name: "Berlin" }],
    },
    {
      id: 4002,
      title: "Account Executive",
      absolute_url: "https://boards.greenhouse.io/acme/jobs/4002",
      updated_at: daysAgo(40).toISOString(),
      location: { name: "New York, NY" },
      content: "&lt;p&gt;Sell things.&lt;/p&gt;",
      departments: [],
      offices: [],
    },
  ],
  meta: { total: 2 },
}

export const LEVER_JOBS = [
  {
    id: "7d1e6c2a-1b2c-4d3e-9f10-aaaaaaaaaaaa",
    text: "Platform Engineer, Data",
    hostedUrl: "https://jobs.lever.co/betacorp/7d1e6c2a-1b2c-4d3e-9f10-aaaaaaaaaaaa",
    applyUrl: "https://jobs.lever.co/betacorp/7d1e6c2a-1b2c-4d3e-9f10-aaaaaaaaaaaa/apply",
    createdAt: daysAgo(5).getTime(),
    categories: { location: "Remote - Europe", team: "Data", commitment: "Full-time" },
    workplaceType: "remote",
    descriptionPlain: "Own the data platform.",
    description: "<p>Own the data platform.</p>",
    lists: [{ text: "Requirements", content: "<li>Python &amp; SQL</li><li>Airflow</li>" }],
    additionalPlain: "We are an equal opportunity employer.",
  },
]

export const ASHBY_JOBS = {
  jobs: [
    {
      id: "0a1b2c3d-4e5f-4a6b-8c7d-bbbbbbbbbbbb",
      title: "Staff Engineer, Infrastructure",
      jobUrl: "https://jobs.ashbyhq.com/gamma/0a1b2c3d-4e5f-4a6b-8c7d-bbbbbbbbbbbb",
      applyUrl: "https://jobs.ashbyhq.com/gamma/0a1b2c3d-4e5f-4a6b-8c7d-bbbbbbbbbbbb/application",
      publishedAt: daysAgo(1).toISOString(),
      location: "San Francisco",
      isRemote: true,
      isListed: true,
      descriptionPlain: "Run the infrastructure.",
      descriptionHtml: "<p>Run the infrastructure.</p>",
      employmentType: "FullTime",
      department: "Engineering",
    },
    {
      id: "unlisted-0000-0000-0000-cccccccccccc",
      title: "Hidden Role",
      jobUrl: "https://jobs.ashbyhq.com/gamma/unlisted",
      publishedAt: daysAgo(1).toISOString(),
      location: "Nowhere",
      isRemote: false,
      isListed: false,
      descriptionPlain: "Should not be listed.",
    },
  ],
}

export const SMARTRECRUITERS_LIST = {
  totalFound: 1,
  content: [
    {
      id: "743999900000001",
      name: "Platform Engineer (m/f/d)",
      releasedDate: daysAgo(10).toISOString(),
      location: { city: "Stuttgart", region: "BW", country: "de", remote: false },
      ref: "https://api.smartrecruiters.com/v1/companies/DeltaAG/postings/743999900000001",
      company: { name: "Delta AG", identifier: "DeltaAG" },
    },
  ],
}

export const SMARTRECRUITERS_DETAIL = {
  id: "743999900000001",
  name: "Platform Engineer (m/f/d)",
  releasedDate: daysAgo(10).toISOString(),
  location: { city: "Stuttgart", region: "BW", country: "de", remote: false },
  company: { name: "Delta AG", identifier: "DeltaAG" },
  applyUrl: "https://jobs.smartrecruiters.com/DeltaAG/743999900000001-platform-engineer?apply=1",
  postingUrl: "https://jobs.smartrecruiters.com/DeltaAG/743999900000001-platform-engineer",
  jobAd: {
    sections: {
      jobDescription: { title: "Job Description", text: "<p>Keep the platform running.</p>" },
      qualifications: { title: "Qualifications", text: "<ul><li>Terraform</li></ul>" },
    },
  },
}

export const WORKABLE_LIST = {
  total: 1,
  results: [
    {
      id: 9001,
      shortcode: "AB12CD",
      title: "Platform Engineer",
      published: daysAgo(3).toISOString(),
      location: { city: "Athens", country: "Greece", workplaceType: "hybrid" },
      remote: false,
      url: "https://apply.workable.com/epsilon/j/AB12CD/",
    },
  ],
  nextPage: null,
}

export const WORKABLE_DETAIL = {
  id: 9001,
  shortcode: "AB12CD",
  title: "Platform Engineer",
  published: daysAgo(3).toISOString(),
  location: { city: "Athens", country: "Greece" },
  remote: false,
  description: "<p>Platform work.</p>",
  requirements: "<ul><li>Linux</li></ul>",
  url: "https://apply.workable.com/epsilon/j/AB12CD/",
}

export interface Route {
  /** Substring (or RegExp) the request URL must match. */
  match: string | RegExp
  status?: number
  body?: unknown
  /** Hold the response this long (ms) - for ordering tests. */
  delayMs?: number
}

export interface Recorded {
  url: string
  method: string
  body: string | null
  headers: Record<string, string>
}

export const DEFAULT_ROUTES: Route[] = [
  { match: "boards-api.greenhouse.io/v1/boards/acme/jobs/4001", body: GREENHOUSE_JOBS.jobs[0] },
  { match: "boards-api.greenhouse.io/v1/boards/acme/jobs/4002", body: GREENHOUSE_JOBS.jobs[1] },
  { match: "boards-api.greenhouse.io/v1/boards/acme/jobs?content=true", body: GREENHOUSE_JOBS },
  { match: "api.lever.co/v0/postings/betacorp/7d1e6c2a", body: LEVER_JOBS[0] },
  { match: "api.lever.co/v0/postings/betacorp?mode=json", body: LEVER_JOBS },
  { match: "api.ashbyhq.com/posting-api/job-board/gamma", body: ASHBY_JOBS },
  { match: "api.smartrecruiters.com/v1/companies/DeltaAG/postings/743999900000001", body: SMARTRECRUITERS_DETAIL },
  { match: "api.smartrecruiters.com/v1/companies/DeltaAG/postings?", body: SMARTRECRUITERS_LIST },
  { match: "apply.workable.com/api/v3/accounts/epsilon/jobs", body: WORKABLE_LIST },
  { match: "apply.workable.com/api/v2/accounts/epsilon/jobs/AB12CD", body: WORKABLE_DETAIL },
]

/**
 * Install a fetch that answers from `routes` (first match wins; unmatched
 * URLs get a 404) and sleeps instantly. Returns the request log.
 */
export function routeFetch(routes: Route[] = DEFAULT_ROUTES): Recorded[] {
  const log: Recorded[] = []
  const fn: FetchFn = async (url, init) => {
    const headers: Record<string, string> = {}
    for (const [k, v] of Object.entries((init?.headers ?? {}) as Record<string, string>)) headers[k.toLowerCase()] = v
    log.push({ url, method: init?.method ?? "GET", body: typeof init?.body === "string" ? init.body : null, headers })
    const route = routes.find((r) => (typeof r.match === "string" ? url.includes(r.match) : r.match.test(url)))
    if (!route) return new Response("not found", { status: 404 })
    if (route.delayMs) await new Promise((r) => setTimeout(r, route.delayMs))
    const status = route.status ?? 200
    const body = route.body === undefined ? "" : typeof route.body === "string" ? route.body : JSON.stringify(route.body)
    return new Response(body, { status, headers: { "content-type": "application/json" } })
  }
  setFetch(fn)
  setSleep(async () => {})
  return log
}

export function restoreFetch(): void {
  setFetch(null)
  setSleep(null)
}

/** Capture process.stdout writes; returns the buffer accessor and a restore function. */
export function captureStdout(): { get: () => string; restore: () => void } {
  let buf = ""
  const original = process.stdout.write
  process.stdout.write = ((chunk: string | Uint8Array) => {
    buf += chunk.toString()
    return true
  }) as typeof process.stdout.write
  return { get: () => buf, restore: () => void (process.stdout.write = original) }
}

export function captureStderr(): { get: () => string; restore: () => void } {
  let buf = ""
  const original = process.stderr.write
  process.stderr.write = ((chunk: string | Uint8Array) => {
    buf += chunk.toString()
    return true
  }) as typeof process.stderr.write
  return { get: () => buf, restore: () => void (process.stderr.write = original) }
}
