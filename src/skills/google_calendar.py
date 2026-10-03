from datetime import date, datetime, time, timedelta
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from src.skills.google_service_account import google_auth_headers, service_account_email

logger = logging.getLogger("google_calendar")

CALENDAR_SCOPES = ("https://www.googleapis.com/auth/calendar",)
CALENDAR_API = "https://www.googleapis.com/calendar/v3"
HOURS_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


class CalendarNotShared(Exception):
    """The business calendar has not been shared with the app's service account."""


class SlotUnavailable(Exception):
    """The requested time is in the past, outside business hours, or already taken."""


def parse_business_hours(hours: str) -> Tuple[time, time]:
    match = HOURS_RE.match(hours or "")
    if not match:
        raise ValueError(f"Business hours must look like 09:00-18:00, got '{hours}'.")
    h1, m1, h2, m2 = (int(g) for g in match.groups())
    opens, closes = time(h1, m1), time(h2, m2)
    if opens >= closes:
        raise ValueError("Business hours must open before they close.")
    return opens, closes


def not_shared_message() -> str:
    email = service_account_email() or "the assistant's Google service account"
    return (
        "I can't reach the business calendar. The owner needs to share their Google Calendar with "
        f"{email} (permission: 'Make changes to events')."
    )


class GoogleCalendarBooking:
    """Checks availability and books appointments in one business's Google Calendar."""

    def __init__(
        self,
        calendar_id: str,
        timezone: str = "Asia/Dubai",
        business_hours: str = "09:00-18:00",
        slot_minutes: int = 60,
        now_fn: Optional[Callable[[], datetime]] = None,
    ):
        self.calendar_id = calendar_id
        self.tz = ZoneInfo(timezone)
        self.timezone = timezone
        self.opens, self.closes = parse_business_hours(business_hours)
        self.slot_minutes = slot_minutes
        self._now_fn = now_fn or (lambda: datetime.now(self.tz))

    def now(self) -> datetime:
        return self._now_fn().astimezone(self.tz)

    def _day_window(self, day: date) -> Tuple[datetime, datetime]:
        return (datetime.combine(day, self.opens, tzinfo=self.tz), datetime.combine(day, self.closes, tzinfo=self.tz))

    async def _request(self, method: str, url: str, **kwargs: Any) -> Dict[str, Any]:
        headers = await google_auth_headers(CALENDAR_SCOPES)
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.request(method, url, headers=headers, **kwargs)
        if resp.status_code in (403, 404):
            logger.warning(f"[CALENDAR] Access denied ({resp.status_code}) for the business calendar.")
            raise CalendarNotShared()
        if resp.status_code >= 400:
            raise RuntimeError(f"Google Calendar error {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    async def busy_periods(self, start: datetime, end: datetime) -> List[Tuple[datetime, datetime]]:
        data = await self._request("POST", f"{CALENDAR_API}/freeBusy", json={
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "timeZone": self.timezone,
            "items": [{"id": self.calendar_id}],
        })
        calendar = (data.get("calendars") or {}).get(self.calendar_id) or {}
        if calendar.get("errors"):
            raise CalendarNotShared()
        return [
            (datetime.fromisoformat(b["start"].replace("Z", "+00:00")).astimezone(self.tz),
             datetime.fromisoformat(b["end"].replace("Z", "+00:00")).astimezone(self.tz))
            for b in calendar.get("busy") or []
        ]

    async def free_slots(self, day: date) -> List[Tuple[datetime, datetime]]:
        """Free slots of slot_minutes within business hours (only future slots for today)."""
        opens, closes = self._day_window(day)
        busy = await self.busy_periods(opens, closes)
        step = timedelta(minutes=self.slot_minutes)
        now = self.now()
        slots, cursor = [], opens
        while cursor + step <= closes:
            end = cursor + step
            if cursor >= now and not any(b_start < end and cursor < b_end for b_start, b_end in busy):
                slots.append((cursor, end))
            cursor = end
        return slots

    async def book(
        self,
        customer_name: str,
        start: datetime,
        duration_minutes: int,
        service: str,
        customer_phone: str = "",
        booked_by: str = "",
    ) -> Dict[str, Any]:
        start = start.replace(tzinfo=self.tz) if start.tzinfo is None else start.astimezone(self.tz)
        end = start + timedelta(minutes=duration_minutes)
        opens, closes = self._day_window(start.date())
        if start < self.now():
            raise SlotUnavailable("That time has already passed.")
        if start < opens or end > closes:
            raise SlotUnavailable(
                f"That's outside business hours ({self.opens.strftime('%H:%M')}-{self.closes.strftime('%H:%M')})."
            )
        if await self.busy_periods(start, end):
            raise SlotUnavailable("That time is already booked.")

        description = "\n".join(line for line in [
            "Booked via the WhatsApp business assistant.",
            f"Customer: {customer_name}",
            f"Customer phone: {customer_phone}" if customer_phone else "",
            f"Booked by: {booked_by}" if booked_by else "",
        ] if line)
        event = await self._request(
            "POST", f"{CALENDAR_API}/calendars/{quote(self.calendar_id, safe='')}/events",
            json={
                "summary": f"{service} - {customer_name}",
                "description": description,
                "start": {"dateTime": start.isoformat(), "timeZone": self.timezone},
                "end": {"dateTime": end.isoformat(), "timeZone": self.timezone},
            },
        )
        return {"id": event.get("id", ""), "start": start, "end": end}

    async def events_on(self, day: date) -> List[Dict[str, Any]]:
        day_start = datetime.combine(day, time(0, 0), tzinfo=self.tz)
        data = await self._request(
            "GET", f"{CALENDAR_API}/calendars/{quote(self.calendar_id, safe='')}/events",
            params={
                "timeMin": day_start.isoformat(),
                "timeMax": (day_start + timedelta(days=1)).isoformat(),
                "singleEvents": "true",
                "orderBy": "startTime",
                "timeZone": self.timezone,
            },
        )
        events = []
        for item in data.get("items") or []:
            start_raw = (item.get("start") or {}).get("dateTime")
            end_raw = (item.get("end") or {}).get("dateTime")
            if not start_raw:  # all-day event
                events.append({"start": None, "end": None, "summary": item.get("summary", "(no title)")})
                continue
            events.append({
                "start": datetime.fromisoformat(start_raw.replace("Z", "+00:00")).astimezone(self.tz),
                "end": datetime.fromisoformat(end_raw.replace("Z", "+00:00")).astimezone(self.tz),
                "summary": item.get("summary", "(no title)"),
            })
        return events
