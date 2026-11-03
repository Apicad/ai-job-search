---
framework_version: 1.1.0
---

# Agent Guidelines: AI Job Search

This workspace is structured to manage job search activities, scraper tools, CVs, cover letters, and interview preparation.

## Thin-Pointer Design (Single Source of Truth)

To prevent duplication and configuration drift across different AI agent frameworks (Claude Code, Google Antigravity, Codex, Cursor, Gemini CLI, etc.), this workspace uses a unified thin-pointer design. All agent runtimes should load the canonical specifications and candidate profiles from the files and directories below:

1. **Personal Candidate Profile:**
   - The candidate profile, contact details, education, and target preferences are defined in [CLAUDE.md](CLAUDE.md) and the individual profile methodology files under [.claude/skills/job-application-assistant/](.claude/skills/job-application-assistant/) (specifically `01-*.md` etc.).
2. **Canonical Workflow Specifications:**
   - The step-by-step instructions and triggers for tasks (setup, scrape, rank, apply, upskill, interview) are defined in the [.claude/](.claude/) directory (specifically under `.claude/skills/` and `.claude/commands/`).
   - Do not duplicate these rules or specifications. Treat `.claude/` files as the single source of truth.
3. **Portal Search Skills:**
   - Job-portal search CLIs live under [.agents/skills/](.agents/skills/) in the portable Agent Skills format (with a `SKILL.md` per portal). Codex and Antigravity discover these automatically; the `/scrape` workflow in [.claude/skills/job-scraper/](.claude/skills/job-scraper/) orchestrates them.

## Model Tiers per Stage

The workflow specs name a **tier**, not a model, wherever they dispatch a subagent. The
tier is passed as the Agent tool's `model:` hint and is meant to be routed by whatever
sits in front of the API (an `ANTHROPIC_BASE_URL` proxy such as OmniRoute, a model
alias, or nothing at all). Override routing in that layer, not by editing the specs.

| Stage | Tier hint | Why |
|-------|-----------|-----|
| `/rank` scoring agents (triage of stored posting text against a rubric) | `sonnet` | Bounded exposure: the shortlist is re-evaluated in full by `/apply`. Gated by `tools/eval_rank.py` (see `/rank` Step 2). |
| `/apply` research-only agent (company discovery that fills `company_research/*.json`) | `sonnet` | Discovery only; every claim is re-fetched from its recorded URL before use. |
| `/apply` reviewer critique | default (unset) | Grounding audit and voice critique are the quality-bearing steps. |
| `/apply` drafter (main context) | default (unset) | Writes the artifacts the user sends. |
| `/scrape` | none | No subagents: portal CLIs run directly and `tools/jobs_db.py` handles state. |

Changing a tier is a methodology change: run `tools/eval_rank.py compare` against a saved
golden run before adopting a cheaper route for scoring, and never route the drafter or
the critique to a tier that has not passed the same comparison on real drafts.
