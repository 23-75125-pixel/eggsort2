import unittest

from validation import (
    ValidationError,
    normalize_avatar_url,
    parse_staff_profile,
)


class ValidationTests(unittest.TestCase):
    def test_staff_profile_is_normalized(self) -> None:
        profile = parse_staff_profile(
            {
                "email": " Staff@Example.com ",
                "username": " Operator.One ",
                "display_name": " Operator One ",
                "avatar_url": "https://example.com/avatar.png",
            }
        )
        self.assertEqual(profile.email, "staff@example.com")
        self.assertEqual(profile.username, "operator.one")
        self.assertEqual(profile.display_name, "Operator One")

    def test_invalid_profile_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            parse_staff_profile(
                {"email": "invalid", "username": "x", "display_name": "A"}
            )

    def test_avatar_requires_https(self) -> None:
        self.assertIsNone(normalize_avatar_url("http://example.com/avatar.png"))
        self.assertEqual(
            normalize_avatar_url("https://example.com/avatar.png"),
            "https://example.com/avatar.png",
        )

if __name__ == "__main__":
    unittest.main()
