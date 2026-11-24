"""Secret and PII classification (P5-03)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from memoryhub.safety import classify, findings, looks_sensitive  # noqa: E402


class SecretTests(unittest.TestCase):
    def test_credentials_are_secret_and_win_over_everything(self) -> None:
        level, labels = classify("address 示例区示例路 390 号 and key sk-abcdefghijklmnopqrstuvwx123456")
        self.assertEqual(level, "secret")
        self.assertIn("openai-style key", labels)

    def test_clean_text_is_normal(self) -> None:
        self.assertEqual(classify("prefers pnpm over npm"), ("normal", []))

    def test_helpers_agree(self) -> None:
        self.assertTrue(looks_sensitive("ghp_" + "a" * 30))
        self.assertEqual(findings("nothing here"), [])


class PiiTests(unittest.TestCase):
    def test_national_id_and_phone_are_sensitive(self) -> None:
        self.assertEqual(classify("110101199003070000")[0], "sensitive")
        self.assertEqual(classify("call 13900000000")[0], "sensitive")

    def test_full_address_is_sensitive_in_both_shapes(self) -> None:
        for text in (
            "住示例区示例路 390 号",
            "北京市示例区示例路 390 号",
        ):
            self.assertEqual(classify(text)[0], "sensitive", text)

    def test_city_or_school_alone_is_only_personal(self) -> None:
        # Weak signals must not block: a rule that fires on every city name gets
        # ignored, and then the strong signal is ignored too.
        self.assertEqual(classify("示例省示例市示例区")[0], "personal")
        self.assertEqual(classify("示例大学")[0], "personal")
        self.assertEqual(classify("在上海读书四年")[0], "normal")


if __name__ == "__main__":
    unittest.main()
