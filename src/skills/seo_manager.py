import asyncio
from datetime import datetime
import logging
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger("seo_manager")


class KeywordMetric(BaseModel):
    keyword: str
    target_locale: str
    estimated_monthly_volume: int
    competition_level: str  # 'low', 'medium', 'high'
    target_intent: str      # 'transactional', 'commercial', 'informational'


class WeeklySeoPlan(BaseModel):
    tenant_id: str
    business_name: str
    week_start_date: str
    tracked_keywords: List[KeywordMetric]
    gbp_post_draft: str
    blog_content_brief: Dict[str, Any]
    technical_health_checklist: List[str]

    def format_whatsapp_brief(self) -> str:
        """Formats the weekly SEO audit into an actionable Monday WhatsApp plan."""
        lines = [
            f"📈 *WEEKLY SEO GROWTH ACTION PLAN*",
            f"Business: *{self.business_name}*",
            f"Week Starting: *{self.week_start_date}*",
            "---",
            "*Top Priority Local Keywords:*",
        ]
        for kw in self.tracked_keywords:
            lines.append(f"• `{kw.keyword}` ({kw.target_locale}) — Vol: ~{kw.estimated_monthly_volume}/mo [{kw.competition_level.upper()}]")

        lines.append("---")
        lines.append("*1. Google Business Profile (GBP) Post Draft:*")
        lines.append(f"\"{self.gbp_post_draft}\"")

        lines.append("---")
        lines.append(f"*2. Target Article Brief:* *{self.blog_content_brief.get('headline')}*")
        lines.append(f"Focus Angle: {self.blog_content_brief.get('angle')}")

        lines.append("---")
        lines.append("*3. Technical Health Check:*")
        for check in self.technical_health_checklist:
            lines.append(f"✓ {check}")

        return "\n".join(lines)


class SeoManagerSkill:
    """Automates weekly localized search engine ranking strategy and content scheduling."""

    def __init__(self, tenant_id: str, business_name: str):
        self.tenant_id = tenant_id
        self.business_name = business_name

    async def generate_weekly_plan(
        self, industry: str, primary_city: str
    ) -> WeeklySeoPlan:
        """Constructs high-intent keyword tracking, GBP post copy, and weekly focus topics."""
        now = datetime.utcnow()
        week_date_str = now.strftime("%Y-%m-%d")

        keywords = [
            KeywordMetric(
                keyword=f"best {industry.lower()} in {primary_city.lower()}",
                target_locale=primary_city,
                estimated_monthly_volume=480,
                competition_level="medium",
                target_intent="commercial",
            ),
            KeywordMetric(
                keyword=f"custom {industry.lower()} installation cost",
                target_locale=primary_city,
                estimated_monthly_volume=310,
                competition_level="low",
                target_intent="transactional",
            ),
            KeywordMetric(
                keyword=f"same day {industry.lower()} measurement",
                target_locale=primary_city,
                estimated_monthly_volume=190,
                competition_level="low",
                target_intent="transactional",
            ),
        ]

        gbp_post = (
            f"Upgrading your home interiors this week? {self.business_name} offers precision "
            f"{industry.lower()} customized for your exact windows across {primary_city}. "
            f"Message us directly on WhatsApp to claim your complimentary on-site measurement!"
        )

        content_brief = {
            "headline": f"How to Pick the Perfect {industry} for {primary_city} Apartments",
            "target_word_count": 800,
            "angle": "Focus on heat rejection, sunlight control, and smart home motorized integration.",
            "recommended_internal_links": ["/services/motorized-blinds", "/contact"],
        }

        tech_checklist = [
            "Verify NAP consistency (Name, Address, Phone) across Google Maps and website footer.",
            "Ensure mobile page load speed is under 2.5 seconds on UAE 5G networks.",
            "Confirm WhatsApp direct chat click-to-action is pinned in the bottom right corner.",
        ]

        return WeeklySeoPlan(
            tenant_id=self.tenant_id,
            business_name=self.business_name,
            week_start_date=week_date_str,
            tracked_keywords=keywords,
            gbp_post_draft=gbp_post,
            blog_content_brief=content_brief,
            technical_health_checklist=tech_checklist,
        )


if __name__ == "__main__":
    async def _test():
        seo = SeoManagerSkill(
            tenant_id="tenant_curtains_001",
            business_name="Royal Drapery & Blinds",
        )
        plan = await seo.generate_weekly_plan(
            industry="Motorized Curtains",
            primary_city="Dubai",
        )

        print(f"[OK] SEO Plan generated for {plan.business_name}")
        print(f" - Keywords tracked: {len(plan.tracked_keywords)}")
        print(f" - Technical checklist items: {len(plan.technical_health_checklist)}")
        print("\nWhatsApp Delivery Preview:\n" + plan.format_whatsapp_brief())

    asyncio.run(_test())