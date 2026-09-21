from enum import Enum
from pathlib import Path
from typing import List, Optional
import yaml
from pydantic import BaseModel, Field, HttpUrl, field_validator


class PlanTier(str, Enum):
    STARTER = "starter"
    PRO = "pro"
    ENTERPRISE = "enterprise"


class TenantStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    PENDING = "pending"


class TenantConfig(BaseModel):
    id: str = Field(..., pattern=r"^tenant_[a-z0-9_]+$", description="Unique tenant slug")
    business_name: str = Field(..., min_length=2, max_length=100)
    owner_name: str = Field(..., min_length=2, max_length=100)
    whatsapp_number: str = Field(..., description="E.164 formatted WhatsApp phone number")
    active_plan: PlanTier = Field(default=PlanTier.STARTER)
    status: TenantStatus = Field(default=TenantStatus.ACTIVE)
    website_url: Optional[HttpUrl] = None
    target_keywords: List[str] = Field(default_factory=list)
    enabled_skills: List[str] = Field(default_factory=list)

    @field_validator("whatsapp_number")
    @classmethod
    def validate_e164(cls, v: str) -> str:
        clean = v.strip().replace(" ", "").replace("-", "")
        if not clean.startswith("+") or not clean[1:].isdigit():
            raise ValueError(f"Phone number {v} must be in valid E.164 international format (e.g. +971501234567)")
        return clean


class TenantsRegistry(BaseModel):
    tenants: List[TenantConfig]


def load_tenants_registry(config_path: str = "config/tenants.yaml") -> TenantsRegistry:
    """Loads and validates the tenants configuration registry."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found at: {config_path}")
    
    with open(path, "r", encoding="utf-8") as f:
        raw_data = yaml.safe_load(f)
        
    return TenantsRegistry(**raw_data)


if __name__ == "__main__":
    try:
        registry = load_tenants_registry()
        print(f"[OK] Validation passed: Successfully loaded {len(registry.tenants)} tenants.")
        for tenant in registry.tenants:
            print(f" - {tenant.business_name} ({tenant.id}) | Plan: {tenant.active_plan.value} | Skills: {len(tenant.enabled_skills)}")
    except Exception as e:
        print(f"[ERROR] Tenant config validation failed: {e}")