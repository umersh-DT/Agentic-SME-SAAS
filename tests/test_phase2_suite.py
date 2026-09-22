import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import shutil
import unittest

from src.skills.memory_tree import MemoryNode, TenantMemoryTree
from src.core.context_extractor import ContextExtractor
from src.skills.calendar_sync import CalendarSyncSkill, BookingRequest
from src.skills.invoicing import InvoicingSkill, LineItem
from src.skills.research import DeepResearchSkill
from src.skills.seo_manager import SeoManagerSkill


class TestPhase2Integration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_dir = Path("/tmp/test_phase2_suite")
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)
        self.test_dir.mkdir(parents=True, exist_ok=True)

    async def asyncTearDown(self):
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir)

    async def test_multi_tenant_isolation_and_skill_flow(self):
        # 1. Initialize two isolated tenants
        tenant_a = "tenant_curtains_001"
        tree_a = TenantMemoryTree(tenant_id=tenant_a, base_data_dir=str(self.test_dir))
        await tree_a.initialize()
        extractor_a = ContextExtractor(memory_tree=tree_a)

        tenant_b = "tenant_photographer_001"
        tree_b = TenantMemoryTree(tenant_id=tenant_b, base_data_dir=str(self.test_dir))
        await tree_b.initialize()
        extractor_b = ContextExtractor(memory_tree=tree_b)

        # 2. Extract rules and store in memory
        node_a = await extractor_a.extract_and_store(
            "All custom motorized curtains require a mandatory 50% deposit before cutting fabric."
        )
        self.assertIsNotNone(node_a)

        node_b = await extractor_b.extract_and_store(
            "Studio portrait sessions require a 25% non-refundable booking deposit."
        )
        self.assertIsNotNone(node_b)

        # 3. Assert Database Isolation (FTS search across tenants)
        search_a = await tree_a.search_memory("portrait session")
        self.assertEqual(len(search_a), 0, "Tenant A database leaked Tenant B records")

        search_b = await tree_b.search_memory("portrait session")
        self.assertGreaterEqual(len(search_b), 1)
        self.assertIn("25%", search_b[0]["content"])

        # 4. Verify Invoicing integration with extracted 50% deposit rule
        invoicing_a = InvoicingSkill(tenant_id=tenant_a, default_currency="AED")
        invoice_a = invoicing_a.generate_invoice(
            client_name="Sarah Al-Nuaimi",
            client_contact="+971501112233",
            items=[
                LineItem(
                    description="Motorized Living Room Drapes",
                    quantity=Decimal("1.0"),
                    unit_price=Decimal("2000.00"),
                    tax_rate=Decimal("0.05"),
                )
            ],
            deposit_percentage=Decimal("50.0"),
        )
        self.assertEqual(invoice_a.grand_total, Decimal("2100.00"))
        self.assertEqual(invoice_a.required_deposit, Decimal("1050.00"))
        self.assertEqual(invoice_a.balance_due, Decimal("1050.00"))

        # 5. Verify Calendar booking and slot conflict detection
        cal_b = CalendarSyncSkill(tenant_id=tenant_b)
        now = datetime.utcnow()
        booking_start = datetime(now.year, now.month, now.day, 14, 0)

        booked = await cal_b.create_booking(
            BookingRequest(
                tenant_id=tenant_b,
                client_name="John Doe",
                client_contact="+971509998877",
                service_title="Commercial Headshot",
                start_time=booking_start,
                duration_minutes=60,
                buffer_minutes=15,
            )
        )
        self.assertEqual(booked.status, "confirmed")

        with self.assertRaises(ValueError) as ctx:
            await cal_b.create_booking(
                BookingRequest(
                    tenant_id=tenant_b,
                    client_name="Overlap Client",
                    client_contact="+971508887766",
                    service_title="Family Portrait",
                    start_time=booking_start + timedelta(minutes=30),
                    duration_minutes=45,
                )
            )
        self.assertIn("Slot conflict", str(ctx.exception))

        # 6. Verify SEO Plan and Competitor Intelligence generation
        seo = SeoManagerSkill(tenant_id=tenant_a, business_name="Royal Drapery")
        plan = await seo.generate_weekly_plan(industry="Motorized Curtains", primary_city="Dubai")
        self.assertEqual(len(plan.tracked_keywords), 3)
        self.assertIn("complimentary on-site measurement", plan.gbp_post_draft)

        research = DeepResearchSkill(tenant_id=tenant_a)
        report = await research.analyze_competitors(industry="Custom Curtains", location="Dubai Marina")
        self.assertGreaterEqual(len(report.competitors), 2)
        self.assertGreaterEqual(len(report.strategic_recommendations), 3)

        print("\n[SUCCESS] All Phase 2 end-to-end integration assertions passed successfully!")


if __name__ == "__main__":
    unittest.main()