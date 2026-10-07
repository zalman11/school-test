import unittest

import app


class UpdaterTests(unittest.TestCase):
    def test_parse_app_args_detects_updated_flag(self):
        args = app.parse_app_args(["--updated"])
        self.assertTrue(args.updated)

    def test_parse_app_args_defaults_to_normal_start(self):
        args = app.parse_app_args([])
        self.assertFalse(args.updated)

    def test_windows_update_command_replaces_running_executable(self):
        source = r"C:\Users\Administrator\AppData\Local\Temp\school-test.exe"
        target = r"C:\Program Files\School Test\school-test.exe"

        command = app.build_windows_update_command(source, target)

        self.assertIn("Start-Sleep -Seconds 2", command)
        self.assertIn("Remove-Item -LiteralPath $target -Force", command)
        self.assertIn("Move-Item -LiteralPath $source -Destination $target -Force", command)
        self.assertIn("Start-Process -FilePath $target -ArgumentList '--updated'", command)

    def test_windows_update_command_quotes_paths_with_single_quotes(self):
        source = r"C:\Temp\school's-test.exe"
        target = r"C:\Program Files\school's-test.exe"

        command = app.build_windows_update_command(source, target)

        self.assertIn("C:\\Temp\\school''s-test.exe", command)
        self.assertIn("C:\\Program Files\\school''s-test.exe", command)


if __name__ == "__main__":
    unittest.main()
