"""Fetch raw data from GitHub GraphQL API for PostHog/posthog."""

import os
import json
import time
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

GRAPHQL_URL = "https://api.github.com/graphql"
BOT_LOGINS = {
    "dependabot", "github-actions", "posthog-bot", "codecov-commenter",
    "posthog-contributions-bot", "greptile-apps", "graphite-app",
    "copilot-pull-request-reviewer", "mendral-app", "chatgpt-codex-connector",
    "scheduled-actions-posthog", "posthog-js-upgrader",
}


def is_bot(login):
    if not login:
        return True
    if "[bot]" in login:
        return True
    return login.lower() in BOT_LOGINS


def gql(token, query, variables=None):
    """Execute a GraphQL request with rate-limit retry on 403."""
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"query": query, "variables": variables or {}}
    resp = requests.post(GRAPHQL_URL, json=payload, headers=headers)
    if resp.status_code == 403:
        print("  Rate limited — sleeping 60s...")
        time.sleep(60)
        resp = requests.post(GRAPHQL_URL, json=payload, headers=headers)
    resp.raise_for_status()
    body = resp.json()
    if "errors" in body:
        print("  GraphQL errors:", body["errors"][:2])
    if body.get("data") is None:
        raise RuntimeError(f"GraphQL returned no data: {body}")
    return body["data"]


# ── 1. Merged PRs ────────────────────────────────────────────────

MERGED_PRS_QUERY = """
query($cursor: String, $q: String!) {
  search(query: $q, type: ISSUE, first: 100, after: $cursor) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number title body
        author { login }
        createdAt mergedAt
        additions deletions changedFiles
        labels(first: 20) { nodes { name } }
        files(first: 100) { nodes { path } }
        closingIssuesReferences(first: 10) { nodes { number } }
      }
    }
  }
}
"""


def fetch_merged_prs(token, since_date):
    """Fetch all merged PRs since since_date, excluding bots."""
    q = f"repo:PostHog/posthog is:pr is:merged merged:>{since_date}"
    prs = []
    cursor = None
    page = 0

    while True:
        page += 1
        data = gql(token, MERGED_PRS_QUERY, {"q": q, "cursor": cursor})
        search = data["search"]

        if page == 1:
            print(f"  Total merged PRs matching query: {search['issueCount']}")

        for node in search["nodes"]:
            if not node or "number" not in node:
                continue
            author = (node.get("author") or {}).get("login")
            if is_bot(author):
                continue
            prs.append({
                "number": node["number"],
                "title": node["title"],
                "body": (node.get("body") or "")[:500],
                "author": author,
                "created_at": node["createdAt"],
                "merged_at": node["mergedAt"],
                "additions": node["additions"],
                "deletions": node["deletions"],
                "changed_files": node["changedFiles"],
                "labels": [l["name"] for l in (node.get("labels", {}).get("nodes") or [])],
                "file_paths": [f["path"] for f in (node.get("files", {}).get("nodes") or [])],
                "closing_issue_numbers": [
                    i["number"] for i in (node.get("closingIssuesReferences", {}).get("nodes") or [])
                ],
            })

        print(f"  Page {page}: {len(prs)} PRs collected")

        if not search["pageInfo"]["hasNextPage"]:
            break
        cursor = search["pageInfo"]["endCursor"]

    return prs


# ── 2. Reviews & Review Comments ─────────────────────────────────

def _build_review_query(batch):
    """Build a batched GraphQL query using aliases — one alias per PR."""
    fragments = []
    for num in batch:
        fragments.append(f"""
            pr_{num}: pullRequest(number: {num}) {{
                reviews(first: 100) {{
                    nodes {{ author {{ login }} state submittedAt body }}
                }}
                reviewThreads(first: 100) {{
                    nodes {{
                        comments(first: 50) {{
                            nodes {{ author {{ login }} createdAt body }}
                        }}
                    }}
                }}
            }}""")
    return '{ repository(owner: "PostHog", name: "posthog") {' + "".join(fragments) + " } }"


