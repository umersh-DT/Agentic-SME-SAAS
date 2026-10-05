from datetime import date, datetime
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import httpx

from src.gateway.dispatcher import TenantWorkerDispatcher
from src.skills.google_calendar import GoogleCalendarBooking, SlotUnavailable
from src.skills.tools_registry import get_scoped_tools
from src.skills.website_seo import build_seo_report
from src.utils.security import TenantConfig

DUBAI = ZoneInfo("Asia/Dubai")
CAL_ID = "owner.business@gmail.com"
ROBOT = "assistant-robot@pilot-project.iam.gserviceaccount.com"
NOW = datetime(2026, 10, 4, 8, 0, tzinfo=DUBAI)  # Sunday 08:00 Dubai time


def _resp(status: int, payload: dict, method: str = "POST") -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request(method, "https://www.googleapis.com"))


class FakeGoogle:
    """Records Google API calls and answers freeBusy / events like Google does."""

    def __init__(self, busy=None, events=None, freebusy_errors=None):
        self.busy = busy or []
        self.events = events or []
        self.freebusy_errors = freebusy_errors
        self.calls = []

    async def request(self, method, url, headers=None, json=None, params=None, **kwargs):
        self.calls.append({"method": method, "url": url, "json": json, "params": params, "headers": headers})
        if url.endswith("/freeBusy"):
            cal = {"busy": self.busy}
            if self.freebusy_errors:
                cal = {"errors": self.freebusy_errors, "busy": []}
            return _resp(200, {"calendars": {CAL_ID: cal}})
        if method == "POST" and url.endswith("/events"):
            return _resp(200, {"id": "evt_123", **json})
        if method == "GET" and url.endswith("/events"):
            return _resp(200, {"items": self.events}, "GET")
        raise AssertionError(f"unexpected call {method} {url}")


class CalendarBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.auth = patch("src.skills.google_calendar.google_auth_headers",
                          AsyncMock(return_value={"Authorization": "Bearer test-token"}))
        self.email = patch("src.skills.google_calendar.service_account_email", return_value=ROBOT)
        self.auth.start()
        self.email.start()

    def tearDown(self):
        self.auth.stop()
        self.email.stop()

    def calendar(self, hours="09:00-13:00"):
        return GoogleCalendarBooking(CAL_ID, "Asia/Dubai", hours, 60, now_fn=lambda: NOW)


class TestGoogleCalendar(CalendarBase):
    async def test_free_slots_skip_busy_times(self):
        google = FakeGoogle(busy=[{"start": "2026-10-05T06:00:00Z", "end": "2026-10-05T07:30:00Z"}])  # 10:00-11:30 Dubai
        with patch.object(httpx.AsyncClient, "request", google.request):
            slots = await self.calendar().free_slots(date(2026, 10, 5))
        self.assertEqual([s.strftime("%H:%M") for s, _ in slots], ["09:00", "12:00"])
        body = google.calls[0]["json"]
        self.assertEqual(body["items"], [{"id": CAL_ID}])
        self.assertEqual(body["timeMin"], "2026-10-05T09:00:00+04:00")
        self.assertEqual(google.calls[0]["headers"], {"Authorization": "Bearer test-token"})

    async def test_book_creates_event_with_customer_details(self):
        google = FakeGoogle()
        with patch.object(httpx.AsyncClient, "request", google.request):
            booking = await self.calendar("09:00-18:00").book(
                "Sara", datetime(2026, 10, 5, 15, 0), 60, "Deep cleaning", "+971500000009", "Staff +971502222222")
        insert = google.calls[-1]
        self.assertEqual(insert["url"], "https://www.googleapis.com/calendar/v3/calendars/owner.business%40gmail.com/events")
        self.assertEqual(insert["json"]["summary"], "Deep cleaning - Sara")
        self.assertEqual(insert["json"]["start"], {"dateTime": "2026-10-05T15:00:00+04:00", "timeZone": "Asia/Dubai"})
        self.assertEqual(insert["json"]["end"], {"dateTime": "2026-10-05T16:00:00+04:00", "timeZone": "Asia/Dubai"})
        self.assertIn("Customer phone: +971500000009", insert["json"]["description"])
        self.assertIn("Booked by: Staff +971502222222", insert["json"]["description"])
        self.assertEqual(booking["id"], "evt_123")

    async def test_book_refuses_taken_past_and_out_of_hours_times(self):
        google = FakeGoogle(busy=[{"start": "2026-10-05T11:00:00Z", "end": "2026-10-05T12:00:00Z"}])
        cal = self.calendar("09:00-18:00")
        with patch.object(httpx.AsyncClient, "request", google.request):
            with self.assertRaisesRegex(SlotUnavailable, "already booked"):
                await cal.book("Ali", datetime(2026, 10, 5, 15, 30), 60, "Visit")
            with self.assertRaisesRegex(SlotUnavailable, "already passed"):
                await cal.book("Ali", datetime(2026, 10, 3, 10, 0), 60, "Visit")
            with self.assertRaisesRegex(SlotUnavailable, "outside business hours"):
                await cal.book("Ali", datetime(2026, 10, 5, 17, 30), 60, "Visit")
        self.assertFalse(any(c["url"].endswith("/events") for c in google.calls), "nothing may be inserted")


