import { afterEach, describe, expect, test } from "bun:test"
import { decodeHtmlEntities, escapedHtmlToText, fetchJson, htmlToText, loadCompanies, setFetch, setSleep, toIsoDate, MAX_TRIES } from "../src/helpers"
import { tempWatchlist } from "./fixtures"

afterEach(() => {
  setFetch(null)
  setSleep(null)
})

describe("htmlToText", () => {
  test("strips tags, decodes entities, keeps paragraph and list structure", () => {
    const html = "<h2>About</h2><p>We build &amp; ship &lt;fast&gt;.</p><ul><li>Go</li><li>Rust &#x2014; optional</li></ul><p>Caf&eacute;<br>Berlin</p>"
    expect(htmlToText(html)).toBe("About\n\nWe build & ship <fast>.\n\n• Go\n• Rust — optional\n\nCafé\nBerlin")
  })

  test("drops script/style blocks and returns null for empty input", () => {
    expect(htmlToText("<style>p{}</style><script>x()</script><p> </p>")).toBeNull()
    expect(htmlToText("")).toBeNull()
    expect(htmlToText(null)).toBeNull()
  })

  test("escapedHtmlToText unescapes Greenhouse's double-encoded content once", () => {
    expect(escapedHtmlToText("&lt;p&gt;A &amp;amp; B&lt;/p&gt;")).toBe("A & B")
    expect(escapedHtmlToText("<p>Plain &amp; simple</p>")).toBe("Plain & simple")
  })

  test("decodeHtmlEntities handles numeric, hex, named and leaves unknown names alone", () => {
    expect(decodeHtmlEntities("&#65;&#x42;&nbsp;&rsquo;&unknown;&amp;lt;")).toBe("AB ’&unknown;&lt;")
  })
})

describe("toIsoDate", () => {
  test("accepts ISO strings, epoch milliseconds, epoch seconds and rejects junk", () => {
    expect(toIsoDate("2026-10-29T15:04:05.000Z")).toBe("2026-10-29")
    expect(toIsoDate("2026-10-29T23:30:00-05:00")).toBe("2026-10-30")
    const ms = Date.UTC(2026, 9, 29, 15, 4, 5)
    expect(toIsoDate(ms)).toBe("2026-10-29")
    expect(toIsoDate(Math.floor(ms / 1000))).toBe("2026-10-29")
    expect(toIsoDate(String(ms))).toBe("2026-10-29")
    expect(toIsoDate("yesterday")).toBeNull()
    expect(toIsoDate(null)).toBeNull()
    expect(toIsoDate({})).toBeNull()
  })
})

describe("fetchJson retry/backoff", () => {
  function stub(responses: Array<() => Response>): { calls: number } {
    const state = { calls: 0 }
    setFetch(async () => responses[Math.min(state.calls++, responses.length - 1)]())
    setSleep(async () => {})
    return state
  }

  test("retries a 429 and succeeds on the next attempt", async () => {
    const state = stub([() => new Response("", { status: 429 }), () => new Response('{"ok":true}', { status: 200 })])
    expect(await fetchJson("https://api.example/x")).toEqual({ ok: true })
    expect(state.calls).toBe(2)
  })

  test("returns null on 404 without retrying", async () => {
    const state = stub([() => new Response("", { status: 404 })])
    expect(await fetchJson("https://api.example/x")).toBeNull()
    expect(state.calls).toBe(1)
  })

  test(`gives up after ${MAX_TRIES} tries on persistent 5xx`, async () => {
    const state = stub([() => new Response("", { status: 503 })])
    await expect(fetchJson("https://api.example/x")).rejects.toThrow(/503/)
    expect(state.calls).toBe(MAX_TRIES)
  })

  test("backs off between retries with growing delays", async () => {
    const delays: number[] = []
    let calls = 0
    setFetch(async () => new Response("", { status: calls++ < 2 ? 500 : 200, headers: {} }).status === 200 ? new Response("{}") : new Response("", { status: 500 }))
    setSleep(async (ms) => void delays.push(ms))
    await fetchJson("https://api.example/x")
    expect(delays).toHaveLength(2)
    expect(delays[0]).toBeGreaterThanOrEqual(500)
    expect(delays[1]).toBeGreaterThanOrEqual(1000)
  })

  test("a 403 or other non-retryable status throws without retrying", async () => {
    const state = stub([() => new Response("", { status: 403 })])
    await expect(fetchJson("https://api.example/x")).rejects.toThrow(/403/)
    expect(state.calls).toBe(1)
  })

  test("a non-JSON 200 body is reported as such", async () => {
    stub([() => new Response("<html>login</html>", { status: 200 })])
    await expect(fetchJson("https://api.example/x")).rejects.toThrow(/non-JSON/)
  })

  test("passes an AbortSignal timeout and the honest User-Agent to fetch", async () => {
    let init: RequestInit | undefined
    setFetch(async (_url, i) => {
      init = i
      return new Response("{}")
    })
    await fetchJson("https://api.example/x")
    expect(init?.signal).toBeInstanceOf(AbortSignal)
    expect((init?.headers as Record<string, string>)["User-Agent"]).toBe("Mozilla/5.0 (compatible; ats-feeds-search-cli/1.0)")
  })

  test("a connection failure is reported as unreachable", async () => {
    setFetch(async () => {
      throw new Error("ECONNREFUSED")
    })
    await expect(fetchJson("https://api.example/x")).rejects.toThrow(/could not reach api.example/)
  })
})

describe("loadCompanies", () => {
  test("accepts the documented shape and normalises ats case", () => {
    const path = tempWatchlist({ companies: [{ name: "X", ats: "Greenhouse", slug: "x-co" }] })
    expect(loadCompanies(path)).toEqual([{ name: "X", ats: "greenhouse", slug: "x-co" }])
  })

  test("drops a repeated (ats, slug) pair, first entry wins, slug case preserved", () => {
    const path = tempWatchlist({
      companies: [
        { name: "Visa", ats: "smartrecruiters", slug: "Visa" },
        { name: "Visa dup", ats: "SmartRecruiters", slug: "Visa" },
        { name: "visa lower", ats: "smartrecruiters", slug: "visa" },
      ],
    })
    expect(loadCompanies(path)).toEqual([
      { name: "Visa", ats: "smartrecruiters", slug: "Visa" },
      { name: "visa lower", ats: "smartrecruiters", slug: "visa" },
    ])
  })

  test("rejects a missing file, bad JSON, unknown ats, unsafe slug and an empty list with distinct codes", () => {
    expect(() => loadCompanies("/nonexistent/companies.json")).toThrow(expect.objectContaining({ code: "NO_COMPANIES_FILE" }))
    const dir = tempWatchlist().replace(/companies\.json$/, "")
    const bad = `${dir}bad.json`
    require("fs").writeFileSync(bad, "{not json")
    expect(() => loadCompanies(bad)).toThrow(expect.objectContaining({ code: "BAD_COMPANIES_FILE" }))
    expect(() => loadCompanies(tempWatchlist({ companies: [{ name: "X", ats: "taleo", slug: "x" }] }))).toThrow(/ats/)
    expect(() => loadCompanies(tempWatchlist({ companies: [{ name: "X", ats: "lever", slug: "../x" }] }))).toThrow(/slug/)
    expect(() => loadCompanies(tempWatchlist({ companies: [] }))).toThrow(expect.objectContaining({ code: "NO_COMPANIES" }))
  })
})
