# Engineering Impact Dashboard — Full Implementation Plan

## What We're Building

A single-page dashboard that answers "who are the top 5 most impactful engineers at PostHog in the last 90 days, and why?" We pull data from GitHub's API, enrich it with LLM classification, score engineers deterministically, and display everything with full transparency.

---

## PHASE 1: Data Gathering (Minutes 0–25)

Everything comes from the GitHub API against the `PostHog/posthog` repo. You need a Personal Access Token (github.com/settings/tokens, `public_repo` scope). Use the GraphQL API where possible — fewer requests, richer data per call.

### 1a. Merged Pull Requests

**API source:** GitHub GraphQL API — `repository.pullRequests` with `states: MERGED`

**Filter:** only PRs where `mergedAt` is within the last 90 days. Exclude any author whose login contains `[bot]` or matches known bots like `dependabot`, `github-actions`, `codecov`.

**Fields you extract per PR:**
- `author.login` — who wrote it
- `title` — PR title
- `body` — PR description text (truncate to ~500 chars for LLM use later)
- `createdAt` — when the PR was opened
- `mergedAt` — when it was merged
- `additions` — lines added
- `deletions` — lines removed
- `changedFiles` — number of files changed
- `labels` — list of label names (e.g., "bug", "feature", "infrastructure")
- `files` — list of changed file paths (e.g., `frontend/src/scenes/insights/`, `plugin-server/src/worker/`)
- `closingIssuesReferences` — issues this PR closes (GraphQL gives this directly)

**Pagination:** PRs come in pages of 100. PostHog is very active — expect 500+ merged PRs in 90 days. Handle pagination with cursors.

**Output:** `data/raw_prs.json`

### 1b. Reviews and Review Comments

**API source:** For each merged PR, fetch `reviews` and `reviewThreads` via GraphQL (nested under the PR query so you can get them in the same call).

**Fields you extract per review:**
- `author.login` — who reviewed
- `state` — APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED
- `submittedAt` — when
- `body` — review text (for counting substantive vs empty reviews)

**Fields per review comment:**
- `author.login`
- `createdAt`
- `body`

**Why this matters:** This is how you measure collaboration. "Reviews given" means this person reviewed someone else's PR. "Review comments written" means they left substantive inline feedback. Both come directly from this data.

**Output:** `data/raw_reviews.json`

### 1c. Closed Issues

**API source:** GitHub GraphQL API — `repository.issues` with `states: CLOSED`, filtered to `closedAt` within last 90 days.

**Fields you extract per issue:**
- `number` — issue number
- `title` — issue title
- `labels` — label names (bug, enhancement, etc.)
- `author.login` — who opened the issue
- `closedAt` — when it was closed
- `stateReason` — COMPLETED vs NOT_PLANNED (only count COMPLETED)
- The closer — this is trickier. GitHub doesn't give a direct "closedBy" in GraphQL reliably. You get this from the issue's `timelineItems` looking for a `ClosedEvent` which has an `actor.login`. Alternatively, if a PR closes the issue, the PR author gets credit.

**Why this matters:** Closing issues (especially bugs) is a direct ownership and follow-through signal. An engineer who files a bug, writes the fix PR, and closes the issue is demonstrating full ownership. This data also lets you distinguish "bug slayers" from "feature builders."

**Output:** `data/raw_issues.json`

### Bot Filtering

After fetching, filter out any author matching:
- Login ends with `[bot]`
- Login is exactly: `dependabot`, `github-actions`, `posthog-bot`, `codecov-commenter`
- Any author with 50+ merged PRs and zero reviews given (likely automation)

---

## PHASE 2: Compute Raw Metrics Per Engineer (Minutes 25–40)

Load the three JSON files into pandas DataFrames. Group by engineer and compute these metrics. Every single number shown on the dashboard traces back to one of these.

### Product / Execution Metrics

| Metric | Source | How It's Computed |
|---|---|---|
| `merged_prs` | raw_prs.json | Count of merged PRs authored by this engineer |
| `capped_lines_changed` | raw_prs.json | For each PR: `min(additions + deletions, 1000)`. Then sum across all their PRs. The cap prevents one massive auto-generated PR from inflating the score. |
| `issues_closed` | raw_issues.json | Count of issues where this engineer is the closer (from ClosedEvent actor) AND stateReason = COMPLETED |
| `bugfix_prs` | raw_prs.json | Count of their merged PRs that have a "bug" label |

