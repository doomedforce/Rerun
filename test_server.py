import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server


class ServerTests(unittest.TestCase):
    def test_optional_source_failure_does_not_block_verified_required_feed(self):
        sources = [
            {"name": "American Cinematheque", "status": "verified", "required": True},
            {"name": "Vista Theater Hollywood", "status": "verified", "required": True},
            {"id": "new-beverly", "name": "New Beverly Cinema", "status": "degraded"},
            {"name": "Vidiots", "status": "degraded", "required": False},
        ]
        meta = {"status": "ok", "verifiedRequiredSourceCount": 2}
        self.assertTrue(server.feed_is_complete(meta, sources))

    def test_required_source_failure_still_blocks_feed(self):
        sources = [
            {"name": "American Cinematheque", "status": "verified", "required": True},
            {"name": "Vista Theater Hollywood", "status": "degraded", "required": True},
            {"name": "New Beverly Cinema", "status": "degraded", "required": False},
        ]
        meta = {"status": "ok", "verifiedRequiredSourceCount": 1}
        self.assertFalse(server.feed_is_complete(meta, sources))


if __name__ == "__main__":
    unittest.main()
