import os
import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
