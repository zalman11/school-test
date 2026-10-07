import unittest

import app


class PassportSelectionTests(unittest.TestCase):
    def test_all_uses_registered_passport_count(self):
        self.assertEqual(app.resolve_passport_selection('all', 2), 2)
        self.assertEqual(app.resolve_passport_selection('all', 3), 3)

    def test_specific_count_is_limited_to_registered_passports(self):
        self.assertEqual(app.resolve_passport_selection('2', 2), 2)
        self.assertEqual(app.resolve_passport_selection('3', 2), None)
        self.assertEqual(app.resolve_passport_selection('3', 3), 3)

    def test_invalid_selection_is_rejected(self):
        self.assertIsNone(app.resolve_passport_selection('invalid', 3))
        self.assertIsNone(app.resolve_passport_selection('', 3))


if __name__ == '__main__':
    unittest.main()