class TestCalendarTools(CalendarBase):
    profile = {"google_calendar_id": CAL_ID, "timezone": "Asia/Dubai", "business_hours": "09:00-18:00",
               "appointment_minutes": 60, "booked_by": "Owner +971501234567"}

    def tools(self, profile=None):
        return get_scoped_tools("tenant_cal_01", tempfile.mkdtemp(), profile=profile if profile is not None else self.profile)

    async def test_calendar_tools_only_when_calendar_connected(self):
        names_with = [t["function"]["name"] for t in self.tools()[0]]
        names_without = [t["function"]["name"] for t in self.tools({})[0]]
        self.assertIn("book_appointment", names_with)
        self.assertNotIn("book_appointment", names_without)
        self.assertIn("website_seo_report", names_without)

    async def test_book_and_list_tools(self):
        google = FakeGoogle(events=[{"summary": "Deep cleaning - Sara",
                                     "start": {"dateTime": "2026-10-05T15:00:00+04:00"},
                                     "end": {"dateTime": "2026-10-05T16:00:00+04:00"}}])
        _, tools = self.tools()
        with patch.object(httpx.AsyncClient, "request", google.request), \
             patch.object(GoogleCalendarBooking, "now", return_value=NOW):
            booked = await tools["book_appointment"](customer_name="Sara", date="2026-10-05", time="15:00",
                                                     service="Deep cleaning")
            listed = await tools["list_appointments"](date="2026-10-05")
        self.assertEqual(booked, "Booked: Deep cleaning for Sara on Monday 05 October 2026, 15:00-16:00. "
                                 "It is now in the business Google Calendar.")
        self.assertEqual(listed, "Appointments on Monday 05 October 2026:\n- 15:00-16:00 Deep cleaning - Sara")

    async def test_unshared_calendar_explains_what_to_share(self):
        google = FakeGoogle(freebusy_errors=[{"domain": "global", "reason": "notFound"}])
        _, tools = self.tools()
        with patch.object(httpx.AsyncClient, "request", google.request):
            reply = await tools["check_availability"](date="2026-10-05")
        self.assertEqual(reply, "I can't reach the business calendar. The owner needs to share their Google Calendar "
                                f"with {ROBOT} (permission: 'Make changes to events').")

    async def test_business_calendar_setting_reaches_the_ai(self):
        from types import SimpleNamespace
        message = SimpleNamespace(content="Sure.", tool_calls=None, to_dict=lambda: {"role": "assistant", "content": "Sure."})
        response = SimpleNamespace(choices=[SimpleNamespace(message=message)],
                                   usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2))
        with tempfile.TemporaryDirectory() as data_dir:
            dispatcher = TenantWorkerDispatcher(data_root=data_dir)
            with patch.object(dispatcher.reply_skill, "send_reply", AsyncMock(return_value={"success": True})), \
                 patch("litellm.acompletion", AsyncMock(return_value=response)) as mock_llm:
                await dispatcher.process_incoming_message(
                    tenant_id="tenant_cal_02", from_number="+971501234567", owner_phone="+971501234567",
                    body="Is Monday free?", message_sid="SM_cal_q",
                    profile={"google_calendar_id": CAL_ID, "timezone": "Asia/Dubai", "business_hours": "10:00-19:00"},
                )
        kwargs = mock_llm.call_args.kwargs
        self.assertIn("book_appointment", [t["function"]["name"] for t in kwargs["tools"]])
        self.assertIn("business hours 10:00-19:00", kwargs["messages"][0]["content"])
        self.assertIn("(Asia/Dubai)", kwargs["messages"][0]["content"])

    async def test_disabled_calendar_api_is_reported_as_such(self):
        async def disabled(self_client, method, url, **kw):
            return _resp(403, {"error": {"code": 403, "message": "Google Calendar API has not been used in project "
                                         "123 before or it is disabled.", "status": "PERMISSION_DENIED",
                                         "details": [{"reason": "SERVICE_DISABLED"}]}})
        _, tools = self.tools()
        with patch.object(httpx.AsyncClient, "request", disabled):
            reply = await tools["check_availability"](date="2026-10-05")
        self.assertEqual(reply, "The Google Calendar API isn't switched on for the assistant's Google project yet. "
                                "The admin needs to enable \"Google Calendar API\" in Google Cloud Console "
                                "(APIs & Services → Library).")

    def test_tenant_settings_are_validated(self):
        with self.assertRaisesRegex(ValueError, "Unknown timezone"):
            TenantConfig(tenant_id="tenant_x", business_name="Xy", timezone="Dubai/Nowhere")
        with self.assertRaisesRegex(ValueError, "business_hours must look like"):
            TenantConfig(tenant_id="tenant_x", business_name="Xy", business_hours="9am to 6pm")


