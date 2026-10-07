import unittest

import app


class UpdateCheckerTests(unittest.TestCase):
    def test_newer_release_is_detected(self):
        self.assertTrue(app.is_newer_release('v1.0.3', 'v1.0.4'))
        self.assertFalse(app.is_newer_release('v1.0.4', 'v1.0.3'))
        self.assertFalse(app.is_newer_release('v1.0.4', 'v1.0.4'))

    def test_release_tag_uses_numeric_version(self):
        self.assertTrue(app.is_newer_release('1.0.3', 'v1.0.4'))


if __name__ == '__main__':
    unittest.main()
