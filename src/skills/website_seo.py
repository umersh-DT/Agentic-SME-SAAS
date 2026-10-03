from datetime import date, timedelta
import logging
import os
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse

import httpx

from src.skills.google_service_account import GoogleNotConfigured, google_auth_headers, service_account_email

logger = logging.getLogger("website_seo")

PAGESPEED_API = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
SEARCH_CONSOLE_API = "https://www.googleapis.com/webmasters/v3/sites"
SEARCH_CONSOLE_SCOPES = ("https://www.googleapis.com/auth/webmasters.readonly",)
CATEGORIES = [("performance", "Speed"), ("seo", "SEO basics"), ("accessibility", "Accessibility"),
              ("best-practices", "Best practices")]


def _rating(score: int) -> str:
    return "good" if score >= 90 else "needs work" if score >= 50 else "poor"


async def pagespeed_check(url: str) -> Dict[str, Any]:
    """Runs Google's free PageSpeed Insights (Lighthouse) check on the mobile version of a site."""
    params: List[Any] = [("url", url), ("strategy", "mobile")]
    params += [("category", key.upper().replace("-", "_")) for key, _ in CATEGORIES]
    api_key = os.getenv("PAGESPEED_API_KEY", "").strip()
    if api_key:
        params.append(("key", api_key))
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.get(PAGESPEED_API, params=params)
    if resp.status_code != 200:
        raise RuntimeError(f"PageSpeed check failed with HTTP {resp.status_code}: {resp.text[:200]}")

    lighthouse = resp.json().get("lighthouseResult") or {}
    categories = lighthouse.get("categories") or {}
    audits = lighthouse.get("audits") or {}
    scores = {
        key: round(float(categories[key]["score"]) * 100)
        for key, _ in CATEGORIES
        if (categories.get(key) or {}).get("score") is not None
    }
    # Failing checks from the SEO and speed categories, worst first
    to_fix = []
    for key in ("seo", "performance"):
        for ref in (categories.get(key) or {}).get("auditRefs") or []:
            audit = audits.get(ref.get("id")) or {}
            score = audit.get("score")
            if score is not None and audit.get("scoreDisplayMode") in ("binary", "numeric", "metricSavings") \
                    and score < 0.9 and ref.get("weight", 0) > 0:
                to_fix.append((score, audit.get("title", ref.get("id"))))
    to_fix.sort(key=lambda item: item[0])
    return {
        "scores": scores,
        "largest_content_paint": (audits.get("largest-contentful-paint") or {}).get("displayValue"),
        "to_fix": [title for _, title in to_fix[:5]],
    }


async def search_console_summary(site_property: str, days: int = 28, today: Optional[date] = None) -> Dict[str, Any]:
    """Real Google Search numbers for the property (clicks, impressions, position, top searches)."""
    end = (today or date.today()) - timedelta(days=2)  # Search Console data lags ~2 days
    start = end - timedelta(days=days - 1)
    headers = await google_auth_headers(SEARCH_CONSOLE_SCOPES)
    url = f"{SEARCH_CONSOLE_API}/{quote(site_property, safe='')}/searchAnalytics/query"
    base = {"startDate": start.isoformat(), "endDate": end.isoformat()}
    async with httpx.AsyncClient(timeout=30.0) as client:
        totals_resp = await client.post(url, headers=headers, json=base)
        queries_resp = await client.post(url, headers=headers, json=dict(base, dimensions=["query"], rowLimit=5))
    for resp in (totals_resp, queries_resp):
        if resp.status_code in (403, 404):
            raise PermissionError("Search Console property not shared with the assistant.")
        if resp.status_code != 200:
            raise RuntimeError(f"Search Console error {resp.status_code}: {resp.text[:200]}")
    totals = (totals_resp.json().get("rows") or [{}])[0]
    return {
        "days": days,
        "clicks": int(totals.get("clicks", 0)),
        "impressions": int(totals.get("impressions", 0)),
        "position": float(totals.get("position", 0) or 0),
        "top_queries": [
            {"query": row["keys"][0], "clicks": int(row.get("clicks", 0)), "impressions": int(row.get("impressions", 0))}
            for row in queries_resp.json().get("rows") or []
        ],
    }


async def build_seo_report(website_url: Optional[str], search_console_property: Optional[str]) -> str:
    """WhatsApp-ready website report from real Google data. Never invents numbers."""
    if not website_url:
        return ("No website is set for this business yet. Ask the admin to add website_url to the business list, "
                "then send \"seo report\" again.")

    host = urlparse(website_url).netloc or website_url
    lines = [f"Website report for {host} (mobile)"]
    try:
        check = await pagespeed_check(website_url)
        for key, label in CATEGORIES:
            if key in check["scores"]:
                score = check["scores"][key]
                lines.append(f"• {label}: {score}/100 ({_rating(score)})")
        if check["largest_content_paint"]:
            lines.append(f"• Main content appears after {check['largest_content_paint']} (aim for under 2.5 s)")
        if check["to_fix"]:
            lines.append("\nFix first:")
            lines += [f"- {title}" for title in check["to_fix"]]
    except Exception as e:
        logger.error(f"[SEO] PageSpeed check failed: {e}")
        lines.append("• I couldn't run Google's website check right now. Please try again later.")

    if search_console_property:
        try:
            sc = await search_console_summary(search_console_property)
            lines.append(
                f"\nGoogle Search, last {sc['days']} days: {sc['clicks']:,} clicks, "
                f"{sc['impressions']:,} times shown, average position {sc['position']:.1f}"
            )
            if sc["top_queries"]:
                lines.append("Top searches:")
                lines += [f"{i}. {q['query']} — {q['clicks']} clicks, shown {q['impressions']:,} times"
                          for i, q in enumerate(sc["top_queries"], start=1)]
        except (PermissionError, GoogleNotConfigured):
            email = service_account_email() or "the assistant's Google service account"
            lines.append(f"\nGoogle Search numbers unavailable: add {email} as a user in Google Search Console.")
        except Exception as e:
            logger.error(f"[SEO] Search Console failed: {e}")
            lines.append("\nI couldn't load Google Search numbers right now.")
    else:
        lines.append("\nFor real Google Search numbers (clicks, top searches), connect Google Search Console.")
    return "\n".join(lines)
