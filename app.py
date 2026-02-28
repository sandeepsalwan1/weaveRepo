import json
import math
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from collections import Counter

st.set_page_config(page_title="PostHog Impact", layout="wide")
st.markdown("""<style>
section.main>div{padding-top:0}
.block-container{padding-top:5.5rem;max-width:1100px;margin:auto}
div[data-testid="stRadio"] label{font-size:1.05rem}
div[data-testid="stExpander"] details summary p{font-size:0.82rem;margin:0}
</style>""", unsafe_allow_html=True)

scores = json.load(open("data/engineer_scores.json"))
summaries = json.load(open("data/engineer_summaries.json"))
enriched_prs = json.load(open("data/enriched_prs.json"))
df = pd.DataFrame(scores)
top5 = df.head(5)

TYPE_COLORS = {"feature": "#1f77b4", "bugfix": "#d62728", "refactor": "#ff7f0e",
               "infra": "#9467bd", "docs": "#2ca02c", "chore": "#7f7f7f", "unknown": "#bcbd22"}
IMPACT_ORDER = {"high": 0, "medium": 1, "low": 2}
TYPE_MULT = {"feature": 1.0, "bugfix": 1.0, "refactor": 0.6,
             "infra": 0.4, "chore": 0.25, "docs": 0.25, "unknown": 0.3}
IMPACT_MULT = {"high": 3.0, "medium": 1.5, "low": 1.0}

# ── Ranked list | Profile ──

pick_col, profile_col = st.columns([0.20, 0.80], gap="medium")

with pick_col:
    st.markdown("**Top 5 Engineers** · PostHog · Past 90 days")
    options = [f"#{i+1}  {r['engineer']}" for i, r in top5.iterrows()]
    choice = st.radio("rank", options, label_visibility="collapsed")
    selected = top5.iloc[options.index(choice)]
    name = selected["engineer"]

