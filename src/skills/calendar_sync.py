import asyncio
from datetime import datetime, timedelta
import logging
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger("calendar_sync")


class CalendarSlot(BaseModel):
    start_time: datetime
    end_time: datetime
    is_available: bool = True
    summary: Optional[str] = None


class BookingRequest(BaseModel):
    tenant_id: str
    client_name: str
    client_contact: str
    service_title: str
    start_time: datetime
    duration_minutes: int = 60
    notes: Optional[str] = None
    buffer_minutes: int = 15


class CalendarEvent(BaseModel):
    event_id: str
    tenant_id: str
    title: str
    start_time: datetime
    end_time: datetime
    client_name: str
    client_contact: str
    status: str = "confirmed"  # 'confirmed', 'tentative', 'cancelled'


class CalendarSyncSkill:
    """Manages calendar slot detection, conflict resolution, and booking creation.

    Includes mock capabilities for testing environments without Google API credentials.
    """

    def __init__(self, tenant_id: str, credentials_path: Optional[str] = None):
        self.tenant_id = tenant_id
        self.credentials_path = credentials_path
        # In-memory mock event registry for local development / testing
        self._mock_events: List[CalendarEvent] = []

    async def get_available_slots(
        self,
        date: datetime,
        day_start_hour: int = 9,
        day_end_hour: int = 18,
        slot_duration_minutes: int = 60,
    ) -> List[CalendarSlot]:
        """Calculates free slots for a given day respecting existing bookings."""
        slots: List[CalendarSlot] = []
        current_time = datetime(
            date.year, date.month, date.day, day_start_hour, 0
        )
        end_boundary = datetime(
            date.year, date.month, date.day, day_end_hour, 0
        )

        while (
            current_time + timedelta(minutes=slot_duration_minutes)
            <= end_boundary
        ):
            slot_end = current_time + timedelta(minutes=slot_duration_minutes)

            # Check overlap against booked events
            conflict = False
            for ev in self._mock_events:
                if ev.status != "cancelled":
                    if not (slot_end <= ev.start_time or current_time >= ev.end_time):
                        conflict = True
                        break

            slots.append(
                CalendarSlot(
                    start_time=current_time,
                    end_time=slot_end,
                    is_available=not conflict,
                    summary="Available" if not conflict else "Booked",
                )
            )

            current_time = slot_end

        return slots

    async def create_booking(self, request: BookingRequest) -> CalendarEvent:
        """Schedules an appointment after ensuring no conflicting overlaps."""
        total_end = request.start_time + timedelta(
            minutes=request.duration_minutes + request.buffer_minutes
        )

        for ev in self._mock_events:
            if ev.status != "cancelled":
                if not (total_end <= ev.start_time or request.start_time >= ev.end_time):
                    raise ValueError(
                        f"Slot conflict: Proposed booking overlaps with '{ev.title}' "
                        f"({ev.start_time.strftime('%H:%M')} - {ev.end_time.strftime('%H:%M')})."
                    )

        event_id = f"evt_{int(request.start_time.timestamp())}_{request.tenant_id[:6]}"
        event = CalendarEvent(
            event_id=event_id,
            tenant_id=request.tenant_id,
            title=f"{request.service_title} - {request.client_name}",
            start_time=request.start_time,
            end_time=request.start_time + timedelta(minutes=request.duration_minutes),
            client_name=request.client_name,
            client_contact=request.client_contact,
            status="confirmed",
        )

        self._mock_events.append(event)
        return event

    async def cancel_booking(self, event_id: str) -> bool:
        """Marks an event as cancelled to release the time slot."""
        for ev in self._mock_events:
            if ev.event_id == event_id:
                ev.status = "cancelled"
                return True
        return False


if __name__ == "__main__":
    async def _test():
        cal = CalendarSyncSkill(tenant_id="tenant_cleaning_001")

        # Test booking an appointment
        today = datetime.now()
        target_slot = datetime(today.year, today.month, today.day, 10, 0)
        req = BookingRequest(
            tenant_id="tenant_cleaning_001",
            client_name="Sarah Jenkins",
            client_contact="+971501234567",
            service_title="Deep Villa Cleaning (3BR)",
            start_time=target_slot,
            duration_minutes=120,
            buffer_minutes=30,
        )

        created = await cal.create_booking(req)
        print(f"[OK] Booking confirmed: {created.title} (ID: {created.event_id})")

        # Test conflict detection for overlapping time
        try:
            overlap_req = BookingRequest(
                tenant_id="tenant_cleaning_001",
                client_name="Mark Vance",
                client_contact="+971507654321",
                service_title="Kitchen Sanitization",
                start_time=target_slot + timedelta(minutes=30),
                duration_minutes=60,
            )
            await cal.create_booking(overlap_req)
            print("[FAIL] Conflict check missed overlap.")
        except ValueError as e:
            print(f"[OK] Conflict detected correctly: {e}")

        # Test available slot generation
        slots = await cal.get_available_slots(today, day_start_hour=9, day_end_hour=14)
        print(f"[OK] Generated {len(slots)} schedule slots:")
        for s in slots:
            status = "FREE" if s.is_available else "BUSY"
            print(f"  {s.start_time.strftime('%H:%M')} - {s.end_time.strftime('%H:%M')}: [{status}]")

    asyncio.run(_test())