PAGESPEED_PAYLOAD = {
    "lighthouseResult": {
        "categories": {
            "performance": {"score": 0.62, "auditRefs": [
                {"id": "largest-contentful-paint", "weight": 25},
                {"id": "render-blocking-resources", "weight": 0},
            ]},
            "seo": {"score": 0.91, "auditRefs": [
                {"id": "image-alt", "weight": 1},
                {"id": "meta-description", "weight": 1},
                {"id": "document-title", "weight": 1},
            ]},
            "accessibility": {"score": 0.88, "auditRefs": []},
            "best-practices": {"score": 0.96, "auditRefs": []},
        },
        "audits": {
            "largest-contentful-paint": {"title": "Largest Contentful Paint", "score": 0.3,
                                         "scoreDisplayMode": "numeric", "displayValue": "3.9 s"},
            "render-blocking-resources": {"title": "Eliminate render-blocking resources", "score": 0.1,
                                          "scoreDisplayMode": "metricSavings"},
            "image-alt": {"title": "Image elements do not have [alt] attributes", "score": 0,
                          "scoreDisplayMode": "binary"},
            "meta-description": {"title": "Document has a meta description", "score": 1, "scoreDisplayMode": "binary"},
            "document-title": {"title": "Document has a <title> element", "score": 1, "scoreDisplayMode": "binary"},
        },
    }
}


