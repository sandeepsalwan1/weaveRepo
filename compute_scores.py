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

        # Bugfix: use LLM pr_type if available, fall back to GitHub labels
        is_bugfix = pr.get("pr_type") == "bugfix" if "pr_type" in pr else \
            any("bug" in label.lower() for label in pr.get("labels", []))
        if is_bugfix:
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


# ── Impact Scores ─────────────────────────────────────────────────

TYPE_MULT = {"feature": 1.0, "bugfix": 1.0, "refactor": 0.6, "infra": 0.4,
             "chore": 0.25, "docs": 0.25, "unknown": 0.3}
IMPACT_MULT = {"high": 3.0, "medium": 1.5, "low": 1.0}


def _norm(values):
    """Normalize a list of numbers to 0-1 by dividing by max."""
    mx = max(values) if values else 0
    return [v / mx for v in values] if mx else [0.0] * len(values)


def compute_impact_scores(raw_metrics, enriched_prs):
    """Per-PR contribution scoring -> product/ownership/collaboration/overall.

    Returns sorted list of engineer score dicts.
    """
    # ── Step 1: Per-PR contribution scores summed per engineer ──
    pr_stats = {}
    for pr in enriched_prs:
        author = pr["author"]
        if author not in pr_stats:
            pr_stats[author] = {
                "weighted_pr_impact": 0.0,
                "high_impact_prs": 0,
                "areas": set(),
                "area_freq": Counter(),
            }
        s = pr_stats[author]
        pr_type = pr.get("pr_type", "unknown")
        impact = pr.get("impact_level", "medium")
        area = pr.get("area", "unknown")

        s["weighted_pr_impact"] += TYPE_MULT.get(pr_type, 0.3) * IMPACT_MULT.get(impact, 1.0)
        if impact == "high":
            s["high_impact_prs"] += 1
        s["areas"].add(area)
        s["area_freq"][area] += 1

    # ── Filter: >= 1 merged PR or >= 5 reviews given ──
    eligible = {
        eng: m for eng, m in raw_metrics.items()
        if m["merged_prs"] >= 1 or m["reviews_given"] >= 5
    }
    engineers = list(eligible.keys())

    def ps(eng, field):
        return pr_stats.get(eng, {}).get(field, 0)

    def ps_set_len(eng, field):
        v = pr_stats.get(eng, {}).get(field, set())
        return len(v) if isinstance(v, set) else 0

    # ── Step 2-4: Gather sub-metric arrays and normalize ──
    # Product
    weighted_impact = [ps(e, "weighted_pr_impact") for e in engineers]
    log_lines = [math.log(1 + eligible[e]["capped_lines_changed"]) for e in engineers]
    issues_closed = [eligible[e]["issues_closed"] for e in engineers]
    bugfix_counts = [eligible[e]["bugfix_prs"] for e in engineers]

    n_wpi = _norm(weighted_impact)
    n_lines = _norm(log_lines)
    n_issues = _norm(issues_closed)
    n_bugs = _norm(bugfix_counts)

    # Ownership (log dirs to compress 29 vs 10 gap)
    log_dirs = [math.log(1 + eligible[e]["unique_dirs_touched"]) for e in engineers]
    complex_prs = [eligible[e]["complex_prs"] for e in engineers]
    areas_touched = [ps_set_len(e, "areas") for e in engineers]
    self_issues = [eligible[e]["self_opened_issues_closed"] for e in engineers]

    n_dirs = _norm(log_dirs)
    n_complex = _norm(complex_prs)
    n_areas = _norm(areas_touched)
    n_self = _norm(self_issues)

    # Collaboration
    reviews_given = [eligible[e]["reviews_given"] for e in engineers]
    review_comments = [eligible[e]["review_comments_written"] for e in engineers]
    people_reviewed = [eligible[e]["distinct_people_reviewed"] for e in engineers]

    n_revs = _norm(reviews_given)
    n_rcmt = _norm(review_comments)
    n_ppl = _norm(people_reviewed)

    # ── Step 5: Compute scores ──
    results = []
    for i, eng in enumerate(engineers):
        product = 0.45 * n_wpi[i] + 0.25 * n_lines[i] + 0.15 * n_issues[i] + 0.15 * n_bugs[i]
        ownership = 0.40 * n_dirs[i] + 0.25 * n_complex[i] + 0.20 * n_areas[i] + 0.15 * n_self[i]
        collab = 0.45 * n_revs[i] + 0.30 * n_rcmt[i] + 0.25 * n_ppl[i]
        overall = 0.50 * product + 0.20 * ownership + 0.30 * collab

        pst = pr_stats.get(eng, {})
        dominant_area = pst.get("area_freq", Counter()).most_common(1)
        dominant_area = dominant_area[0][0] if dominant_area else "unknown"

        m = eligible[eng]
        results.append({
            "engineer": eng,
            "impact_score": round(overall, 3),
            "product_score": round(product, 3),
            "ownership_score": round(ownership, 3),
            "collaboration_score": round(collab, 3),
            "merged_prs": m["merged_prs"],
            "capped_lines_changed": m["capped_lines_changed"],
            "bugfix_prs": m["bugfix_prs"],
            "issues_closed": m["issues_closed"],
            "unique_dirs_touched": m["unique_dirs_touched"],
            "complex_prs": m["complex_prs"],
            "self_opened_issues_closed": m["self_opened_issues_closed"],
            "reviews_given": m["reviews_given"],
            "review_comments_written": m["review_comments_written"],
            "distinct_people_reviewed": m["distinct_people_reviewed"],
            "dominant_area": dominant_area,
            "weighted_pr_impact": round(ps(eng, "weighted_pr_impact"), 1),
            "high_impact_prs": ps(eng, "high_impact_prs"),
            "areas_touched": ps_set_len(eng, "areas"),
        })

    results.sort(key=lambda x: x["impact_score"], reverse=True)
    return results


