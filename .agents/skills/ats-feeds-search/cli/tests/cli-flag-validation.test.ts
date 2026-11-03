import { describe, expect, test } from "bun:test"
import { runCLI } from "./helpers"
import { tempWatchlist } from "./fixtures"

// These spawn the real CLI process. Every case is rejected before any network
// request is made (bad flags, bad values, missing watchlist), so the suite
// stays offline.

function parsedStderr(stderr: string): { error?: string; code?: string } {
  try {
    return JSON.parse(stderr)
  } catch {
    return {}
  }
}

describe("ats-feeds CLI flag validation (subprocess)", () => {
  test("no command prints help and exits 1", async () => {
    const result = await runCLI([])
    expect(result.exitCode).toBe(1)
    expect(result.stdout).toContain("USAGE")
  })

  test("a bogus --flag exits 1 with UNKNOWN_FLAG on stderr and nothing on stdout", async () => {
    const result = await runCLI(["search", "--bogus-flag", "x", "--companies", tempWatchlist()])
    expect(result.exitCode).toBe(1)
    expect(result.stdout).toBe("")
    const err = parsedStderr(result.stderr)
    expect(err.code).toBe("UNKNOWN_FLAG")
    expect(err.error).toContain("--bogus-flag")
  })

  test("--days must be a non-negative integer", async () => {
    for (const bad of ["foo", "7.5"]) {
      const result = await runCLI(["search", "--companies", tempWatchlist(), "--days", bad])
      expect(result.exitCode).toBe(1)
      expect(parsedStderr(result.stderr).code).toBe("BAD_ARG")
      expect(parsedStderr(result.stderr).error).toMatch(/days/)
    }
  })

  test("a bare --query or --location (no value) is BAD_ARG, never a silently widened search", async () => {
    for (const flag of ["--query", "-q", "--location", "-l"]) {
      const result = await runCLI(["search", "--companies", tempWatchlist(), flag, "--days", "7"])
      expect(result.exitCode).toBe(1)
      expect(result.stdout).toBe("")
      const err = parsedStderr(result.stderr)
      expect(err.code).toBe("BAD_ARG")
      expect(err.error).toMatch(/--(query|location) needs a value/)
    }
  })

  test("--days and --jobage together are rejected", async () => {
    const result = await runCLI(["search", "--companies", tempWatchlist(), "--days", "7", "--jobage", "7"])
    expect(result.exitCode).toBe(1)
    expect(parsedStderr(result.stderr).code).toBe("BAD_ARG")
  })

  test("--limit and --page reject non-numbers; --page 0 is rejected", async () => {
    expect(parsedStderr((await runCLI(["search", "--companies", tempWatchlist(), "--limit", "xyz"])).stderr).code).toBe("BAD_ARG")
    expect(parsedStderr((await runCLI(["search", "--companies", tempWatchlist(), "--page", "abc"])).stderr).code).toBe("BAD_ARG")
    expect(parsedStderr((await runCLI(["search", "--companies", tempWatchlist(), "--page", "0"])).stderr).code).toBe("BAD_ARG")
  })

  test("--ats rejects an unknown provider", async () => {
    const result = await runCLI(["search", "--companies", tempWatchlist(), "--ats", "greenhouse,taleo"])
    expect(result.exitCode).toBe(1)
    const err = parsedStderr(result.stderr)
    expect(err.code).toBe("BAD_ARG")
    expect(err.error).toContain("taleo")
  })

  test("--format rejects an unknown format", async () => {
    const result = await runCLI(["search", "--companies", tempWatchlist(), "--format", "xml"])
    expect(result.exitCode).toBe(1)
    expect(parsedStderr(result.stderr).code).toBe("BAD_ARG")
  })

  test("a missing watchlist exits 1 with NO_COMPANIES_FILE naming the example to copy", async () => {
    const result = await runCLI(["search", "--companies", "/nonexistent/dir/companies.json"])
    expect(result.exitCode).toBe(1)
    const err = parsedStderr(result.stderr)
    expect(err.code).toBe("NO_COMPANIES_FILE")
    expect(err.error).toContain("companies.example.json")
  })

  test("an --ats filter that matches no watchlist company exits 1 with NO_COMPANIES", async () => {
    const path = tempWatchlist({ companies: [{ name: "Only", ats: "lever", slug: "only" }] })
    const result = await runCLI(["search", "--companies", path, "--ats", "ashby"])
    expect(result.exitCode).toBe(1)
    expect(parsedStderr(result.stderr).code).toBe("NO_COMPANIES")
  })

  test("detail without an id exits 1 with NO_ID", async () => {
    const result = await runCLI(["detail"])
    expect(result.exitCode).toBe(1)
    expect(parsedStderr(result.stderr).code).toBe("NO_ID")
  })

  test("an unknown command exits 1 with BAD_CMD", async () => {
    const result = await runCLI(["frobnicate"])
    expect(result.exitCode).toBe(1)
    expect(parsedStderr(result.stderr).code).toBe("BAD_CMD")
  })
})