class TestWebsiteSeo(unittest.IsolatedAsyncioTestCase):
    async def test_report_from_real_pagespeed_data(self):
        get = AsyncMock(return_value=_resp(200, PAGESPEED_PAYLOAD, "GET"))
        with patch.object(httpx.AsyncClient, "get", get):
            report = await build_seo_report("https://apexclean.example.com", None)
        self.assertEqual(report, "\n".join([
            "Website report for apexclean.example.com (mobile)",
            "• Speed: 62/100 (needs work)",
            "• SEO basics: 91/100 (good)",
            "• Accessibility: 88/100 (needs work)",
            "• Best practices: 96/100 (good)",
            "• Main content appears after 3.9 s (aim for under 2.5 s)",
            "",
            "Fix first:",
            "- Image elements do not have [alt] attributes",
            "- Largest Contentful Paint",
            "",
            "For real Google Search numbers (clicks, top searches), connect Google Search Console.",
        ]))
        params = get.call_args.kwargs["params"]
        self.assertIn(("url", "https://apexclean.example.com"), params)
        self.assertIn(("strategy", "mobile"), params)
        self.assertIn(("category", "BEST_PRACTICES"), params)

    async def test_search_console_numbers_are_included(self):
        posts = []

        async def fake_post(self_client, url, headers=None, json=None, **kw):
            posts.append({"url": url, "json": json, "headers": headers})
            if "dimensions" in json:
                return _resp(200, {"rows": [
                    {"keys": ["deep cleaning dubai"], "clicks": 40, "impressions": 2100},
                    {"keys": ["apex clean"], "clicks": 25, "impressions": 90},
                ]})
            return _resp(200, {"rows": [{"clicks": 120, "impressions": 15400, "position": 18.24}]})

        with patch.object(httpx.AsyncClient, "get", AsyncMock(return_value=_resp(200, PAGESPEED_PAYLOAD, "GET"))), \
             patch.object(httpx.AsyncClient, "post", fake_post), \
             patch("src.skills.website_seo.google_auth_headers", AsyncMock(return_value={"Authorization": "Bearer t"})), \
             patch("src.skills.website_seo.date") as fake_date:
            fake_date.today.return_value = date(2026, 10, 3)
            report = await build_seo_report("https://apexclean.example.com", "sc-domain:apexclean.example.com")

        self.assertIn("Google Search, last 28 days: 120 clicks, 15,400 times shown, average position 18.2", report)
        self.assertIn("1. deep cleaning dubai — 40 clicks, shown 2,100 times", report)
        self.assertIn("2. apex clean — 25 clicks, shown 90 times", report)
        self.assertEqual(posts[0]["url"], "https://www.googleapis.com/webmasters/v3/sites/"
                                          "sc-domain%3Aapexclean.example.com/searchAnalytics/query")
        self.assertEqual((posts[0]["json"]["startDate"], posts[0]["json"]["endDate"]), ("2026-09-04", "2026-10-01"))

    async def test_unshared_search_console_and_missing_website(self):
        async def forbidden(self_client, url, **kw):
            return _resp(403, {"error": "forbidden"})

        with patch.object(httpx.AsyncClient, "get", AsyncMock(return_value=_resp(500, {}, "GET"))), \
             patch.object(httpx.AsyncClient, "post", forbidden), \
             patch("src.skills.website_seo.google_auth_headers", AsyncMock(return_value={"Authorization": "Bearer t"})), \
             patch("src.skills.website_seo.service_account_email", return_value=ROBOT):
            report = await build_seo_report("https://apexclean.example.com", "sc-domain:apexclean.example.com")
        self.assertIn("• I couldn't run Google's website check right now. Please try again later.", report)
        self.assertIn(f"Google Search numbers unavailable: add {ROBOT} as a user in Google Search Console.", report)
        self.assertIn("No website is set for this business yet", await build_seo_report(None, None))

    async def test_seo_report_command_uses_no_ai(self):
        with tempfile.TemporaryDirectory() as data_dir:
            dispatcher = TenantWorkerDispatcher(data_root=data_dir)
            with patch.object(dispatcher.reply_skill, "send_reply", AsyncMock(return_value={"success": True})) as send, \
                 patch.object(httpx.AsyncClient, "get", AsyncMock(return_value=_resp(200, PAGESPEED_PAYLOAD, "GET"))), \
                 patch("litellm.acompletion", AsyncMock()) as mock_llm:
                res = await dispatcher.process_incoming_message(
                    tenant_id="tenant_seo_01", from_number="+971502222222", owner_phone="+971501234567",
                    body="seo report", message_sid="SM_seo", profile={"website_url": "https://apexclean.example.com"},
                )
        mock_llm.assert_not_called()
        self.assertEqual(res["command"], "seo_report")
        self.assertTrue(send.call_args.kwargs["message"].startswith("Website report for apexclean.example.com"))


if __name__ == "__main__":
    unittest.main()
