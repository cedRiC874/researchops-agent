"""Native Windows file identity tests; no Provider, claim or Key use."""
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from researchops_completion_timing.campaign_workspace import _owned_campaign_work_paths


@unittest.skipUnless(os.name=='nt','designated Windows execution environment')
class CampaignWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.identifier='a'*64

    def open(self): return _owned_campaign_work_paths(self.root,self.identifier)

    def test_fixed_names_and_real_sqlite_with_held_file_and_ancestors(self):
        with self.open() as (database,archive,summary):
            self.assertEqual(database.relative_to(self.root).as_posix(),f'output/campaign-v1/{self.identifier}.work/phase6_audit.sqlite3')
            self.assertEqual(archive.name,self.identifier)
            self.assertEqual(summary.name,self.identifier+'.invocation.json')
            with closing(sqlite3.connect(database)) as connection,connection:
                connection.execute('CREATE TABLE example(value INTEGER)')
                connection.execute('INSERT INTO example VALUES (1)')
            with self.assertRaises(OSError): database.rename(database.with_suffix('.moved'))
            with self.assertRaises(OSError): database.parent.rename(database.parent.with_suffix('.moved'))
            replacement=database.parent/'replacement'; replacement.write_bytes(b'preserve')
            with self.assertRaises(OSError): os.replace(replacement,database)
            self.assertFalse(archive.exists()); self.assertFalse(summary.exists())
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute('SELECT value FROM example').fetchall(),[(1,)])

    def test_exception_retains_work_database_and_does_not_allow_resume(self):
        with self.assertRaisesRegex(RuntimeError,'fixture failure'):
            with self.open() as (database,_,__): raise RuntimeError('fixture failure')
        self.assertEqual(database.read_bytes(),b'')
        moved=database.with_suffix('.retained'); database.rename(moved)
        self.assertTrue(moved.exists())
        with self.assertRaises(FileExistsError):
            with self.open(): self.fail('existing work reused')

    def test_existing_archive_is_preserved_and_blocks_work_creation(self):
        parent=self.root/'output'/'campaign-v1'; parent.mkdir(parents=True)
        archive=parent/self.identifier; archive.mkdir(); (archive/'keep').write_bytes(b'keep')
        with self.assertRaises(FileExistsError):
            with self.open(): self.fail('existing archive accepted')
        self.assertEqual((archive/'keep').read_bytes(),b'keep')
        self.assertFalse((parent/(self.identifier+'.work')).exists())

    def test_existing_summary_is_preserved_and_blocks_work_creation(self):
        parent=self.root/'output'/'campaign-v1'; parent.mkdir(parents=True)
        summary=parent/(self.identifier+'.invocation.json'); summary.write_bytes(b'keep')
        with self.assertRaises(FileExistsError):
            with self.open(): self.fail('existing summary accepted')
        self.assertEqual(summary.read_bytes(),b'keep')
        self.assertFalse((parent/(self.identifier+'.work')).exists())

    def test_invalid_identifiers_and_relative_root_create_nothing(self):
        for value in ('../escape','a'*63,'0'*64,'a'*64+'\n',True,None):
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'identifier_invalid'):
                with _owned_campaign_work_paths(self.root,value): self.fail('invalid identifier accepted')
        with self.assertRaisesRegex(ValueError,'root_invalid'):
            with _owned_campaign_work_paths(Path('relative'),self.identifier): self.fail('relative root accepted')
        self.assertEqual(list(self.root.iterdir()),[])

    def test_added_hardlink_is_rejected_at_final_identity_check(self):
        with self.assertRaisesRegex(ValueError,'database_changed'):
            with self.open() as (database,_,__):
                os.link(database,database.parent/'extra-link')
        self.assertTrue(database.exists()); self.assertEqual(database.stat().st_nlink,2)


@unittest.skipUnless(os.name=='nt','designated Windows execution environment')
class HeldCampaignPublicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_runner_and_publication_keep_the_work_database_held(self):
        from researchops.audit import AuditLedger
        from researchops_completion_timing import campaign_publish as publish
        from tests import test_completion_campaign_artifacts as fixtures
        from tests import test_completion_campaign_runner_bridge as bridge
        temporary=tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root=Path(temporary.name)
        helper=fixtures.CampaignArtifactProducerTests()
        with _owned_campaign_work_paths(root,'b'*64) as (database,archive,summary):
            # Redirect only the fixture's ledger constructor; production's exact
            # AuditLedger type checks and SQLite implementation are unchanged.
            with patch.object(bridge,'AuditLedger',side_effect=lambda _:AuditLedger(database)):
                await helper.asyncSetUp()
            self.addCleanup(helper.helper._cleanup_fixture)
            self.assertEqual(helper.factory._ledger.database_path,database)
            with patch.object(publish,'_check_publication_identity'):
                result=publish._publish_completed_campaign(helper.factory,archive)
            self.assertEqual(result['status'],'campaign_archive_written_and_verified')
            self.assertEqual(len(list(archive.iterdir())),7)
            self.assertFalse(summary.exists())  # Public summary writer not yet wired.
            with self.assertRaises(OSError): database.rename(database.with_suffix('.moved'))
            with self.assertRaises(OSError): database.parent.rename(database.parent.with_suffix('.moved'))
        self.assertTrue(database.exists())
        self.assertTrue((archive/'phase6_audit.sqlite3').exists())


if __name__=='__main__': unittest.main()
