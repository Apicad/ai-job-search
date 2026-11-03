// Data source: the official, public, unauthenticated JSON job-board APIs that
// five applicant-tracking systems publish for every company that hosts its
// careers page on them (Greenhouse, Lever, Ashby, SmartRecruiters, Workable).
// There is no HTML to scrape and no login: each company in the watchlist is
// one documented GET (one POST for Workable) that returns structured JSON,
// which this file reshapes into the portal-skill contract.
//
// Every network call goes through `fetchJson`, which reads the fetch function
// from a module-level slot (`setFetch`) so the test suite can inject fixtures
// without touching the network or monkey-patching globals.

import { readFileSync } from "fs"
import { dirname, isAbsolute, resolve } from "path"

export const USER_AGENT = "Mozilla/5.0 (compatible; ats-feeds-search-cli/1.0)"
export const REQUEST_TIMEOUT_MS = 15000
/** Initial attempt plus three retries on 429/5xx. */
export const MAX_TRIES = 4
/** Companies fetched concurrently during a search. */
export const CONCURRENCY = 5

export const ATS_PROVIDERS = ["greenhouse", "lever", "ashby", "smartrecruiters", "workable"] as const
export type Ats = (typeof ATS_PROVIDERS)[number]

export function isAts(value: string): value is Ats {
  return (ATS_PROVIDERS as readonly string[]).includes(value)
}

export function writeError(error: string, code: string): void {
  process.stderr.write(JSON.stringify({ error, code }) + "\n")
}

// ---------------------------------------------------------------------------
// Injectable fetch + sleep

export type FetchFn = (input: string, init?: RequestInit) => Promise<Response>
export type SleepFn = (ms: number) => Promise<void>

let fetchImpl: FetchFn | null = null
let sleepImpl: SleepFn = (ms) => new Promise((r) => setTimeout(r, ms))

/** Replace the fetch function (tests). Pass `null` to restore the global fetch. */
export function setFetch(fn: FetchFn | null): void {
  fetchImpl = fn
}

/** Replace the backoff sleep (tests). Pass `null` to restore real timers. */
export function setSleep(fn: SleepFn | null): void {
  sleepImpl = fn ?? ((ms) => new Promise((r) => setTimeout(r, ms)))
}

function currentFetch(): FetchFn {
  // Resolved lazily so a test that stubs globalThis.fetch is honored too.
  return fetchImpl ?? ((input, init) => globalThis.fetch(input, init))
}

export interface FetchJsonOptions {
  method?: "GET" | "POST"
  body?: unknown
}

/**
 * Fetch a JSON document. Retries 429/5xx with exponential backoff and jitter
 * (MAX_TRIES attempts in total), returns `null` on a 404 (an unknown board
 * slug, or a posting that was taken down), and throws on any other failure.
 * A connection failure or timeout fails fast with a readable message.
 */
export async function fetchJson<T = unknown>(url: string, options: FetchJsonOptions = {}): Promise<T | null> {
  let delay = 500
  for (let attempt = 1; attempt <= MAX_TRIES; attempt++) {
    const headers: Record<string, string> = {
      "User-Agent": USER_AGENT,
      Accept: "application/json",
    }
    const init: RequestInit = {
      method: options.method ?? "GET",
      headers,
      redirect: "follow",
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    }
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json"
      init.body = JSON.stringify(options.body)
    }

    let response: Response
    try {
      response = await currentFetch()(url, init)
    } catch (e) {
      throw new Error(`could not reach ${hostOf(url)} (${e instanceof Error ? e.message : String(e)})`)
    }

    if (response.status === 429 || response.status >= 500) {
      if (attempt === MAX_TRIES) {
        throw new Error(`request failed: ${response.status} ${response.statusText} (${url})`)
      }
      await sleepImpl(delay + Math.floor(Math.random() * 500))
      delay = Math.min(delay * 2, 8000)
      continue
    }
    if (response.status === 404) return null
    if (!response.ok) {
      throw new Error(`request failed: ${response.status} ${response.statusText} (${url})`)
    }
    const text = await response.text()
    try {
      return JSON.parse(text) as T
    } catch {
      throw new Error(`${hostOf(url)} returned a non-JSON response`)
    }
  }
  throw new Error("request failed after retries")
}

function hostOf(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return url
  }
}

// ---------------------------------------------------------------------------
// HTML -> plain text

/**
 * Convert a Unicode code point to a string. Uses `fromCodePoint` (not
 * `fromCharCode`) so supplementary-plane code points decode correctly, and
 * drops out-of-range values instead of throwing.
 */
function numericEntity(cp: number): string {
  return cp >= 0 && cp <= 0x10ffff ? String.fromCodePoint(cp) : ""
}

