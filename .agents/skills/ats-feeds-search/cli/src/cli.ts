#!/usr/bin/env bun
// Self-contained CLI that reads the official public JSON job-board APIs of
// five applicant-tracking systems (Greenhouse, Lever, Ashby, SmartRecruiters,
// Workable) for a watchlist of companies, and emits the repo's standard
// portal-CLI contract. No HTML scraping, no login, no browser, no runtime
// dependencies - it runs anywhere `bun` is available.

import { runSearch, type SearchOpts } from "./commands/search.js"
import { runDetail, type DetailOpts } from "./commands/detail.js"
import { ATS_PROVIDERS, DEFAULT_COMPANIES_PATH, isAts, resolveCompaniesPath, type Ats } from "./helpers.js"

interface Flags {
  _: string[]
  [k: string]: string | boolean | string[]
}

function parseFlags(argv: string[]): Flags {
  const flags: Flags = { _: [] }
  const alias: Record<string, string> = { q: "query", l: "location", n: "limit" }
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (a.startsWith("--") || a.startsWith("-")) {
      const key = alias[a.replace(/^-+/, "")] ?? a.replace(/^-+/, "")
      const next = argv[i + 1]
      if (next === undefined || next.startsWith("-")) {
        flags[key] = true
      } else {
        flags[key] = next
        i++
      }
    } else {
      ;(flags._ as string[]).push(a)
    }
  }
  return flags
}

const HELP = `ats-feeds-cli — search the official job-board APIs of a watchlist of companies
(Greenhouse, Lever, Ashby, SmartRecruiters, Workable). No scraping, no login.

USAGE
  bun run src/cli.ts search [flags]
  bun run src/cli.ts detail <id|url> [--companies <path>] [--format json|plain]

SEARCH FLAGS
  --companies <path>      Watchlist JSON ({"companies":[{"name","ats","slug"},...]}).
                          Default: $ATS_FEEDS_COMPANIES or
                          ${DEFAULT_COMPANIES_PATH}
  --query, -q <text>      Title filter: every word must appear (case-insensitive).
  --location, -l <text>   Location substring; "remote" also matches remote-flagged jobs.
  --days <n>              Posted/updated within the last N days (--jobage is an alias).
  --limit, -n <n>         Cap results emitted (client-side, after sorting newest first).
  --page <n>              1-indexed page of size --limit. Default 1.
  --ats <list>            Only these providers, comma-separated: ${ATS_PROVIDERS.join(",")}
  --no-description        Omit description bodies (cheap discovery pass).
  --format <fmt>          json (default) | table | plain.

DETAIL
  <id|url>                An id from search ("<ats>:<slug>:<id>") or a hosted job URL
                          (boards.greenhouse.io, jobs.lever.co, jobs.ashbyhq.com,
                          jobs.smartrecruiters.com, apply.workable.com).

EXAMPLES
  bun run src/cli.ts search -q "platform engineer" --days 14 --format table
  bun run src/cli.ts search -q "data" -l remote --ats greenhouse,ashby --limit 20 --no-description
  bun run src/cli.ts detail greenhouse:cloudflare:123456 --format plain

Reads only official, public, unauthenticated JSON endpoints - a watchlist feed,
not a search engine. Companies that fail are reported in "errors", never abort the run.
`

// Long-form flag names each command accepts (parseFlags resolves the short
// aliases q/l/n to these before validation). "help"/"h" pass so `search --help`
// still prints usage.
const KNOWN_FLAGS: Record<string, Set<string>> = {
  search: new Set([
    "companies", "query", "location", "days", "jobage", "limit", "page", "ats", "no-description", "format", "help", "h",
  ]),
  detail: new Set(["companies", "format", "help", "h"]),
}

function badArg(message: string): void {
  process.stderr.write(JSON.stringify({ error: message, code: "BAD_ARG" }) + "\n")
}

function stringFlag(v: string | boolean | string[] | undefined): string | undefined {
  return typeof v === "string" ? v : undefined
}

/** Parse a non-negative integer flag; writes BAD_ARG and returns null on failure. */
function intFlag(name: string, raw: string | boolean | string[], min: number): number | null {
  const text = typeof raw === "string" ? raw.trim() : ""
  const val = /^\d+$/.test(text) ? parseInt(text, 10) : NaN
  if (isNaN(val) || val < min) {
    badArg(`--${name} must be an integer >= ${min}, got "${typeof raw === "string" ? raw : ""}"`)
    return null
  }
  return val
}