def fetch_reviews_for_prs(token, pr_numbers):
    """Fetch reviews and inline review comments, batching 5 PRs per request."""
    reviews, comments = [], []
    total = len(pr_numbers)

    for i in range(0, total, 5):
        batch = pr_numbers[i:i + 5]
        query = _build_review_query(batch)
        data = gql(token, query)
        repo = data["repository"]

        for num in batch:
            pr_data = repo.get(f"pr_{num}")
            if not pr_data:
                continue

            # Reviews (APPROVED, CHANGES_REQUESTED, etc.)
            for r in (pr_data.get("reviews", {}).get("nodes") or []):
                login = (r.get("author") or {}).get("login")
                if is_bot(login):
                    continue
                reviews.append({
                    "pr_number": num,
                    "reviewer": login,
                    "review_state": r["state"],
                    "submitted_at": r["submittedAt"],
                    "body": r.get("body") or "",
                })

            # Inline review comments from review threads
            for thread in (pr_data.get("reviewThreads", {}).get("nodes") or []):
                for c in (thread.get("comments", {}).get("nodes") or []):
                    login = (c.get("author") or {}).get("login")
                    if is_bot(login):
                        continue
                    comments.append({
                        "pr_number": num,
                        "commenter": login,
                        "created_at": c["createdAt"],
                        "body": c.get("body") or "",
                    })

        batch_num = i // 5 + 1
        total_batches = (total + 4) // 5
        print(f"  Batch {batch_num}/{total_batches}: {len(reviews)} reviews, {len(comments)} comments")

    return reviews, comments


# ── 3. Closed Issues ─────────────────────────────────────────────

CLOSED_ISSUES_QUERY = """
query($cursor: String) {
  repository(owner: "PostHog", name: "posthog") {
    issues(states: CLOSED, first: 100, after: $cursor,
           orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title
        labels(first: 20) { nodes { name } }
        author { login }
        closedAt
        stateReason
        timelineItems(itemTypes: [CLOSED_EVENT], first: 1) {
          nodes { ... on ClosedEvent { actor { login } } }
        }
      }
    }
  }
}
"""


def fetch_closed_issues(token, since_date):
    """Fetch issues closed as COMPLETED since since_date."""
    issues = []
    cursor = None
    page = 0
    since_dt = datetime.fromisoformat(since_date + "T00:00:00+00:00")
    empty_pages = 0

    while True:
        page += 1
        data = gql(token, CLOSED_ISSUES_QUERY, {"cursor": cursor})
        issue_data = data["repository"]["issues"]
        nodes = issue_data["nodes"]
        page_info = issue_data["pageInfo"]

        found = 0
        for node in nodes:
            closed_at = node.get("closedAt")
            if not closed_at:
                continue
            closed_dt = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
            if closed_dt < since_dt:
                continue
            if node.get("stateReason") != "COMPLETED":
                continue

            author = (node.get("author") or {}).get("login")
            timeline = node.get("timelineItems", {}).get("nodes") or []
            closed_by = (timeline[0].get("actor") or {}).get("login") if timeline else None
            if is_bot(closed_by):
                continue

            found += 1
            issues.append({
                "number": node["number"],
                "title": node["title"],
                "labels": [l["name"] for l in (node.get("labels", {}).get("nodes") or [])],
                "author": author,
                "closed_at": closed_at,
                "closed_by": closed_by,
            })

        print(f"  Page {page}: {len(issues)} issues collected ({found} new this page)")

        # Stop if two consecutive pages have zero matches (past our window)
        empty_pages = empty_pages + 1 if found == 0 else 0
        if empty_pages >= 2 or not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]

    return issues


# ── Main ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    load_dotenv()
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN not set in .env")

    since = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
    os.makedirs("data", exist_ok=True)
    print(f"Fetching PostHog/posthog data since {since}\n")

    # 1. Merged PRs
    print("1/3  Merged PRs...")
    prs = fetch_merged_prs(token, since)
    with open("data/raw_prs.json", "w") as f:
        json.dump(prs, f, indent=2)

    # 2. Reviews & comments
    pr_numbers = [pr["number"] for pr in prs]
    print(f"\n2/3  Reviews for {len(pr_numbers)} PRs...")
    reviews, review_comments = fetch_reviews_for_prs(token, pr_numbers)
    with open("data/raw_reviews.json", "w") as f:
        json.dump(reviews, f, indent=2)
    with open("data/raw_review_comments.json", "w") as f:
        json.dump(review_comments, f, indent=2)

    # 3. Closed issues
    print("\n3/3  Closed issues...")
    issues = fetch_closed_issues(token, since)
    with open("data/raw_issues.json", "w") as f:
        json.dump(issues, f, indent=2)

    print(f"\n{'='*40}")
    print(f"Merged PRs:       {len(prs)}")
    print(f"Reviews:          {len(reviews)}")
    print(f"Review comments:  {len(review_comments)}")
    print(f"Closed issues:    {len(issues)}")
    print("Saved to data/")