const NAMED_ENTITIES: Record<string, string> = {
  amp: "&",
  lt: "<",
  gt: ">",
  quot: '"',
  apos: "'",
  nbsp: " ",
  ndash: "–",
  mdash: "—",
  hellip: "…",
  lsquo: "‘",
  rsquo: "’",
  ldquo: "“",
  rdquo: "”",
  bull: "•",
  middot: "·",
  copy: "©",
  reg: "®",
  trade: "™",
  euro: "€",
  pound: "£",
  yen: "¥",
  eacute: "é",
  egrave: "è",
  agrave: "à",
  aacute: "á",
  ccedil: "ç",
  uuml: "ü",
  ouml: "ö",
  auml: "ä",
  szlig: "ß",
  aring: "å",
  oslash: "ø",
  aelig: "æ",
}

export function decodeHtmlEntities(text: string): string {
  return text
    .replace(/&#(\d+);/g, (_, dec) => numericEntity(parseInt(dec, 10)))
    .replace(/&#[xX]([0-9a-fA-F]+);/g, (_, hex) => numericEntity(parseInt(hex, 16)))
    .replace(/&([a-zA-Z]+);/g, (whole, name: string) => NAMED_ENTITIES[name] ?? whole)
    // `&amp;` last, so `&amp;lt;` decodes to `&lt;` (one layer), not `<`.
    .replace(/&amp;/g, "&")
}

/**
 * Strip HTML to readable plain text: block-level closes and <br> become line
 * breaks, list items get a bullet, inline tags collapse to spaces, entities
 * are decoded, and runs of blank lines are squeezed to one.
 */
export function htmlToText(html: string | null | undefined): string | null {
  if (!html) return null
  const withBreaks = html
    .replace(/<\s*(script|style)[^>]*>[\s\S]*?<\/\s*\1\s*>/gi, " ")
    .replace(/<!--[\s\S]*?-->/g, " ")
    .replace(/<\s*br\s*\/?>/gi, "\n")
    .replace(/<\s*li[^>]*>/gi, "\n• ")
    // <li> opens its own line, so its close is just a separator.
    .replace(/<\/\s*li\s*>/gi, " ")
    .replace(/<\/\s*(p|ul|ol|div|h[1-6]|tr|table|blockquote|section|article|header|footer)\s*>/gi, "\n")
    .replace(/<\s*(p|div|h[1-6]|tr|table|blockquote|section|article)[^>]*>/gi, "\n")
  const text = decodeHtmlEntities(withBreaks.replace(/<[^>]+>/g, " "))
    .replace(/ /g, " ")
    .split("\n")
    .map((line) => line.replace(/[ \t]+/g, " ").trim())
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim()
  return text || null
}

/**
 * Greenhouse's `content` field is HTML that has itself been entity-escaped
 * (`&lt;p&gt;...`). Unescape one layer first when it looks that way, then
 * strip like any other HTML.
 */
export function escapedHtmlToText(content: string | null | undefined): string | null {
  if (!content) return null
  const looksEscaped = /&lt;\s*\/?\s*[a-zA-Z]/.test(content) && !/<\s*\/?\s*[a-zA-Z]/.test(content)
  return htmlToText(looksEscaped ? decodeHtmlEntities(content) : content)
}

// ---------------------------------------------------------------------------
// Dates

/** ISO string, epoch milliseconds, or epoch seconds -> "YYYY-MM-DD" (UTC), or null. */
export function toIsoDate(value: unknown): string | null {
  if (value === null || value === undefined || value === "") return null
  let ms: number
  if (typeof value === "number") {
    // Lever reports createdAt in milliseconds; tolerate seconds as well.
    ms = value < 1e11 ? value * 1000 : value
  } else if (typeof value === "string") {
    const trimmed = value.trim()
    if (/^\d{10,13}$/.test(trimmed)) {
      const n = parseInt(trimmed, 10)
      ms = n < 1e11 ? n * 1000 : n
    } else {
      ms = Date.parse(trimmed)
    }
  } else {
    return null
  }
  if (!Number.isFinite(ms)) return null
  const d = new Date(ms)
  if (isNaN(d.getTime())) return null
  return d.toISOString().slice(0, 10)
}

// ---------------------------------------------------------------------------
// Result contract

export interface JobResult {
  title: string
  company: string | null
  location: string | null
  date: string | null
  url: string
  /** `<ats>:<slug>:<provider id>` - stable, and what `detail` resolves. */
  id: string
  ats: Ats
  description: string | null
  deadline: null
  /** Provider remote flag when the API exposes one; null when unknown. */
  remote: boolean | null
}

export interface JobDetail extends JobResult {
  apply_url: string | null
}

export function makeId(ats: Ats, slug: string, providerId: string | number): string {
  return `${ats}:${slug}:${providerId}`
}

/** Join location parts, dropping empties and duplicates. */
export function joinLocation(parts: Array<string | null | undefined>): string | null {
  const seen = new Set<string>()
  const out: string[] = []
  for (const p of parts) {
    const v = (p ?? "").trim()
    if (!v || seen.has(v.toLowerCase())) continue
    seen.add(v.toLowerCase())
    out.push(v)
  }
  return out.length ? out.join(", ") : null
}

// ---------------------------------------------------------------------------
// Watchlist

export interface Company {
  name: string
  ats: Ats
  slug: string
}

/** The skill root (`.agents/skills/ats-feeds-search/`), two levels above `cli/src/`. */
export const SKILL_ROOT = resolve(dirname(new URL(import.meta.url).pathname), "..", "..")
export const DEFAULT_COMPANIES_PATH = resolve(SKILL_ROOT, "companies.json")
export const EXAMPLE_COMPANIES_PATH = resolve(SKILL_ROOT, "companies.example.json")

export class WatchlistError extends Error {
  code: string
  constructor(message: string, code: string) {
    super(message)
    this.code = code
  }
}

const SLUG_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$/

/** Resolve the watchlist path: --companies flag, ATS_FEEDS_COMPANIES env, default. */
export function resolveCompaniesPath(flag?: string): string {
  const candidate = flag?.trim() || (process.env.ATS_FEEDS_COMPANIES ?? "").trim() || DEFAULT_COMPANIES_PATH
  return isAbsolute(candidate) ? candidate : resolve(process.cwd(), candidate)
}

/**
 * Read and validate the watchlist. Throws WatchlistError (with a code the CLI
 * passes through) on a missing/unreadable file, malformed JSON, or an entry
 * that would build a bad URL (unknown ats, unsafe slug).
 */
export function loadCompanies(path: string): Company[] {
  let raw: string
  try {
    raw = readFileSync(path, "utf8")
  } catch {
    throw new WatchlistError(
      `watchlist not found at ${path} - copy ${EXAMPLE_COMPANIES_PATH} to ${DEFAULT_COMPANIES_PATH} and edit it, or pass --companies <path>`,
      "NO_COMPANIES_FILE",
    )
  }
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch (e) {
    throw new WatchlistError(`watchlist ${path} is not valid JSON (${e instanceof Error ? e.message : String(e)})`, "BAD_COMPANIES_FILE")
  }
  const list = Array.isArray(parsed)
    ? parsed
    : parsed && typeof parsed === "object" && Array.isArray((parsed as { companies?: unknown }).companies)
      ? (parsed as { companies: unknown[] }).companies
      : null
  if (!list) {
    throw new WatchlistError(`watchlist ${path} must be {"companies": [{"name", "ats", "slug"}, ...]}`, "BAD_COMPANIES_FILE")
  }
  const companies: Company[] = []
  // (ats, slug) is the board identity: a repeated entry would fetch the board
  // twice and emit every posting twice. First entry wins (its display name).
  // Slugs stay case-sensitive here because SmartRecruiters' are.
  const seen = new Set<string>()
  list.forEach((entry, index) => {
    const e = (entry ?? {}) as Record<string, unknown>
    const ats = typeof e.ats === "string" ? e.ats.trim().toLowerCase() : ""
    const slug = typeof e.slug === "string" ? e.slug.trim() : ""
    const name = typeof e.name === "string" && e.name.trim() ? e.name.trim() : slug
    if (!isAts(ats)) {
      throw new WatchlistError(
        `watchlist entry ${index} (${name || "?"}): "ats" must be one of ${ATS_PROVIDERS.join("|")}, got "${ats}"`,
        "BAD_COMPANIES_FILE",
      )
    }
    if (!SLUG_RE.test(slug)) {
      throw new WatchlistError(
        `watchlist entry ${index} (${name || "?"}): "slug" must be the board identifier (letters, digits, - _ .), got "${slug}"`,
        "BAD_COMPANIES_FILE",
      )
    }
    const key = `${ats}:${slug}`
    if (seen.has(key)) return
    seen.add(key)
    companies.push({ name, ats, slug })
  })
  if (companies.length === 0) {
    throw new WatchlistError(`watchlist ${path} lists no companies`, "NO_COMPANIES")
  }
  return companies
}

/** Run `fn` over `items` with at most `limit` in flight; results keep input order. */
export async function mapConcurrent<T, R>(items: T[], limit: number, fn: (item: T) => Promise<R>): Promise<R[]> {
  const results: R[] = new Array(items.length)
  let next = 0
  const workers = Array.from({ length: Math.max(1, Math.min(limit, items.length)) }, async () => {
    while (next < items.length) {
      const i = next++
      results[i] = await fn(items[i])
    }
  })
  await Promise.all(workers)
  return results
}
