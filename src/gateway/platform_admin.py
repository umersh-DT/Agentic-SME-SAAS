import asyncio
import logging
import os
import re
from typing import Any, Dict, List, Optional, Set

import yaml

from src.gateway.stripe_billing import atomic_write_yaml
from src.utils.security import TenantConfig, describe_error_safely, mask_phone_number, normalize_phone_number

logger = logging.getLogger("platform_admin")

ADMIN_HELP = (
    "Admin commands:\n"
    "• list businesses\n"
    "• add business Ali Cleaning owner +971500000001\n"
    "• add staff +971500000002 to Ali Cleaning\n"
    "• remove staff +971500000002 from Ali Cleaning\n"
    "• set website https://example.com for Ali Cleaning\n"
    "• set calendar owner@example.com for Ali Cleaning (the owner's Google Calendar address)\n"
    "• set hours 09:00-18:00 for Ali Cleaning\n"
    "• set timezone Asia/Dubai for Ali Cleaning\n"
    "• set search console sc-domain:example.com for Ali Cleaning\n"
    "• remove business Ali Cleaning (its saved data is kept)"
)

PHONE = r"(\+?[\d][\d\s()-]{6,})"
ADMIN_HELP_RE = re.compile(r"^\s*admin(?:\s+help)?\s*[?.!]*\s*$", re.IGNORECASE)
LIST_RE = re.compile(r"^\s*(?:list|show)\s+(?:all\s+)?business(?:es)?\s*[?.!]*\s*$", re.IGNORECASE)
ADD_BUSINESS_RE = re.compile(rf"^\s*add\s+business\s+(.+?)\s+owner\s+{PHONE}\s*$", re.IGNORECASE)
REMOVE_BUSINESS_RE = re.compile(r"^\s*remove\s+business\s+(.+?)\s*$", re.IGNORECASE)
ADD_STAFF_RE = re.compile(rf"^\s*add\s+staff\s+{PHONE}\s+to\s+(.+?)\s*$", re.IGNORECASE)
REMOVE_STAFF_RE = re.compile(rf"^\s*remove\s+staff\s+{PHONE}\s+from\s+(.+?)\s*$", re.IGNORECASE)
SET_RE = re.compile(
    r"^\s*set\s+(website|calendar|hours|timezone|search\s+console)\s+(\S+)\s+for\s+(.+?)\s*$", re.IGNORECASE
)
SET_FIELDS = {
    "website": "website_url",
    "calendar": "google_calendar_id",
    "hours": "business_hours",
    "timezone": "timezone",
    "search console": "search_console_property",
}
ALL_COMMANDS = (ADMIN_HELP_RE, LIST_RE, ADD_BUSINESS_RE, REMOVE_BUSINESS_RE, ADD_STAFF_RE, REMOVE_STAFF_RE, SET_RE)

_write_lock = asyncio.Lock()


class AdminError(Exception):
    """A problem to report back to the admin in plain words."""


def admin_phones() -> Set[str]:
    raw = os.getenv("PLATFORM_ADMIN_PHONES", "")
    return {normalize_phone_number(p) for p in raw.split(",") if normalize_phone_number(p)}


def is_platform_admin(phone: str) -> bool:
    return normalize_phone_number(phone) in admin_phones()


def is_admin_command(text: str) -> bool:
    return any(pattern.match(text or "") for pattern in ALL_COMMANDS)


def _e164(raw: str) -> str:
    digits = normalize_phone_number(raw)
    return digits if digits.startswith("+") else f"+{digits}"


def _config_path() -> str:
    return os.environ.get("TENANTS_CONFIG_PATH", "config/tenants.yaml")


def _load_entries() -> List[Dict[str, Any]]:
    path = _config_path()
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    tenants = data.get("tenants") or []
    return list(tenants.values()) if isinstance(tenants, dict) else list(tenants)


def _find(entries: List[Dict[str, Any]], name: str) -> Dict[str, Any]:
    wanted = name.strip().strip("\"'").lower()
    for entry in entries:
        if wanted in (str(entry.get("business_name", "")).lower(), str(entry.get("tenant_id", "")).lower()):
            return entry
    raise AdminError(f"I couldn't find a business called \"{name.strip()}\". Send \"list businesses\" to see them.")


