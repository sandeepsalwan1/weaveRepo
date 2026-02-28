"""Enrich PRs with LLM classification and generate engineer summaries."""

import os
import json
from openai import OpenAI
from dotenv import load_dotenv

# Shared bot list — used to skip bots that slipped into raw data
BOT_LOGINS = {
    "dependabot", "github-actions", "posthog-bot", "codecov-commenter",
    "posthog-contributions-bot", "greptile-apps", "graphite-app",
    "copilot-pull-request-reviewer", "mendral-app", "chatgpt-codex-connector",
    "scheduled-actions-posthog", "posthog-js-upgrader",
}

CLASSIFY_SYSTEM = """You are a senior product engineer at PostHog classifying pull requests for impact analysis.

For each PR, return a JSON object with:
- type: one of feature, bugfix, refactor, infra, docs, chore
- impact_level: high, medium, or low (how much this affects PostHog end users)
- area: short product area name (e.g. "session replay", "feature flags", "insights", "billing", "data pipeline", "web analytics", "internal tooling", "experiments", "surveys", "toolbar", "batch exports", "CDP", "infrastructure", "testing")
- summary: one sentence a VP would understand

When classifying type, look at the actual changes — many bugfixes lack a "bug" label.
A PR that fixes broken behavior, patches errors, or resolves regressions is a bugfix.

Return ONLY valid JSON. No backticks, no markdown, no explanation.
When given an array of PRs, return a JSON array of results in the same order."""

SUMMARY_SYSTEM = """You are a senior engineering manager writing brief impact summaries.
For the given engineer, write 3 to 5 bullet points covering:
- What areas and themes they focused on
- Balance of feature work vs infrastructure vs bugfixes
- Collaboration and review patterns if notable
- Any standout contributions

Keep it non-technical, readable in 15 seconds. No fluff.
Return ONLY the bullet points as plain text, one per line starting with "•"."""

DEFAULT_CLASSIFICATION = {
    "type": "unknown",
    "impact_level": "medium",
    "area": "unknown",
    "summary": "",
}


def _classify_batch(client, prs_batch):
    """Send a batch of PRs to GPT-4o-mini for classification."""
    items = []
    for pr in prs_batch:
        items.append({
            "title": pr["title"],
            "body": pr.get("body", "")[:500],
            "labels": pr.get("labels", []),
            "file_paths": pr.get("file_paths", [])[:30],
        })

    prompt = json.dumps(items) if len(items) > 1 else json.dumps(items[0])

    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.2,
        messages=[
            {"role": "system", "content": CLASSIFY_SYSTEM},
            {"role": "user", "content": prompt},
        ],
    )
    raw = resp.choices[0].message.content.strip()
    result = json.loads(raw)

    # Normalize: single PR returns a dict, batch returns a list
    if isinstance(result, dict):
        return [result]
    return result


def enrich_prs(prs, api_key):
    """Classify each PR using GPT-4o-mini. Batches 3 at a time."""
    client = OpenAI(api_key=api_key)
    enriched = []
    total = len(prs)

    for i in range(0, total, 3):
        batch = prs[i:i + 3]

        try:
            classifications = _classify_batch(client, batch)
        except Exception:
            # Batch failed — fall back to individual calls
            classifications = []
            for pr in batch:
                try:
                    result = _classify_batch(client, [pr])
                    classifications.append(result[0])
                except Exception:
                    classifications.append(dict(DEFAULT_CLASSIFICATION))

        # Pair classifications with PRs
        for j, pr in enumerate(batch):
            cls = classifications[j] if j < len(classifications) else dict(DEFAULT_CLASSIFICATION)
            pr["pr_type"] = cls.get("type", "unknown")
            pr["impact_level"] = cls.get("impact_level", "medium")
            pr["area"] = cls.get("area", "unknown")
            pr["summary"] = cls.get("summary", "")
            enriched.append(pr)

        done = min(i + 3, total)
        if done % 21 < 3 or done == total:
            print(f"  Enriched {done}/{total} PRs")

    return enriched


def generate_engineer_summaries(enriched_prs, raw_metrics, api_key):
    """Generate 3-5 bullet point summaries for engineers with 3+ merged PRs."""
    client = OpenAI(api_key=api_key)

    # Group enriched PRs by author
    prs_by_author = {}
    for pr in enriched_prs:
        author = pr["author"]
        if author not in prs_by_author:
            prs_by_author[author] = []
        prs_by_author[author].append(pr)

    # Only summarize engineers with 3+ merged PRs
    eligible = [eng for eng, m in raw_metrics.items() if m["merged_prs"] >= 3]
    summaries = {}
    total = len(eligible)
    print(f"  Generating summaries for {total} engineers...")

    for idx, eng in enumerate(eligible):
        pr_list = prs_by_author.get(eng, [])
        m = raw_metrics[eng]

        # Build concise PR list for the prompt
        pr_items = [
            {
                "title": p["title"],
                "type": p.get("pr_type", "unknown"),
                "impact": p.get("impact_level", "medium"),
                "area": p.get("area", "unknown"),
                "summary": p.get("summary", ""),
            }
            for p in pr_list[:40]
        ]

        prompt = json.dumps({
            "engineer": eng,
            "merged_prs": m["merged_prs"],
            "reviews_given": m["reviews_given"],
            "issues_closed": m["issues_closed"],
            "bugfix_prs": m["bugfix_prs"],
            "unique_dirs_touched": m["unique_dirs_touched"],
            "distinct_people_reviewed": m["distinct_people_reviewed"],
            "prs": pr_items,
        })

        try:
            resp = client.chat.completions.create(
                model="gpt-5.2",
                temperature=0.3,
                messages=[
                    {"role": "system", "content": SUMMARY_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
            )
            summaries[eng] = resp.choices[0].message.content.strip()
        except Exception:
            summaries[eng] = "Summary unavailable."

        if (idx + 1) % 10 == 0 or idx + 1 == total:
            print(f"  Summarized {idx + 1}/{total}")

    return summaries


# ── Main ──

if __name__ == "__main__":
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY not set in .env")

    # 1. Enrich PRs with LLM classification
    print("Loading raw PRs...")
    prs = json.load(open("data/raw_prs.json"))
    print(f"Enriching {len(prs)} PRs...\n")
    enriched = enrich_prs(prs, api_key)
    with open("data/enriched_prs.json", "w") as f:
        json.dump(enriched, f, indent=2)
    print(f"\nSaved data/enriched_prs.json")

    # 2. Compute raw metrics (needed for summaries)
    from compute_scores import compute_raw_metrics

    reviews = json.load(open("data/raw_reviews.json"))
    review_comments = json.load(open("data/raw_review_comments.json"))
    issues = json.load(open("data/raw_issues.json"))
    raw_metrics = compute_raw_metrics(enriched, reviews, review_comments, issues)

    # 3. Generate engineer summaries
    print(f"\nGenerating engineer summaries...")
    summaries = generate_engineer_summaries(enriched, raw_metrics, api_key)
    with open("data/engineer_summaries.json", "w") as f:
        json.dump(summaries, f, indent=2)
    print(f"Saved data/engineer_summaries.json ({len(summaries)} engineers)")
