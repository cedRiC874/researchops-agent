"""Native temporary Windows handles, not simulated filesystem protection."""
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from researchops_completion_timing.first_live_workspace import _owned_work_paths, _new_database


@unittest.skipUnless(os.name == 'nt', 'designated Windows execution environment')
class FirstLiveWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_sqlite_writes_work_but_file_and_directory_rename_are_denied(self):
        with _owned_work_paths(self.root, 'a' * 64) as (database, archive, envelope):
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute('CREATE TABLE example(value INTEGER)')
                connection.execute('INSERT INTO example VALUES (1)')
            with self.assertRaises(OSError): database.rename(database.with_suffix('.moved'))
            with self.assertRaises(OSError): database.parent.rename(database.parent.with_suffix('.moved'))
            replacement = database.parent / 'replacement'; replacement.write_bytes(b'never installed')
            with self.assertRaises(OSError): os.replace(replacement, database)
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute('SELECT value FROM example').fetchall(), [(1,)])

    def test_existing_database_is_never_opened_for_write(self):
        database = self.root / 'audit.sqlite3'; database.write_bytes(b'preserve existing')
        with self.assertRaisesRegex(ValueError, 'database_create_failed'):
            with _new_database(database): self.fail('existing database accepted')
        self.assertEqual(database.read_bytes(), b'preserve existing')

    def test_failure_retains_database_and_releases_handles_without_resume(self):
        with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
            with _owned_work_paths(self.root, 'b' * 64) as (database, _, __):
                raise RuntimeError('synthetic failure')
        self.assertEqual(database.read_bytes(), b'')
        renamed = database.with_suffix('.retained'); database.rename(renamed)
        self.assertTrue(renamed.is_file())
        with self.assertRaises(FileExistsError):
            with _owned_work_paths(self.root, 'b' * 64): self.fail('resume permitted')

    def test_invalid_identifier_cannot_escape_output(self):
        for identifier in ('../escape', 'a' * 63, '0' * 64, None):
            with self.subTest(identifier=identifier), self.assertRaisesRegex(ValueError, 'identifier_invalid'):
                with _owned_work_paths(self.root, identifier): self.fail('invalid identifier accepted')
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == '__main__': unittest.main()
