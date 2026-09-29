import unittest

from egg_standards import classify_egg_size, servo_command


class EggStandardsTests(unittest.TestCase):
    def test_reference_weight_boundaries(self) -> None:
        cases = {
            44: "Small",
            45: "Medium",
            54: "Medium",
            55: "Large",
            62: "Large",
            63: "Extra Large",
            69: "Extra Large",
            70: "Jumbo",
        }
        for weight, expected in cases.items():
            with self.subTest(weight=weight):
                self.assertEqual(classify_egg_size(weight), expected)

    def test_size_commands_remain_available(self) -> None:
        self.assertEqual(servo_command("Small"), "SORT:SMALL")
        self.assertEqual(servo_command("Medium"), "SORT:MEDIUM")
        self.assertEqual(servo_command("Large"), "SORT:LARGE")
        self.assertEqual(servo_command("Extra Large"), "SORT:EXTRA_LARGE")


if __name__ == "__main__":
    unittest.main()
