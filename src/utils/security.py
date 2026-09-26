import os
import re
from pathlib import Path
from typing import List, Literal, Optional
from pydantic import BaseModel, Field, field_validator


TENANT_ID_REGEX = re.compile(r"^tenant_[a-z0-9_]+$")


def normalize_phone_number(phone: Optional[str]) -> str:
    """Normalizes phone numbers by stripping whatsapp prefixes, spaces, and hyphens."""
    if not phone:
        return ""
    cleaned = phone.strip()
    if cleaned.startswith("whatsapp:"):
        cleaned = cleaned.replace("whatsapp:", "")
    return cleaned.replace(" ", "").replace("-", "").strip()


class TenantConfig(BaseModel):
    """Canonical single-tenant configuration schema."""
    tenant_id: str = Field(..., description="Unique tenant identifier matching ^tenant_[a-z0-9_]+$")
    business_name: str = Field(..., min_length=2)
    plan_tier: Literal["starter", "pro", "enterprise"] = "starter"
    subscription_status: Literal["active", "past_due", "canceled"] = "active"
    whatsapp_number: Optional[str] = Field(None, description="Primary business WhatsApp phone number")
    owner_phone: Optional[str] = Field(None, description="Owner contact phone number")
    staff_phones: List[str] = Field(default_factory=list, description="Authorized staff phone numbers")
    stripe_customer_id: Optional[str] = None
    stripe_subscription_id: Optional[str] = None
    enabled_skills: List[str] = Field(
        default_factory=lambda: ["calendar_sync", "invoicing", "research", "memory_tree"]
    )
    website_url: Optional[str] = None
    target_keywords: List[str] = Field(default_factory=list)

    @field_validator("tenant_id")
    @classmethod
    def validate_tenant_id_regex(cls, v: str) -> str:
        if not TENANT_ID_REGEX.match(v):
            raise ValueError(
                f"Invalid tenant_id format: '{v}'. Must match pattern ^tenant_[a-z0-9_]+$."
            )
        return v

    @field_validator("whatsapp_number", "owner_phone", mode="before")
    @classmethod
    def sanitize_single_phone(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        norm = normalize_phone_number(v)
        return norm if norm else None

    @field_validator("staff_phones", mode="before")
    @classmethod
    def sanitize_phone_list(cls, v: Optional[List[str]]) -> List[str]:
        if not v:
            return []
        cleaned_list = [normalize_phone_number(p) for p in v if normalize_phone_number(p)]
        return list(dict.fromkeys(cleaned_list))

    def get_all_associated_phones(self) -> List[str]:
        """Collects all unique phone numbers associated with this tenant."""
        phones = []
        if self.whatsapp_number:
            phones.append(self.whatsapp_number)
        if self.owner_phone:
            phones.append(self.owner_phone)
        phones.extend(self.staff_phones)
        return list(dict.fromkeys(phones))


def get_safe_tenant_storage_path(tenant_id: str, base_dir: str = "/app/data/tenants") -> Path:
    """Guarantees path containment within base directory to prevent directory traversal."""
    if not TENANT_ID_REGEX.match(tenant_id):
        raise ValueError(f"Dangerous or invalid tenant_id: {tenant_id}")
    
    base_path = Path(base_dir).resolve()
    target_path = (base_path / f"{tenant_id}.sqlite").resolve()
    
    if not str(target_path).startswith(str(base_path)):
        raise PermissionError(f"Directory traversal detected for tenant: {tenant_id}")
    return target_path