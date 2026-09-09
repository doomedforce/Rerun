import os
import unittest
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

import instagram_publisher


class InstagramPublisherTests(unittest.TestCase):
    def test_selection_prefers_one_future_official_screening_per_venue(self):
        items = [
            {"id": "a", "city": "los-angeles", "title": "A", "venue": "Venue One", "startAt": "2026-09-10T19:00:00-07:00", "sourceStatus": "verified"},
            {"id": "b", "city": "los-angeles", "title": "B", "venue": "Venue One", "startAt": "2026-09-10T20:00:00-07:00", "sourceStatus": "verified"},
            {"id": "c", "city": "los-angeles", "title": "C", "venue": "Venue Two", "startAt": "2026-09-10T21:00:00-07:00", "sourceStatus": "verified"},
            {"id": "d", "city": "los-angeles", "title": "D", "venue": "Fallback", "startAt": "2026-09-10T22:00:00-07:00", "sourceStatus": "partner-fallback"},
        ]
        with patch.object(instagram_publisher, "local_today", return_value="2026-09-10"), patch.dict(os.environ, {"RERUN_INSTAGRAM_SELECTION_LIMIT": "3"}, clear=False):
            selected = instagram_publisher.selected_screenings(items)
        self.assertEqual([item["id"] for item in selected], ["a", "c"])

    def test_publisher_stays_dormant_without_secrets(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(instagram_publisher.publish_today()["status"], "disabled")

    def test_meta_error_body_is_preserved(self):
        error = HTTPError(
            "https://graph.instagram.com/v23.0/me",
            400,
            "Bad Request",
            {},
            BytesIO(b'{"error":{"message":"Invalid OAuth access token.","code":190}}'),
        )
        self.assertEqual(
            instagram_publisher.format_http_error(error),
            "HTTP 400: Invalid OAuth access token. (code 190)",
        )

    def test_facebook_login_mode_requires_explicit_account_id(self):
        with patch.object(instagram_publisher, "API_MODE", "facebook-login"), patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "RERUN_INSTAGRAM_USER_ID is required"):
                instagram_publisher.instagram_user_id("token")


if __name__ == "__main__":
    unittest.main()