### Ownership / Autonomy Metrics

| Metric | Source | How It's Computed |
|---|---|---|
| `unique_dirs_touched` | raw_prs.json → files | For each of their PRs, extract the top-level directory from each changed file path (e.g., `frontend/`, `plugin-server/`, `posthog/api/`). Count distinct directories across all their PRs. |
| `complex_prs` | raw_prs.json | Count of their merged PRs where `changedFiles >= 10` AND the PR received 3+ review comments. Proxy for cross-cutting, non-trivial work. |
| `self_opened_issues_closed` | raw_issues.json | Count of issues where the engineer both opened AND closed the issue. Signal of self-directed problem identification and resolution. |

### Collaboration / Stewardship Metrics

| Metric | Source | How It's Computed |
|---|---|---|
| `reviews_given` | raw_reviews.json | Count of reviews this engineer submitted on OTHER people's PRs (exclude self-reviews). |
| `review_comments_written` | raw_reviews.json | Count of individual review comments this engineer wrote on other people's PRs. Only count comments with `body.length > 10` to exclude empty/trivial ones. |
| `distinct_people_reviewed` | raw_reviews.json | Number of unique PR authors this engineer reviewed. Measures how broadly they support the team. |

**Output:** `data/engineer_raw_metrics.json` — one record per engineer with all the above fields.

---

## PHASE 3: LLM Enrichment (Minutes 40–60)

The LLM does two jobs: classify PRs and summarize engineers. It never scores or ranks anyone.

### 3a. PR-Level Classification

**What you send to the LLM per PR:**
- `title`
- `body` (truncated to 500 chars)
- `labels`
- `changed file paths` (list)
- `additions` and `deletions` counts

**What the LLM returns per PR (structured JSON):**
- `type`: one of `feature`, `bugfix`, `refactor`, `infra`, `docs`, `chore`
- `impact_level`: `high`, `medium`, or `low` (how much this affects PostHog end users)
- `area`: short product area name like "session replay", "feature flags", "insights", "billing", "data ingestion", "internal tooling"
- `summary`: one sentence a VP would understand, e.g., "Rebuilt the batch export pipeline to handle higher volume"

**How to batch:** Send 3–5 PRs per API call as a JSON array, ask for a JSON array back. Use a fast/cheap model (GPT-4o-mini or Claude Haiku) since this is classification, not reasoning.

**Output:** `data/enriched_prs.json` — same as raw_prs but with `type`, `impact_level`, `area`, `summary` added.

### 3b. Engineer-Level Narrative Summary

**What you send to the LLM per engineer:**
- List of their enriched PRs (just `type`, `area`, `impact_level`, `summary` fields)
- Their raw metrics (merged_prs count, reviews_given count, issues_closed count)

**What the LLM returns:**
- 3–5 bullet points describing what this engineer focused on, the balance of their work (feature vs infra vs bugfix), and any collaboration patterns.

**Output:** `data/engineer_summaries.json` — one record per engineer with their bullet summary.

---

## PHASE 4: Scoring (Minutes 60–70)

All scoring is deterministic Python math. The LLM labels feed into counts, but the formula is fixed and transparent.

### Step 1: Derive LLM-Informed Counts

From the enriched PRs, compute per engineer:
- `high_impact_feature_prs` — count of PRs where type=feature AND impact_level=high
- `medium_impact_feature_prs` — count where type=feature AND impact_level=medium
- `high_impact_bugfix_prs` — count where type=bugfix AND impact_level=high
- `areas_touched` — count of distinct `area` values across their PRs

### Step 2: Normalize

