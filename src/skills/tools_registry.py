from functools import partial
import logging
from typing import Any, Callable, Dict, List, Tuple

from src.skills.memory_tree import TenantMemoryTree

logger = logging.getLogger("tools_registry")


async def _execute_create_invoice(
    tenant_id: str,
    customer_name: str,
    amount: float,
    description: str = "Standard services",
) -> str:
    """Internal implementation for creating customer invoices scoped to tenant_id."""
    logger.info(
        f"[TOOL INVOICE] Tenant={tenant_id} | Customer={customer_name} | Amount={amount} | Desc={description}"
    )
    # Simulated invoice creation record in tenant SQLite
    return (
        f"SUCCESS: Invoice generated for {customer_name}.\n"
        f"Amount: ${amount:.2f}\n"
        f"Description: {description}\n"
        f"Status: Sent to client"
    )


async def _execute_search_memory(
    tenant_id: str,
    base_data_dir: str,
    query: str,
) -> str:
    """Internal implementation for searching verified business facts in Memory Tree."""
    logger.info(f"[TOOL MEMORY] Tenant={tenant_id} | Searching query: {query}")
    try:
        memory = TenantMemoryTree(tenant_id=tenant_id, base_data_dir=base_data_dir)
        await memory.initialize()
        results = await memory.search_memory(query=query, limit=3)
        if not results:
            return "No matching business policies or records found."
        formatted = "\n".join(f"- {r.get('content', '')}" for r in results)
        return f"Verified Records Found:\n{formatted}"
    except Exception as e:
        logger.error(f"[TOOL MEMORY ERROR] Tenant={tenant_id}: {e}")
        return "Failed to search business memory records."


def get_scoped_tools(
    tenant_id: str,
    base_data_dir: str = "/app/data/tenants",
) -> Tuple[List[Dict[str, Any]], Dict[str, Callable]]:
    """Returns OpenAI/LiteLLM tool definitions and executable callables with tenant_id bound.
    
    The LLM schema NEVER exposes tenant_id. The dispatcher injects tenant_id securely.
    """
    tools_schema = [
        {
            "type": "function",
            "function": {
                "name": "create_invoice",
                "description": "Generate and issue a customer invoice for services rendered.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "customer_name": {
                            "type": "string",
                            "description": "Full name or business name of the client being billed.",
                        },
                        "amount": {
                            "type": "number",
                            "description": "Total monetary amount for the invoice in USD.",
                        },
                        "description": {
                            "type": "string",
                            "description": "Line-item description of services or products.",
                        },
                    },
                    "required": ["customer_name", "amount"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_business_memory",
                "description": "Search verified business policies, pricing sheets, and past records.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The policy or topic to search (e.g. cancellation policy, hourly rate).",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
    ]

    # Map function names to server-bound callables
    callables_map = {
        "create_invoice": partial(_execute_create_invoice, tenant_id),
        "search_business_memory": partial(_execute_search_memory, tenant_id, base_data_dir),
    }

    return tools_schema, callables_map