"""Compute raw metrics and impact scores per engineer."""

import json
import math
from collections import Counter

# Same bot list as fetch_data.py — filters at compute time for already-fetched data
BOT_LOGINS = {
    "dependabot", "github-actions", "posthog-bot", "codecov-commenter",
    "posthog-contributions-bot", "greptile-apps", "graphite-app",
    "copilot-pull-request-reviewer", "mendral-app", "chatgpt-codex-connector",
    "scheduled-actions-posthog", "posthog-js-upgrader",
}


def _is_bot(login):
    if not login:
        return True
    if "[bot]" in login:
        return True
    return login.lower() in BOT_LOGINS


def compute_raw_metrics(prs, reviews, review_comments, issues):
    """Compute per-engineer metrics from the four raw data lists.

    Returns a dict keyed by engineer login with all metric fields.
    Engineers from ANY data source are included (reviewers with 0 PRs, etc.).
    """
    # PR number -> author lookup (needed to exclude self-reviews)
    pr_author = {pr["number"]: pr["author"] for pr in prs}

    # Collect every engineer seen in any data source, skipping bots
    engineers = set()
    for pr in prs:
        if not _is_bot(pr["author"]):
            engineers.add(pr["author"])
    for r in reviews:
        if not _is_bot(r["reviewer"]):
            engineers.add(r["reviewer"])
    for c in review_comments:
        if not _is_bot(c["commenter"]):
            engineers.add(c["commenter"])
    for issue in issues:
        if not _is_bot(issue.get("closed_by")):
            engineers.add(issue["closed_by"])
        if not _is_bot(issue.get("author")):
            engineers.add(issue["author"])

    # Initialize every engineer with zeroed metrics
    metrics = {}
    for eng in engineers:
        metrics[eng] = {
            "merged_prs": 0,
            "capped_lines_changed": 0,
            "bugfix_prs": 0,
            "issues_closed": 0,
            "unique_dirs_touched": set(),
            "complex_prs": 0,
            "self_opened_issues_closed": 0,
            "reviews_given": 0,
            "review_comments_written": 0,
            "_people_reviewed": set(),
        }

    # ── Product & Ownership metrics from PRs ──
    for pr in prs:
        author = pr["author"]
        if not author or author not in metrics:
            continue
        m = metrics[author]

        m["merged_prs"] += 1
        m["capped_lines_changed"] += min(pr["additions"] + pr["deletions"], 1000)

        # Bugfix: any label containing "bug"
        if any("bug" in label.lower() for label in pr.get("labels", [])):
            m["bugfix_prs"] += 1

        # Dirs touched: first path segment
        for path in pr.get("file_paths", []):
            first_segment = path.split("/")[0]
            if first_segment:
                m["unique_dirs_touched"].add(first_segment)

        # Complex PR: 10+ changed files
        if pr["changed_files"] >= 10:
            m["complex_prs"] += 1

    # ── Issues closed ──
    for issue in issues:
        closer = issue.get("closed_by")
        if not closer or closer not in metrics:
            continue
        metrics[closer]["issues_closed"] += 1
        if issue.get("author") == closer:
            metrics[closer]["self_opened_issues_closed"] += 1

    # ── Reviews given (exclude self-reviews) ──
    for r in reviews:
        reviewer = r["reviewer"]
        pr_auth = pr_author.get(r["pr_number"])
        if reviewer == pr_auth:
            continue
        if reviewer not in metrics:
            continue
        metrics[reviewer]["reviews_given"] += 1
        if pr_auth:
            metrics[reviewer]["_people_reviewed"].add(pr_auth)

    # ── Review comments written (exclude self, body > 10 chars) ──
    for c in review_comments:
        commenter = c["commenter"]
        pr_auth = pr_author.get(c["pr_number"])
        if commenter == pr_auth:
            continue
        if commenter not in metrics:
            continue
        if len(c.get("body", "")) > 10:
            metrics[commenter]["review_comments_written"] += 1
            if pr_auth:
                metrics[commenter]["_people_reviewed"].add(pr_auth)

    # ── Finalize: convert sets to counts ──
    for m in metrics.values():
        m["unique_dirs_touched"] = len(m["unique_dirs_touched"])
        m["distinct_people_reviewed"] = len(m.pop("_people_reviewed"))

    return metrics


# ── Temp main for sanity checking ──

if __name__ == "__main__":
    prs = json.load(open("data/raw_prs.json"))
    reviews = json.load(open("data/raw_reviews.json"))
    review_comments = json.load(open("data/raw_review_comments.json"))
    issues = json.load(open("data/raw_issues.json"))

    metrics = compute_raw_metrics(prs, reviews, review_comments, issues)

    print(f"Total engineers: {len(metrics)}\n")
    print("Top 10 by merged_prs:")
    print(f"{'Engineer':<30} {'PRs':>5} {'Lines':>7} {'Bugs':>5} {'Issues':>6} "
          f"{'Dirs':>5} {'Cplx':>5} {'Revs':>5} {'RvCmt':>6} {'Ppl':>4}")
    print("-" * 95)

    top = sorted(metrics.items(), key=lambda x: x[1]["merged_prs"], reverse=True)[:10]
    for eng, m in top:
        print(f"{eng:<30} {m['merged_prs']:>5} {m['capped_lines_changed']:>7} "
              f"{m['bugfix_prs']:>5} {m['issues_closed']:>6} {m['unique_dirs_touched']:>5} "
              f"{m['complex_prs']:>5} {m['reviews_given']:>5} "
              f"{m['review_comments_written']:>6} {m['distinct_people_reviewed']:>4}")