with profile_col:
    sc, dc, mc = st.columns([0.18, 0.28, 0.54])

    with sc:
        st.markdown(
            f"<div style='text-align:center;padding-top:0.3rem'>"
            f"<div style='font-size:0.82rem;color:#888'>Impact Score</div>"
            f"<div style='font-size:2.8rem;font-weight:700;line-height:1'>{selected['impact_score']:.2f}</div>"
            f"<div style='font-size:0.9rem;color:#666;margin-top:4px'>{name}</div>"
            f"<div style='font-size:0.78rem;color:#aaa'>{selected['dominant_area']}</div>"
            f"</div>", unsafe_allow_html=True,
        )

    with dc:
        prod_c = selected["product_score"] * 0.50
        own_c = selected["ownership_score"] * 0.20
        coll_c = selected["collaboration_score"] * 0.30
        fig = go.Figure(go.Pie(
            labels=["Prod", "Own", "Collab"],
            values=[prod_c, own_c, coll_c],
            hole=0.55, marker_colors=["#3b82f6", "#f59e0b", "#10b981"],
            textinfo="label+percent", textfont_size=10,
            textposition="inside",
            hovertemplate="%{label}: %{value:.3f}<extra></extra>",
            sort=False,
        ))
        fig.update_layout(margin=dict(l=5, r=5, t=5, b=5), height=175, showlegend=False)
        st.plotly_chart(fig, use_container_width=True)

    with mc:
        e = selected
        wpi = e["weighted_pr_impact"]
        n_prs = int(e["merged_prs"])
        lines = int(e["capped_lines_changed"])
        iss = int(e["issues_closed"])
        bugs = int(e["bugfix_prs"])
        dirs = int(e["unique_dirs_touched"])
        cplx = int(e["complex_prs"])
        areas = int(e["areas_touched"])
        self_iss = int(e["self_opened_issues_closed"])
        revs = int(e["reviews_given"])
        rcmt = int(e["review_comments_written"])
        ppl = int(e["distinct_people_reviewed"])

        my_prs = [p for p in enriched_prs if p["author"] == name]
        breakdown = Counter()
        for p in my_prs:
            breakdown[(p.get("pr_type", "unknown"), p.get("impact_level", "medium"))] += 1
        wpi_parts = []
        for (t, imp), cnt in sorted(breakdown.items(), key=lambda x: -x[1]):
            sc_ = TYPE_MULT.get(t, 0.3) * IMPACT_MULT.get(imp, 1.0)
            wpi_parts.append(f"{cnt}x {t}/{imp} ({cnt} x {sc_:.1f} = {cnt*sc_:.1f})")
        wpi_detail = " + ".join(wpi_parts)

        prod_tip = (
            f"Product = 45% weighted PR score + 25% lines + 15% issues + 15% bugfixes\n\n"
            f"PR formula: every PR x (type {{feature/bugfix:1.0, refactor:0.6, infra:0.4, chore/docs:0.25}} "
            f"x impact {{high:3, med:1.5, low:1}})"
            f" Impact classified by LLM using PR title, description, and file paths.\n\n"
            f"{name} WEIGHTED PR SCORE = {wpi}"
            f"{wpi_detail}\n\n"
            f"LINES CHANGED = {lines} -> ln(1+{lines}) = {math.log(1+lines):.1f}, capped at 1000/PR\n\n"
            f"ISSUES CLOSED = {iss}\n\n"
            f"BUGFIXES = {bugs}, LLM-classified"
        )

        own_tip = (
            f"Ownership = 40% dirs + 25% complex PRs + 20% areas + 15% self-closed issues\n\n"
            f"{name} OWNERSHIP BREAKDOWN\n\n"
            f"DIRECTORIES = {dirs} -> ln(1+{dirs}) = {math.log(1+dirs):.1f}\n\n"
            f"COMPLEX PRS = {cplx} (PRs with 10+ changed files)\n\n"
            f"PRODUCT AREAS = {areas} (distinct LLM-assigned areas)\n\n"
            f"SELF-CLOSED ISSUES = {self_iss}"
        )

        coll_tip = (
            f"Collaboration = 45% reviews + 30% comments + 25% teammates\n\n"
            f"{name} COLLABORATION BREAKDOWN\n\n"
            f"REVIEWS GIVEN = {revs} (on others' PRs, self-reviews excluded)\n\n"
            f"REVIEW COMMENTS = {rcmt} (body > 10 chars)\n\n"
            f"TEAMMATES REVIEWED = {ppl} (distinct PR authors reviewed)"
        )

        c1, c2, c3 = st.columns(3)
        c1.metric("Product", f"{e['product_score']:.2f}", help=prod_tip)
        c2.metric("Ownership", f"{e['ownership_score']:.2f}", help=own_tip)
        c3.metric("Collaboration", f"{e['collaboration_score']:.2f}", help=coll_tip)

    # ── Summary ──
    summary = summaries.get(name, "")
    bullets = [b.strip() for b in summary.split("\n") if b.strip()]
    if bullets:
        st.markdown(bullets[0])
        if len(bullets) > 1:
            with st.expander("Expanded summary", expanded=False):
                st.markdown("\n\n".join(bullets[1:]))

    # ── Top PRs ──
    eng_prs = sorted(
        [p for p in enriched_prs if p["author"] == name],
        key=lambda p: (IMPACT_ORDER.get(p.get("impact_level", "low"), 2), -(p["additions"] + p["deletions"])),
    )

    def render_pr(pr):
        pt = pr.get("pr_type", "unknown")
        color = TYPE_COLORS.get(pt, "#999")
        link = f"https://github.com/PostHog/posthog/pull/{pr['number']}"
        badge = f'<span style="background:{color};color:#fff;padding:1px 6px;border-radius:3px;font-size:0.73em">{pt}</span>'
        ibadge = f'<span style="background:#444;color:#fff;padding:1px 6px;border-radius:3px;font-size:0.73em">{pr.get("impact_level","")}</span>'
        st.markdown(
            f'{badge} {ibadge} **[{pr["title"]}]({link})** · {pr.get("area","")}<br>'
            f'<span style="color:#888;font-size:0.83em">{pr.get("summary","")}</span>',
            unsafe_allow_html=True,
        )

    if eng_prs:
        render_pr(eng_prs[0])
        if len(eng_prs) > 1:
            with st.expander(f"{min(len(eng_prs)-1, 2)} more Impactful PRs"):
                for pr in eng_prs[1:3]:
                    render_pr(pr)

# ── Formula (always visible, at bottom) ──

st.markdown("---")
st.markdown("""<div style="font-size:0.82rem;color:#555;line-height:1.6">

**Impact Score** = 50% Product + 20% Ownership + 30% Collaboration

**Product** = weighted PR score + issues closed + lines changed + bugfix bonus<br>
**Ownership** = directories touched + complex PRs + product areas + self-closed issues<br>
**Collaboration** = reviews given + review comments + teammates reviewed

For detailed calculations per engineer click the ? over each of the 3 metrics. 

</div>""", unsafe_allow_html=True)

st.caption("GitHub GraphQL API · LLM enriched data  · Weave")
