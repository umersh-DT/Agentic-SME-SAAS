import os
import re
from pathlib import Path
from typing import List, Literal, Optional
from pydantic import BaseModel, Field, ValidationError, field_validator


TENANT_ID_REGEX = re.compile(r"^tenant_[a-z0-9_]+$")
# Strict E.164: must start with '+' followed by country code and subscriber digits (total 7-15 digits)
E164_REGEX = re.compile(r"^\+[1-9]\d{6,14}$")


def normalize_phone_number(phone: Optional[str]) -> str:
    """Normalizes phone numbers by stripping whatsapp: prefix, spaces, and formatting characters."""
    if not phone:
        return ""
    cleaned = str(phone).strip()
    if cleaned.startswith("whatsapp:"):
        cleaned = cleaned.replace("whatsapp:", "")
    return cleaned.replace(" ", "").replace("-", "").replace("(", "").replace(")", "").strip()


def mask_phone_number(phone: Optional[str]) -> str:
    """Masks a phone number for privacy-safe logging (e.g. +97150***67)."""
    cleaned = normalize_phone_number(phone)
    if len(cleaned) >= 8:
        return f"{cleaned[:5]}***{cleaned[-2:]}"
    return "***"


def describe_error_safely(error: Exception) -> str:
    """Describes an exception for logs/responses without echoing raw input values (e.g. phone numbers)."""
    if isinstance(error, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
            for err in error.errors(include_url=False, include_input=False)
        )
    return str(error)


class TenantConfig(BaseModel):
    """Canonical single-tenant configuration schema."""
    tenant_id: str = Field(..., description="Unique tenant identifier matching ^tenant_[a-z0-9_]+$")
    business_name: str = Field(..., min_length=2)
    plan_tier: Literal["starter", "pro", "enterprise"] = "starter"
    subscription_status: Literal["active", "past_due", "canceled"] = "active"
    whatsapp_number: Optional[str] = Field(None, description="Primary business WhatsApp phone number in E.164")
    owner_phone: Optional[str] = Field(None, description="Owner contact phone number in E.164")
    staff_phones: List[str] = Field(default_factory=list, description="Authorized staff phone numbers in E.164")
    stripe_customer_id: Optional[str] = None
    stripe_subscription_id: Optional[str] = None
    enabled_skills: List[str] = Field(
        default_factory=lambda: ["calendar_sync", "invoicing", "research", "memory_tree"]
    )
    website_url: Optional[str] = None
    target_keywords: List[str] = Field(default_factory=list)
    # Google Calendar to book into (usually the owner's Gmail address), shared with the service account
    google_calendar_id: Optional[str] = None
    timezone: str = "Asia/Dubai"
    business_hours: str = "09:00-18:00"
    appointment_minutes: int = Field(default=60, ge=5, le=480)
    # Search Console property, e.g. "sc-domain:example.com" or "https://example.com/"
    search_console_property: Optional[str] = None

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"Unknown timezone '{v}' (use names like Asia/Dubai).") from e
        return v

    @field_validator("business_hours")
    @classmethod
    def validate_business_hours(cls, v: str) -> str:
        if not re.match(r"^\s*\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}\s*$", v or ""):
            raise ValueError(f"business_hours must look like 09:00-18:00, got '{v}'.")
        return v.replace(" ", "")

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
    def validate_and_sanitize_single_phone(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        norm = normalize_phone_number(v)
        if not E164_REGEX.match(norm):
            raise ValueError(f"Invalid E.164 phone number format: '{mask_phone_number(norm)}'. Must include leading '+' and country code.")
        return norm

    @field_validator("staff_phones", mode="before")
    @classmethod
    def validate_and_sanitize_phone_list(cls, v: Optional[List[str]]) -> List[str]:
        if not v:
            return []
        cleaned_list = []
        for p in v:
            norm = normalize_phone_number(p)
            if not E164_REGEX.match(norm):
                raise ValueError(f"Invalid E.164 phone number in staff_phones: '{mask_phone_number(norm)}'. Must include leading '+' and country code.")
            cleaned_list.append(norm)
        return list(dict.fromkeys(cleaned_list))

    def get_all_associated_phones(self) -> List[str]:
        """Collects all unique normalized phone numbers associated with this tenant."""
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