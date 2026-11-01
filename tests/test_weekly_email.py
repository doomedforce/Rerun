import unittest
from datetime import date

import weekly_email


def show(id, title, start, venue="New Beverly Cinema", fmt="DCP", status="verified"):
    return {"id": id, "title": title, "startAt": start, "venue": venue, "format": fmt, "sourceStatus": status}


class WeeklyEmailTests(unittest.TestCase):
    def test_one_pick_a_day_prefers_film_prints(self):
        items = [show("a", "Alien", "2026-11-05T19:00:00-08:00"), show("b", "Heat", "2026-11-05T21:00:00-08:00", fmt="35mm")]
        self.assertEqual([s["id"] for _, s in weekly_email.picks(items, date(2026, 11, 5))], ["b"])

    def test_skips_repeats_orange_county_unverified_and_other_weeks(self):
        items = [
            show("a", "Heat (35mm)", "2026-11-05T19:00:00-08:00"),
            show("b", "Heat", "2026-11-06T19:00:00-08:00"),
            show("c", "Alien", "2026-11-06T20:00:00-08:00", venue="The Frida Cinema"),
            show("d", "Jaws", "2026-11-07T20:00:00-08:00", status="stale"),
            show("e", "Ran", "2026-11-12T20:00:00-08:00"),
        ]
        self.assertEqual([s["id"] for _, s in weekly_email.picks(items, date(2026, 11, 5))], ["a"])

    def test_issue_links_carry_the_email_source(self):
        subject, body = weekly_email.issue([show("a", "Heat", "2026-11-05T19:00:00-08:00", fmt="35mm")], date(2026, 11, 5))
        self.assertEqual(subject, "This week in LA repertory: Heat")
        self.assertIn("/?screening=a&src=email", body)
        self.assertIn("New Beverly Cinema, 7 PM, 35mm", body)

    def test_a_quiet_week_still_reads_cleanly(self):
        self.assertEqual(weekly_email.issue([], date(2026, 11, 5))[0], "This week in LA repertory")


if __name__ == "__main__":
    unittest.main()
