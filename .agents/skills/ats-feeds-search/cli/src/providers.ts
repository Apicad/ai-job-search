// One adapter per ATS: how to list a company's open postings, how to fetch a
// single posting, and how to recognise that ATS's hosted job URLs. Every
// adapter normalises into the shared JobResult/JobDetail contract, so the
// commands never see provider-specific shapes. All reads are defensive: a
// field the API omits becomes null rather than a crash.

import {
  escapedHtmlToText,
  fetchJson,
  htmlToText,
  joinLocation,
  makeId,
  toIsoDate,
  type Ats,
  type Company,
  type JobDetail,
  type JobResult,
} from "./helpers.js"

export interface Provider {
  ats: Ats
  /** All currently open postings for one company board. */
  list(company: Company): Promise<JobResult[]>
  /** One posting by its provider id; null when it no longer exists. */
  detail(company: Company, providerId: string): Promise<JobDetail | null>
  /** Parse `{slug, id}` out of a hosted job URL this ATS serves, or null. */
  parseUrl(url: string): { slug: string; id: string } | null
}

type Dict = Record<string, unknown>

const str = (v: unknown): string | null => (typeof v === "string" && v.trim() ? v.trim() : null)
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null)
const obj = (v: unknown): Dict => (v && typeof v === "object" && !Array.isArray(v) ? (v as Dict) : {})
const arr = (v: unknown): unknown[] => (Array.isArray(v) ? v : [])
const bool = (v: unknown): boolean | null => (typeof v === "boolean" ? v : null)

// ---------------------------------------------------------------------------
// Greenhouse - https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true

export const GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards"

function greenhouseJob(company: Company, j: Dict, withContent: boolean): JobResult {
  const id = str(j.id) ?? String(num(j.id) ?? "")
  const location = str(obj(j.location).name)
  const offices = arr(j.offices)
    .map((o) => str(obj(o).name))
    .filter((n): n is string => !!n)
  const url = str(j.absolute_url) ?? `https://boards.greenhouse.io/${company.slug}/jobs/${id}`
  return {
    title: str(j.title) ?? "(untitled)",
    company: company.name,
    location: location ?? joinLocation(offices),
    date: toIsoDate(j.updated_at ?? j.first_published),
    url,
    id: makeId("greenhouse", company.slug, id),
    ats: "greenhouse",
    description: withContent ? escapedHtmlToText(str(j.content)) : null,
    deadline: null,
    remote: location ? /remote/i.test(location) || null : null,
  }
}

const greenhouse: Provider = {
  ats: "greenhouse",
  async list(company) {
    const data = await fetchJson<Dict>(`${GREENHOUSE_API}/${encodeURIComponent(company.slug)}/jobs?content=true`)
    if (!data) throw new Error(`greenhouse board "${company.slug}" not found (404)`)
    return arr(data.jobs).map((j) => greenhouseJob(company, obj(j), true))
  },
  async detail(company, providerId) {
    const j = await fetchJson<Dict>(`${GREENHOUSE_API}/${encodeURIComponent(company.slug)}/jobs/${encodeURIComponent(providerId)}`)
    if (!j || !j.id) return null
    const base = greenhouseJob(company, j, true)
    return { ...base, apply_url: base.url }
  },
  parseUrl(url) {
    // Hosted boards (boards.greenhouse.io, job-boards.greenhouse.io, EU
    // variants) and the embed form (embed/job_app?for=<slug>&token=<id>).
    const hosted = url.match(/^https?:\/\/(?:boards|job-boards)\.(?:eu\.)?greenhouse\.io\/([A-Za-z0-9_.-]+)\/jobs\/(\d+)/i)
    if (hosted) return { slug: hosted[1], id: hosted[2] }
    const embed = url.match(/^https?:\/\/boards\.(?:eu\.)?greenhouse\.io\/embed\/job_app\?(.*)$/i)
    if (embed) {
      const params = new URLSearchParams(embed[1])
      const slug = params.get("for")
      const id = params.get("token")
      if (slug && id && /^\d+$/.test(id) && /^[A-Za-z0-9_.-]+$/.test(slug)) return { slug, id }
    }
    return null
  },
}

