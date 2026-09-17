import unittest

from app.rate_limit import RateLimiter


class TestRateLimiter(unittest.TestCase):
    def test_allows_up_to_limit_then_blocks(self):
        rl = RateLimiter(limit_per_minute=3)
        results = [rl.allow("key-a") for _ in range(5)]
        self.assertEqual(results, [True, True, True, False, False])

    def test_keys_are_independent(self):
        rl = RateLimiter(limit_per_minute=1)
        self.assertTrue(rl.allow("key-a"))
        self.assertFalse(rl.allow("key-a"))
        self.assertTrue(rl.allow("key-b"))  # separate bucket


if __name__ == "__main__":
    unittest.main()
