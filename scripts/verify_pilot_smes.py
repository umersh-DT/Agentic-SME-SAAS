import asyncio
import logging
from pathlib import Path
import yaml

from src.skills.memory_tree import MemoryNode, TenantMemoryTree

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("verify_pilot_smes")

PILOT_POLICIES = {
    "tenant_curtains_001": [
        {
            "id": "curtains_deposit_rule",
            "title": "Fabric Cutting Deposit Requirement",
            "content": "All customized curtains require a non-refundable 50% deposit upfront before fabrics are cut.",
            "category": "pricing",
            "concepts": ["deposit", "pricing", "custom curtains", "fabric", "payment"],
        },
        {
            "id": "curtains_measurement_fee",
            "title": "On-Site Consultation & Measurement Fee",
            "content": "On-site window measurement is priced at 150 AED, fully credited against final purchase invoice if contract is signed.",
            "category": "pricing",
            "concepts": ["measurement", "site visit", "consultation", "credit", "quote"],
        },
    ],
    "tenant_photographer_001": [
        {
            "id": "photo_booking_deposit",
            "title": "Weekend Booking Retainer",
            "content": "Weekend portrait and event sessions require a 25% non-refundable booking retainer to reserve the studio date.",
            "category": "pricing",
            "concepts": ["retainer", "deposit", "booking", "weekend", "session"],
        },
        {
            "id": "photo_turnaround_time",
            "title": "Digital Gallery Delivery Timeline",
            "content": "Edited digital portrait galleries are delivered via private cloud download within 5 business days post-shoot.",
            "category": "operations",
            "concepts": ["delivery", "turnaround", "editing", "gallery", "download"],
        },
    ],
    "tenant_cleaning_001": [
        {
            "id": "cleaning_cancellation_window",
            "title": "Cancellation Notice Window",
            "content": "Appointments cancelled less than 24 hours prior to service incur a 100 AED late rescheduling fee.",
            "category": "operations",
            "concepts": ["cancellation", "reschedule", "fee", "notice", "policy"],
        },
        {
            "id": "cleaning_rates_standard",
            "title": "Standard Deep Cleaning Hourly Rate",
            "content": "Standard residential deep cleaning is billed at 45 AED per hour with a mandatory 3-hour minimum per visit.",
            "category": "pricing",
            "concepts": ["hourly rate", "pricing", "deep cleaning", "minimum", "cost"],
        },
    ],
}


async def run_pilot_verification(config_path: str = "config/tenants_pilot.yaml", base_data_dir: str = "data/pilot_tenants"):
    config_file = Path(config_path)
    if not config_file.exists():
        raise FileNotFoundError(f"Configuration file not found at: {config_path}")

    with open(config_file, "r", encoding="utf-8") as f:
        pilot_data = yaml.safe_load(f)

    tenants = pilot_data.get("tenants", {})
    logger.info(f"Loaded {len(tenants)} pilot tenants from {config_path}")

    for tenant_id, tenant_meta in tenants.items():
        logger.info(f"--- Provisioning & Verifying: {tenant_meta['business_name']} ({tenant_id}) ---")

        # 1. Initialize isolated memory tree
        tree = TenantMemoryTree(tenant_id=tenant_id, base_data_dir=base_data_dir)
        await tree.initialize()

        # 2. Seed Root Category Node
        root_node = MemoryNode(
            id=f"root_{tenant_id}",
            parent_id=None,
            node_type="category",
            title=f"{tenant_meta['business_name']} Master Policy",
            content=f"Root operating policies and guidelines for {tenant_meta['industry']}.",
            category="operations",
        )
        await tree.add_node(root_node)

        # 3. Seed Child Rules
        policies = PILOT_POLICIES.get(tenant_id, [])
        for policy in policies:
            node = MemoryNode(
                id=policy["id"],
                parent_id=root_node.id,
                node_type="pricing" if policy["category"] == "pricing" else "policy",
                title=policy["title"],
                content=policy["content"],
                category=policy["category"],
            )
            await tree.add_node(node, concepts=policy.get("concepts", []))

        # 4. Verify FTS5 query resolution
        test_concept = policies[0]["concepts"][0]
        results = await tree.search_memory(test_concept, limit=2)
        assert len(results) > 0, f"FTS search failed for query: '{test_concept}'"
        logger.info(f"[VERIFIED] Tenant {tenant_id} FTS query '{test_concept}' returned: '{results[0]['title']}'")

    logger.info("All 3 pilot tenants successfully verified without modifying config/tenants.yaml!")


if __name__ == "__main__":
    asyncio.run(run_pilot_verification())