def _new_tenant_id(name: str, entries: List[Dict[str, Any]]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "business"
    base = f"tenant_{slug}"[:60]
    existing = {e.get("tenant_id") for e in entries}
    candidate, n = base, 2
    while candidate in existing:
        candidate, n = f"{base}_{n}", n + 1
    return candidate


def _validate_and_save(entries: List[Dict[str, Any]]) -> None:
    """Validates every business and that no phone belongs to two businesses, then writes atomically."""
    owners: Dict[str, str] = {}
    for entry in entries:
        try:
            config = TenantConfig(**entry)
        except Exception as e:
            raise AdminError(f"Not saved: {describe_error_safely(e)}") from None
        for phone in config.get_all_associated_phones():
            if phone in owners and owners[phone] != config.business_name:
                raise AdminError(
                    f"Not saved: {mask_phone_number(phone)} already belongs to {owners[phone]}. "
                    "A number can only belong to one business."
                )
            owners[phone] = config.business_name
    try:
        atomic_write_yaml(_config_path(), {"tenants": entries})
    except PermissionError:
        raise AdminError(
            "Not saved: the server doesn't let the app write the business list. "
            "On the server run: chown -R 1000:1000 /root/Agentic-SME-SAAS/config"
        ) from None


def _apply(text: str, entries: List[Dict[str, Any]]) -> str:
    """Applies one change command to the entries in memory. Returns the confirmation text."""
    if m := ADD_BUSINESS_RE.match(text):
        name, owner = m.group(1).strip().strip("\"'"), _e164(m.group(2))
        if any(str(e.get("business_name", "")).lower() == name.lower() for e in entries):
            raise AdminError(f"A business called \"{name}\" already exists.")
        tenant_id = _new_tenant_id(name, entries)
        entries.append({
            "tenant_id": tenant_id, "business_name": name, "plan_tier": "starter",
            "subscription_status": "active", "owner_phone": owner, "staff_phones": [],
            "enabled_skills": ["invoicing", "memory_tree"],
        })
        return f"Added {name} with owner {owner}. They can message the assistant now."

    if m := REMOVE_BUSINESS_RE.match(text):
        entry = _find(entries, m.group(1))
        entries.remove(entry)
        return (f"Removed {entry['business_name']} from the business list. Its saved data (rules, invoices) "
                "is kept on the server; adding it back with the same name restores access.")

    if m := ADD_STAFF_RE.match(text):
        phone, entry = _e164(m.group(1)), _find(entries, m.group(2))
        staff = list(entry.get("staff_phones") or [])
        if phone in staff:
            raise AdminError(f"{phone} is already staff at {entry['business_name']}.")
        entry["staff_phones"] = staff + [phone]
        return f"Added {phone} as staff at {entry['business_name']}."

    if m := REMOVE_STAFF_RE.match(text):
        phone, entry = _e164(m.group(1)), _find(entries, m.group(2))
        staff = [p for p in (entry.get("staff_phones") or []) if normalize_phone_number(p) != phone]
        if len(staff) == len(entry.get("staff_phones") or []):
            raise AdminError(f"{phone} isn't staff at {entry['business_name']}.")
        entry["staff_phones"] = staff
        return f"Removed {phone} from staff at {entry['business_name']}."

    if m := SET_RE.match(text):
        field_word = re.sub(r"\s+", " ", m.group(1).lower())
        value, entry = m.group(2).strip(), _find(entries, m.group(3))
        if field_word == "website" and not value.startswith(("http://", "https://")):
            value = f"https://{value}"
        entry[SET_FIELDS[field_word]] = value
        return f"Set {field_word} for {entry['business_name']} to {value}."

    raise AdminError(ADMIN_HELP)


async def handle_admin_command(text: str, directory: Any) -> str:
    """Runs an admin command from WhatsApp, saves the business list and reloads it without a restart."""
    if ADMIN_HELP_RE.match(text):
        return ADMIN_HELP
    if LIST_RE.match(text):
        entries = _load_entries()
        if not entries:
            return "No businesses yet. Add one: add business Ali Cleaning owner +971500000001"
        lines = []
        for e in entries:
            staff = len(e.get("staff_phones") or [])
            extras = [label for label, key in (("calendar", "google_calendar_id"), ("website", "website_url"))
                      if e.get(key)]
            lines.append(f"• {e.get('business_name')} — owner {e.get('owner_phone') or 'none'}, {staff} staff"
                         + (f", {', '.join(extras)}" if extras else ""))
        return f"{len(entries)} businesses:\n" + "\n".join(lines)

    async with _write_lock:
        try:
            entries = _load_entries()
            confirmation = _apply(text, entries)
            _validate_and_save(entries)
        except AdminError as e:
            return str(e)
        directory.reload_tenants()
    logger.info(f"[ADMIN] Business list changed: {confirmation.split('.')[0]}")
    return confirmation