export async function main(argv: string[] = process.argv.slice(2)): Promise<number> {
  const flags = parseFlags(argv)
  const cmd = (flags._ as string[])[0]

  if (!cmd || flags.help || flags.h) {
    process.stdout.write(HELP)
    return cmd ? 0 : 1
  }

  // Reject unknown flags instead of silently discarding them: a discarded
  // filter changes what the search returns with no error. add-portal.md's
  // contract requires a bogus flag to exit 1 with a JSON error on stderr.
  const knownFlags = KNOWN_FLAGS[cmd]
  if (knownFlags) {
    for (const key of Object.keys(flags)) {
      if (key === "_" || knownFlags.has(key)) continue
      process.stderr.write(
        JSON.stringify({
          error: `unknown flag --${key} for '${cmd}' - flags are never silently ignored, because a discarded filter changes what the search returns; see --help for the supported flags`,
          code: "UNKNOWN_FLAG",
        }) + "\n",
      )
      return 1
    }
  }

  if (cmd === "search") {
    const fmt = stringFlag(flags.format) ?? "json"
    if (!["json", "table", "plain"].includes(fmt)) {
      badArg(`--format must be json, table or plain, got "${fmt}"`)
      return 1
    }
    if (flags.days !== undefined && flags.jobage !== undefined) {
      badArg("--days and --jobage are the same filter; pass only one")
      return 1
    }
    const daysRaw = flags.days ?? flags.jobage
    let days: number | undefined
    if (daysRaw !== undefined) {
      const v = intFlag(flags.days !== undefined ? "days" : "jobage", daysRaw, 0)
      if (v === null) return 1
      days = v
    }
    let limit: number | undefined
    if (flags.limit !== undefined) {
      const v = intFlag("limit", flags.limit, 0)
      if (v === null) return 1
      limit = v
    }
    let page = 1
    if (flags.page !== undefined) {
      const v = intFlag("page", flags.page, 1)
      if (v === null) return 1
      page = v
    }
    const ats: Ats[] = []
    if (flags.ats !== undefined) {
      const raw = stringFlag(flags.ats) ?? ""
      for (const part of raw.split(",").map((s) => s.trim().toLowerCase()).filter(Boolean)) {
        if (!isAts(part)) {
          badArg(`--ats must be a comma-separated list of ${ATS_PROVIDERS.join("|")}, got "${part}"`)
          return 1
        }
        if (!ats.includes(part)) ats.push(part)
      }
      if (ats.length === 0) {
        badArg(`--ats needs at least one of ${ATS_PROVIDERS.join("|")}`)
        return 1
      }
    }
    if (flags.companies !== undefined && typeof flags.companies !== "string") {
      badArg("--companies needs a path")
      return 1
    }
    // A bare `--query` / `--location` (no value) must not degrade into "no
    // filter": that silently widens the search, the very thing UNKNOWN_FLAG
    // guards against for misspelt flags.
    for (const name of ["query", "location"] as const) {
      if (flags[name] !== undefined && typeof flags[name] !== "string") {
        badArg(`--${name} needs a value`)
        return 1
      }
    }

    const opts: SearchOpts = {
      companiesPath: resolveCompaniesPath(stringFlag(flags.companies)),
      query: stringFlag(flags.query),
      location: stringFlag(flags.location),
      days,
      limit,
      page,
      ats,
      includeDescription: flags["no-description"] === undefined,
      format: fmt as SearchOpts["format"],
    }
    return runSearch(opts)
  }

  if (cmd === "detail") {
    const ref = (flags._ as string[])[1]
    if (!ref) {
      process.stderr.write(JSON.stringify({ error: "detail requires an <id|url>", code: "NO_ID" }) + "\n")
      return 1
    }
    const fmt = stringFlag(flags.format) ?? "json"
    if (!["json", "plain"].includes(fmt)) {
      badArg(`--format must be json or plain, got "${fmt}"`)
      return 1
    }
    if (flags.companies !== undefined && typeof flags.companies !== "string") {
      badArg("--companies needs a path")
      return 1
    }
    const opts: DetailOpts = {
      ref,
      companiesPath: resolveCompaniesPath(stringFlag(flags.companies)),
      format: fmt as DetailOpts["format"],
    }
    return runDetail(opts)
  }

  process.stderr.write(JSON.stringify({ error: `Unknown command "${cmd}"`, code: "BAD_CMD" }) + "\n")
  return 1
}

if (import.meta.main) {
  main()
    .then((code) => process.exit(code))
    .catch((e) => {
      process.stderr.write(
        JSON.stringify({ error: e instanceof Error ? e.message : String(e), code: "INTERNAL_ERROR" }) + "\n",
      )
      process.exit(1)
    })
}
