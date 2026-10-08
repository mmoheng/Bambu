import tempfile
import unittest
from pathlib import Path

from bambu_companion.bridge.api_auth import (
    get_or_create_api_key,
    load_api_key,
    matches,
    save_api_key,
)


class TestApiAuth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "bridge_config.json"

    def test_missing_file_returns_none(self):
        self.assertIsNone(load_api_key(self.path))

    def test_roundtrip_save_then_load(self):
        save_api_key("test-key-123", self.path)
        self.assertEqual(load_api_key(self.path), "test-key-123")

    def test_corrupt_file_returns_none_not_a_crash(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid json", encoding="utf-8")
        self.assertIsNone(load_api_key(self.path))

    def test_get_or_create_generates_once_and_persists(self):
        first = get_or_create_api_key(self.path)
        self.assertTrue(first)
        second = get_or_create_api_key(self.path)
        self.assertEqual(first, second)

    def test_get_or_create_keys_are_reasonably_unique_and_long(self):
        key = get_or_create_api_key(self.path)
        # secrets.token_urlsafe(32) -> 43 chars; just check it's not a
        # trivially short/guessable placeholder.
        self.assertGreaterEqual(len(key), 32)

    def test_matches_true_for_equal_strings(self):
        self.assertTrue(matches("abc123", "abc123"))

    def test_matches_false_for_different_strings(self):
        self.assertFalse(matches("abc123", "different"))

    def test_matches_false_for_empty_candidate(self):
        self.assertFalse(matches("", "abc123"))

    def test_matches_false_when_expected_is_empty(self):
        # Shouldn't be possible in practice (get_or_create_api_key never
        # returns ""), but an empty expected key should never match
        # anything, including another empty string, as a defense-in-depth
        # guard against a misconfigured server accepting all requests.
        self.assertFalse(matches("", ""))


if __name__ == "__main__":
    unittest.main()