// ---------------------------------------------------------------------------
// Lever - https://api.lever.co/v0/postings/{slug}?mode=json

export const LEVER_API = "https://api.lever.co/v0/postings"

function leverDescription(j: Dict): string | null {
  const parts: string[] = []
  const head = str(j.descriptionPlain) ?? htmlToText(str(j.description))
  if (head) parts.push(head)
  for (const raw of arr(j.lists)) {
    const l = obj(raw)
    const heading = str(l.text)
    const body = htmlToText(str(l.content))
    if (heading) parts.push(heading)
    if (body) parts.push(body)
  }
  const tail = str(j.additionalPlain) ?? htmlToText(str(j.additional))
  if (tail) parts.push(tail)
  return parts.length ? parts.join("\n\n") : null
}

function leverJob(company: Company, j: Dict): JobResult {
  const id = str(j.id) ?? ""
  const cats = obj(j.categories)
  // workplaceType is remote | hybrid | on-site | unspecified; "unspecified"
  // says nothing, so fall back to the location text like the other vendors.
  const workplace = str(j.workplaceType)?.toLowerCase()
  const workplaceKnown = workplace !== undefined && workplace !== "unspecified"
  const location = str(cats.location) ?? joinLocation(arr(cats.allLocations).map(str))
  return {
    title: str(j.text) ?? "(untitled)",
    company: company.name,
    location,
    date: toIsoDate(j.createdAt),
    url: str(j.hostedUrl) ?? `https://jobs.lever.co/${company.slug}/${id}`,
    id: makeId("lever", company.slug, id),
    ats: "lever",
    description: leverDescription(j),
    deadline: null,
    remote: workplaceKnown ? workplace === "remote" : location ? /remote/i.test(location) || null : null,
  }
}

const lever: Provider = {
  ats: "lever",
  async list(company) {
    const data = await fetchJson<unknown>(`${LEVER_API}/${encodeURIComponent(company.slug)}?mode=json`)
    if (data === null) throw new Error(`lever board "${company.slug}" not found (404)`)
    return arr(data).map((j) => leverJob(company, obj(j)))
  },
  async detail(company, providerId) {
    const j = await fetchJson<Dict>(`${LEVER_API}/${encodeURIComponent(company.slug)}/${encodeURIComponent(providerId)}`)
    if (!j || !j.id) return null
    const base = leverJob(company, j)
    return { ...base, apply_url: str(j.applyUrl) ?? `${base.url.replace(/\/$/, "")}/apply` }
  },
  parseUrl(url) {
    const m = url.match(/^https?:\/\/jobs\.(?:eu\.)?lever\.co\/([A-Za-z0-9_.-]+)\/([0-9a-f]{8}-[0-9a-f-]{27,})/i)
    return m ? { slug: m[1], id: m[2] } : null
  },
}

// ---------------------------------------------------------------------------
// Ashby - https://api.ashbyhq.com/posting-api/job-board/{slug}

export const ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board"

function ashbyJob(company: Company, j: Dict): JobResult {
  const id = str(j.id) ?? ""
  const secondary = arr(j.secondaryLocations).map((l) => str(obj(l).location))
  return {
    title: str(j.title) ?? "(untitled)",
    company: company.name,
    location: joinLocation([str(j.location), ...secondary]),
    date: toIsoDate(j.publishedAt),
    url: str(j.jobUrl) ?? `https://jobs.ashbyhq.com/${company.slug}/${id}`,
    id: makeId("ashby", company.slug, id),
    ats: "ashby",
    description: str(j.descriptionPlain) ?? htmlToText(str(j.descriptionHtml)),
    deadline: null,
    remote: bool(j.isRemote),
  }
}

