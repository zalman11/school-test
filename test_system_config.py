import unittest

import app


class SystemConfigTests(unittest.TestCase):
    def test_set_system_config_updates_existing_unique_key(self):
        with app.app.app_context():
            app.db.session.query(app.SystemConfig).filter_by(
                key='start_grade_level'
            ).delete()
            app.db.session.add(
                app.SystemConfig(key='start_grade_level', value='8')
            )
            app.db.session.commit()

            app.set_system_config('start_grade_level', '1')
            app.db.session.commit()

            config = app.db.session.get(app.SystemConfig, 'start_grade_level')
            self.assertIsNotNone(config)
            self.assertEqual(config.value, '1')
            self.assertEqual(
                app.db.session.query(app.SystemConfig)
                .filter_by(key='start_grade_level')
                .count(),
                1,
            )

    def test_startup_defaults_preserve_existing_values(self):
        with app.app.app_context():
            app.db.session.query(app.SystemConfig).filter_by(
                key='start_grade_level'
            ).delete()
            app.db.session.add(
                app.SystemConfig(key='start_grade_level', value='1')
            )
            app.db.session.commit()

            app.set_system_config('start_grade_level', '8', overwrite=False)
            app.db.session.commit()

            config = app.db.session.get(app.SystemConfig, 'start_grade_level')
            self.assertEqual(config.value, '1')


if __name__ == '__main__':
    unittest.main()