For each metric across all engineers: divide by the max value so everything is 0 to 1. Apply `log(1 + x)` before normalizing for `capped_lines_changed` (it's heavy-tailed).

### Step 3: Compute Three Component Scores

**Product Score (40% of overall)**
```
product = (
    0.35 × norm(high_impact_feature_prs × 3 + medium_impact_feature_prs × 2 + other_feature_prs × 1)
  + 0.30 × norm(bugfix_prs + high_impact_bugfix_prs × 2)
  + 0.20 × norm(log_capped_lines_changed)
  + 0.15 × norm(issues_closed)
)
```
*Where each number comes from: `high_impact_feature_prs` is from LLM classification of PRs in enriched_prs.json. `bugfix_prs` is from GitHub labels on raw PRs. `capped_lines_changed` is additions+deletions per PR capped at 1000, from GitHub API. `issues_closed` is from the closed issues API.*

**Ownership Score (25% of overall)**
```
ownership = (
    0.40 × norm(unique_dirs_touched)
  + 0.25 × norm(complex_prs)
  + 0.20 × norm(areas_touched)
  + 0.15 × norm(self_opened_issues_closed)
)
```
*Where each number comes from: `unique_dirs_touched` is from changed file paths on merged PRs from GitHub API. `complex_prs` is PRs with 10+ changed files and 3+ review comments, from GitHub API. `areas_touched` is distinct LLM-classified product areas. `self_opened_issues_closed` is from cross-referencing issue opener and closer in the issues API.*

**Collaboration Score (35% of overall)**
```
collaboration = (
    0.45 × norm(reviews_given)
  + 0.30 × norm(review_comments_written)
  + 0.25 × norm(distinct_people_reviewed)
)
```
*Where each number comes from: all three metrics come from the reviews API. `reviews_given` is count of reviews submitted on others' PRs. `review_comments_written` is count of substantive (>10 char) inline comments on others' PRs. `distinct_people_reviewed` is count of unique teammates whose code they reviewed.*

### Step 4: Overall Impact Score

```
impact_score = 0.40 × product + 0.25 × ownership + 0.35 × collaboration
```

### Step 5: Sanity Check

Sort by impact_score. Look at the top 5. Do they pass the smell test? If someone dominates purely from lines changed, raise the cap or lower that sub-weight. If a known bot slipped through, add it to the exclusion list.

**Output:** `data/engineer_scores.json` — one record per engineer with: `impact_score`, `product_score`, `ownership_score`, `collaboration_score`, all raw metrics, `dominant_area`, and a link to their summary.

---

## PHASE 5: Dashboard Build (Minutes 70–85)

Use Streamlit. Read from `engineer_scores.json` and `engineer_summaries.json`. Everything is pre-computed so the app loads instantly.

### Layout (one laptop screen):

**Header:** Title, date range, 2-line impact definition.

**Main area:** Bar chart of top 5 by impact score. Table with columns: Engineer, Impact Score, Product, Ownership, Collaboration, Merged PRs, Reviews Given, Issues Closed, Main Area.

**Scoring explanation panel:** The three dimensions, what feeds each one (with exact data sources named), the weights. Also a "What this doesn't capture" note (mentoring, on-call, architectural vision).

**Engineer detail (on click/select):** Component score breakdown with the actual counts behind each score. LLM narrative summary bullets. Top 3 PRs with title, LLM summary, type, impact level, and a clickable GitHub link.

**LLM transparency note:** "PR classifications were generated by an LLM using PR metadata (title, description, file paths). All scoring and ranking is deterministic."

---

## PHASE 6: Deploy and Submit (Minutes 85–95)

Push to GitHub. Deploy on Streamlit Community Cloud (share.streamlit.io — connect repo, point to app.py, deploys in ~2 min).

### Pre-submit checklist:
- Page loads in under 5 seconds
- Top 5 engineers visible without scrolling
- Every score has a visible explanation
- All GitHub links work
- Bots are excluded
- Date range is stated explicitly
- Scoring formula is visible, not hidden

---

## File Structure

```
project/
├── fetch_data.py            # GitHub API → raw JSON files
├── enrich.py                # LLM classification + engineer summaries
├── compute_scores.py        # Raw metrics → normalized scores
├── app.py                   # Streamlit dashboard
├── data/
│   ├── raw_prs.json
│   ├── raw_reviews.json
│   ├── raw_issues.json
│   ├── enriched_prs.json
│   ├── engineer_raw_metrics.json
│   ├── engineer_scores.json
│   └── engineer_summaries.json
└── requirements.txt
```

---

## Email to Kevin

**URL:** [your Streamlit app link]

**Description (~290 chars):**
"Impact = shipped product value + codebase ownership + team collaboration. Used 90 days of GitHub PR, review, and issue data from PostHog/posthog. LLM classifies PR type/area/user-impact; all scoring is deterministic with transparent weights. Dashboard shows top 5 with full metric traceability."

**Time:** [your timer reading]

**Attached:** coding agent session export