async function ashbyBoard(company: Company): Promise<Dict[]> {
  const data = await fetchJson<Dict>(`${ASHBY_API}/${encodeURIComponent(company.slug)}?includeCompensation=true`)
  if (!data) throw new Error(`ashby board "${company.slug}" not found (404)`)
  return arr(data.jobs).map(obj)
}

const ashby: Provider = {
  ats: "ashby",
  async list(company) {
    return (await ashbyBoard(company)).filter((j) => j.isListed !== false).map((j) => ashbyJob(company, j))
  },
  async detail(company, providerId) {
    // Ashby's public posting API has no per-job endpoint: the board carries
    // every posting with its full description, so find the id in it.
    const j = (await ashbyBoard(company)).find((job) => str(job.id) === providerId)
    if (!j) return null
    const base = ashbyJob(company, j)
    return { ...base, apply_url: str(j.applyUrl) ?? `${base.url.replace(/\/$/, "")}/application` }
  },
  parseUrl(url) {
    const m = url.match(/^https?:\/\/jobs\.ashbyhq\.com\/([A-Za-z0-9_.-]+)\/([0-9a-f]{8}-[0-9a-f-]{27,})/i)
    return m ? { slug: m[1], id: m[2] } : null
  },
}

// ---------------------------------------------------------------------------
// SmartRecruiters - https://api.smartrecruiters.com/v1/companies/{slug}/postings

export const SMARTRECRUITERS_API = "https://api.smartrecruiters.com/v1/companies"
/** Page size (the API maximum) and a cap on pages so one huge employer cannot run away. */
const SR_PAGE = 100
const SR_MAX_PAGES = 5

function smartrecruitersJob(company: Company, j: Dict): JobResult {
  const id = str(j.id) ?? ""
  const loc = obj(j.location)
  const remote = bool(loc.remote)
  return {
    title: str(j.name) ?? "(untitled)",
    company: str(obj(j.company).name) ?? company.name,
    location: joinLocation([str(loc.city), str(loc.region), str(loc.country)]) ?? (remote ? "Remote" : null),
    date: toIsoDate(j.releasedDate),
    url: str(j.postingUrl) ?? `https://jobs.smartrecruiters.com/${company.slug}/${id}`,
    id: makeId("smartrecruiters", company.slug, id),
    ats: "smartrecruiters",
    // The list endpoint carries no description; `detail` fills it in.
    description: null,
    deadline: null,
    remote,
  }
}

function smartrecruitersDescription(j: Dict): string | null {
  const sections = obj(obj(j.jobAd).sections)
  const parts: string[] = []
  for (const key of ["companyDescription", "jobDescription", "qualifications", "additionalInformation"]) {
    const s = obj(sections[key])
    const title = str(s.title)
    const text = htmlToText(str(s.text))
    if (!text) continue
    parts.push(title ? `${title}\n${text}` : text)
  }
  return parts.length ? parts.join("\n\n") : null
}

const smartrecruiters: Provider = {
  ats: "smartrecruiters",
  async list(company) {
    const out: JobResult[] = []
    for (let page = 0; page < SR_MAX_PAGES; page++) {
      const url = `${SMARTRECRUITERS_API}/${encodeURIComponent(company.slug)}/postings?limit=${SR_PAGE}&offset=${page * SR_PAGE}`
      const data = await fetchJson<Dict>(url)
      if (!data) {
        if (page === 0) throw new Error(`smartrecruiters company "${company.slug}" not found (404)`)
        break
      }
      const content = arr(data.content).map(obj)
      out.push(...content.map((j) => smartrecruitersJob(company, j)))
      const total = num(data.totalFound) ?? out.length
      if (content.length < SR_PAGE || out.length >= total) break
    }
    return out
  },
  async detail(company, providerId) {
    const j = await fetchJson<Dict>(`${SMARTRECRUITERS_API}/${encodeURIComponent(company.slug)}/postings/${encodeURIComponent(providerId)}`)
    if (!j || !j.id) return null
    const base = smartrecruitersJob(company, j)
    return {
      ...base,
      description: smartrecruitersDescription(j),
      apply_url: str(j.applyUrl) ?? base.url,
    }
  },
  parseUrl(url) {
    const m = url.match(/^https?:\/\/jobs\.smartrecruiters\.com\/([A-Za-z0-9_.-]+)\/(\d+)/i)
    return m ? { slug: m[1], id: m[2] } : null
  },
}

