import asyncio
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import logging
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger("invoicing")


class LineItem(BaseModel):
    description: str
    quantity: Decimal = Field(default=Decimal("1.0"), ge=0.01)
    unit_price: Decimal = Field(ge=0.0)
    tax_rate: Decimal = Field(default=Decimal("0.05"), ge=0.0)  # Default 5% VAT

    @property
    def subtotal(self) -> Decimal:
        return (self.quantity * self.unit_price).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    @property
    def tax_amount(self) -> Decimal:
        return (self.subtotal * self.tax_rate).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    @property
    def total(self) -> Decimal:
        return self.subtotal + self.tax_amount


class InvoiceDraft(BaseModel):
    invoice_number: str
    tenant_id: str
    client_name: str
    client_contact: str
    currency: str = "AED"
    items: List[LineItem]
    deposit_percentage: Decimal = Field(default=Decimal("0.0"), ge=0.0, le=100.0)
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    status: str = "draft"  # 'draft', 'sent', 'paid', 'cancelled'

    @property
    def subtotal(self) -> Decimal:
        return sum((item.subtotal for item in self.items), Decimal("0.00"))

    @property
    def total_tax(self) -> Decimal:
        return sum((item.tax_amount for item in self.items), Decimal("0.00"))

    @property
    def grand_total(self) -> Decimal:
        return self.subtotal + self.total_tax

    @property
    def required_deposit(self) -> Decimal:
        if self.deposit_percentage <= Decimal("0.0"):
            return Decimal("0.00")
        return (self.grand_total * (self.deposit_percentage / Decimal("100.0"))).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    @property
    def balance_due(self) -> Decimal:
        return self.grand_total - self.required_deposit

    def format_whatsapp_message(self, business_name: str) -> str:
        """Formats the invoice as a concise, customer-ready WhatsApp summary."""
        lines = [
            f"📄 *ESTIMATE / INVOICE: {self.invoice_number}*",
            f"Business: *{business_name}*",
            f"Client: {self.client_name}",
            "---",
        ]
        for item in self.items:
            lines.append(
                f"• {item.description} (x{item.quantity}) - {self.currency} {item.total:.2f}"
            )

        lines.append("---")
        lines.append(f"Subtotal: {self.currency} {self.subtotal:.2f}")
        lines.append(f"VAT / Tax: {self.currency} {self.total_tax:.2f}")
        lines.append(f"*Total: {self.currency} {self.grand_total:.2f}*")

        if self.required_deposit > Decimal("0.00"):
            lines.append(
                f"⚡ *Upfront Deposit Required ({self.deposit_percentage:.0f}%): {self.currency} {self.required_deposit:.2f}*"
            )
            lines.append(f"Balance on Completion: {self.currency} {self.balance_due:.2f}")

        if self.notes:
            lines.append(f"\n_Note: {self.notes}_")

        return "\n".join(lines)


class InvoicingSkill:
    """Manages quote and invoice generation, counter sequences, and tax breakdowns."""

    def __init__(self, tenant_id: str, default_currency: str = "AED"):
        self.tenant_id = tenant_id
        self.default_currency = default_currency
        self._counter: int = 1000

    def generate_invoice(
        self,
        client_name: str,
        client_contact: str,
        items: List[LineItem],
        deposit_percentage: Decimal = Decimal("0.0"),
        notes: Optional[str] = None,
    ) -> InvoiceDraft:
        """Constructs an invoice draft with computed totals and deposit splits."""
        self._counter += 1
        year = datetime.utcnow().year
        prefix = self.tenant_id.replace("tenant_", "")[:4].upper()
        inv_number = f"INV-{prefix}-{year}-{self._counter}"

        return InvoiceDraft(
            invoice_number=inv_number,
            tenant_id=self.tenant_id,
            client_name=client_name,
            client_contact=client_contact,
            currency=self.default_currency,
            items=items,
            deposit_percentage=deposit_percentage,
            notes=notes,
        )


if __name__ == "__main__":
    def _test():
        billing = InvoicingSkill(tenant_id="tenant_curtains_001", default_currency="AED")

        items = [
            LineItem(
                description="Custom Motorized Blackout Curtains (Living Room)",
                quantity=Decimal("2.0"),
                unit_price=Decimal("1250.00"),
                tax_rate=Decimal("0.05"),
            ),
            LineItem(
                description="Ceiling Track Installation & Calibration",
                quantity=Decimal("1.0"),
                unit_price=Decimal("350.00"),
                tax_rate=Decimal("0.05"),
            ),
        ]

        # 50% deposit policy as defined in the memory tree
        invoice = billing.generate_invoice(
            client_name="Ali Al-Maktoum",
            client_contact="+971509988776",
            items=items,
            deposit_percentage=Decimal("50.0"),
            notes="Fabric cutting begins upon deposit receipt. 7 working days lead time.",
        )

        print(f"[OK] Invoice Created: {invoice.invoice_number}")
        print(f" - Subtotal: {invoice.currency} {invoice.subtotal:.2f}")
        print(f" - VAT (5%): {invoice.currency} {invoice.total_tax:.2f}")
        print(f" - Grand Total: {invoice.currency} {invoice.grand_total:.2f}")
        print(f" - Upfront Deposit (50%): {invoice.currency} {invoice.required_deposit:.2f}")
        print(f" - Remaining Balance: {invoice.currency} {invoice.balance_due:.2f}")
        print("\nWhatsApp Preview:\n" + invoice.format_whatsapp_message("Royal Drapery & Blinds"))

    _test()