# ── Main ──

if __name__ == "__main__":
    enriched = json.load(open("data/enriched_prs.json"))
    reviews = json.load(open("data/raw_reviews.json"))
    review_comments = json.load(open("data/raw_review_comments.json"))
    issues = json.load(open("data/raw_issues.json"))

    raw_metrics = compute_raw_metrics(enriched, reviews, review_comments, issues)
    scores = compute_impact_scores(raw_metrics, enriched)

    with open("data/engineer_scores.json", "w") as f:
        json.dump(scores, f, indent=2)

    print(f"Scored {len(scores)} engineers\n")

    header = (
        f"{'#':<3} {'Engineer':<22} {'Impact':>6} {'Prod':>5} {'Own':>5} {'Coll':>5} "
        f"{'WPI':>6} {'PRs':>4} {'Bug':>4} {'Hi':>3} "
        f"{'Lines':>6} {'Iss':>4} {'Dirs':>4} {'Cplx':>4} {'Areas':>5} {'Self':>4} "
        f"{'Revs':>5} {'RvCm':>5} {'Ppl':>4} {'Area'}"
    )
    print(header)
    print("-" * len(header))
    for i, s in enumerate(scores[:15], 1):
        print(
            f"{i:<3} {s['engineer']:<22} "
            f"{s['impact_score']:>6.3f} {s['product_score']:>5.3f} "
            f"{s['ownership_score']:>5.3f} {s['collaboration_score']:>5.3f} "
            f"{s['weighted_pr_impact']:>6.1f} {s['merged_prs']:>4} {s['bugfix_prs']:>4} "
            f"{s['high_impact_prs']:>3} "
            f"{s['capped_lines_changed']:>6} {s['issues_closed']:>4} "
            f"{s['unique_dirs_touched']:>4} {s['complex_prs']:>4} "
            f"{s['areas_touched']:>5} {s['self_opened_issues_closed']:>4} "
            f"{s['reviews_given']:>5} {s['review_comments_written']:>5} "
            f"{s['distinct_people_reviewed']:>4} {s['dominant_area']}"
        )

    print(f"\n{'='*60}")
    print("SANITY CHECKS")
    print(f"{'='*60}")
    top5 = scores[:5]
    gap = top5[0]["impact_score"] - top5[4]["impact_score"]
    print(f"\nScore range (top 5): {top5[0]['impact_score']:.3f} — {top5[4]['impact_score']:.3f}  (gap: {gap:.3f})")
    print(f"Score range (all):   {scores[0]['impact_score']:.3f} — {scores[-1]['impact_score']:.3f}")

    print("\nBalance check — top 5:")
    for i, s in enumerate(top5, 1):
        flags = []
        if s["merged_prs"] < 5: flags.append(f"LOW PRs ({s['merged_prs']})")
        if s["reviews_given"] < 5: flags.append(f"LOW reviews ({s['reviews_given']})")
        if s["collaboration_score"] < 0.05 or s["product_score"] < 0.05: flags.append("LOPSIDED")
        status = ", ".join(flags) if flags else "OK"
        print(f"  #{i} {s['engineer']:<22} PRs={s['merged_prs']:<4} Revs={s['reviews_given']:<4} "
              f"Bugs={s['bugfix_prs']:<4} WPI={s['weighted_pr_impact']:<6} -> {status}")

    print("\nOutlier check — top 10 with < 3 PRs:")
    found = [s for s in scores[:10] if s["merged_prs"] < 3]
    if not found:
        print("  None — all top 10 have 3+ PRs")
    for s in found:
        print(f"  {s['engineer']} has {s['merged_prs']} PRs (score={s['impact_score']:.3f})")