// ---------------------------------------------------------------------------
// Workable (experimental) - https://apply.workable.com/api/v3/accounts/{slug}/jobs

export const WORKABLE_API = "https://apply.workable.com/api"
const WORKABLE_MAX_PAGES = 5

function workableJob(company: Company, j: Dict): JobResult {
  const shortcode = str(j.shortcode) ?? str(j.id) ?? ""
  const loc = obj(j.location)
  const remote = bool(j.remote) ?? (str(loc.workplaceType)?.toLowerCase() === "remote" ? true : null)
  return {
    title: str(j.title) ?? "(untitled)",
    company: company.name,
    location: joinLocation([str(loc.city), str(loc.region), str(loc.country)]) ?? (remote ? "Remote" : null),
    date: toIsoDate(j.published ?? j.created),
    url: str(j.url) ?? `https://apply.workable.com/${company.slug}/j/${shortcode}/`,
    id: makeId("workable", company.slug, shortcode),
    ats: "workable",
    description: null,
    deadline: null,
    remote,
  }
}

const workable: Provider = {
  ats: "workable",
  async list(company) {
    const out: JobResult[] = []
    let token: string | null = null
    for (let page = 0; page < WORKABLE_MAX_PAGES; page++) {
      const body: Dict = { query: "", location: [], department: [], worktype: [], remote: [] }
      if (token) body.token = token
      const data = await fetchJson<Dict>(`${WORKABLE_API}/v3/accounts/${encodeURIComponent(company.slug)}/jobs`, {
        method: "POST",
        body,
      })
      if (!data) {
        if (page === 0) throw new Error(`workable account "${company.slug}" not found (404)`)
        break
      }
      const results = arr(data.results).map(obj)
      out.push(...results.map((j) => workableJob(company, j)))
      token = str(data.nextPage)
      if (!token || results.length === 0) break
    }
    return out
  },
  async detail(company, providerId) {
    const j = await fetchJson<Dict>(`${WORKABLE_API}/v2/accounts/${encodeURIComponent(company.slug)}/jobs/${encodeURIComponent(providerId)}`)
    if (!j || (!j.title && !j.shortcode)) return null
    const base = workableJob(company, { shortcode: providerId, ...j })
    const parts = [htmlToText(str(j.description)), htmlToText(str(j.requirements)), htmlToText(str(j.benefits))].filter(
      (p): p is string => !!p,
    )
    return {
      ...base,
      description: parts.length ? parts.join("\n\n") : null,
      apply_url: str(j.applyUrl) ?? `${base.url.replace(/\/$/, "")}/apply/`,
    }
  },
  parseUrl(url) {
    const m = url.match(/^https?:\/\/apply\.workable\.com\/([A-Za-z0-9_.-]+)\/j\/([A-Za-z0-9]+)/i)
    return m ? { slug: m[1], id: m[2] } : null
  },
}

export const PROVIDERS: Record<Ats, Provider> = { greenhouse, lever, ashby, smartrecruiters, workable }

/** Resolve `<ats>:<slug>:<id>` or a hosted job URL into its parts. */
export function parseJobRef(input: string): { ats: Ats; slug: string; id: string } | null {
  const trimmed = input.trim()
  const m = trimmed.match(/^([a-z]+):([A-Za-z0-9_.-]+):(.+)$/)
  if (m && (m[1] in PROVIDERS)) return { ats: m[1] as Ats, slug: m[2], id: m[3] }
  if (/^https?:\/\//i.test(trimmed)) {
    for (const provider of Object.values(PROVIDERS)) {
      const parsed = provider.parseUrl(trimmed)
      if (parsed) return { ats: provider.ats, ...parsed }
    }
  }
  return null
}
