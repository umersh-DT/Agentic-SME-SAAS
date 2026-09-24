import unittest
from fastapi.testclient import TestClient
from src.gateway.main import app


class TestGatewayMetrics(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_metrics_endpoint_scrapes_successfully(self):
        # Trigger an endpoint to generate sample metric counters
        res = self.client.get("/health")
        self.assertEqual(res.status_code, 200)

        # Scrape /metrics
        metrics_resp = self.client.get("/metrics")
        self.assertEqual(metrics_resp.status_code, 200)
        self.assertIn("text/plain", metrics_resp.headers["content-type"])
        body = metrics_resp.text

        self.assertIn("http_requests_total", body)
        self.assertIn('endpoint="/health"', body)
        self.assertIn("http_request_duration_seconds", body)


if __name__ == "__main__":
    unittest.main()