"""Exclusive-create failures must report the files they actually left behind."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_execution_current_v2 as v7
from tests import test_external_closure_v6_generator as v6
from tests.execution_v2_fixture import first_live_source_files
from tests.test_external_closure_execution_components import component_fixture_files


class SourceSnapshotFailureReceiptTests(unittest.TestCase):
    def exercise(self, generator, files, output_paths):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve(strict=True)
            for relative,payload in files.items():
                path=root/relative
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(payload)
            first=root/output_paths[0]
            second=root/output_paths[1]
            original_open=Path.open
            injection_opens=[]
            injection_writes=[]

            class PartialWriter:
                def __init__(self, stream):
                    self.stream=stream

                def __enter__(self):
                    return self

                def write(self, payload):
                    self.stream.write(payload[:11])
                    self.stream.flush()
                    injection_writes.append(11)
                    raise OSError("synthetic write failure; do not print exception text")

                def __exit__(self, *args):
                    self.stream.close()
                    return False

            def open_file(path, mode="r", *args, **kwargs):
                stream=original_open(path,mode,*args,**kwargs)
                if mode=="xb" and path==first:
                    injection_opens.append(path)
                    return PartialWriter(stream)
                return stream

            captured=io.StringIO()
            with patch.object(Path,"open",open_file),contextlib.redirect_stdout(captured):
                code=generator.main(["--project-root",str(root),"--write"])
            result=json.loads(captured.getvalue())
            self.assertEqual(injection_opens,[first],"failure injection must hit the intended exclusive open once")
            self.assertEqual(injection_writes,[11],"failure injection must execute the partial write once")
            self.assertEqual(code,2)
            self.assertEqual(result["status"],"failed")
            self.assertEqual(result["created_paths"],[output_paths[0]])
            self.assertEqual(len(first.read_bytes()),11)
            self.assertFalse(second.exists())
            self.assertNotIn("synthetic write failure",captured.getvalue())
            self.assertFalse(result["online_execution_authorized"])

    def test_v6_failure_receipt_names_the_partial_created_file(self):
        files,_=component_fixture_files()
        self.exercise(v6.generator,files,(v6.MANIFEST_PATH,v6.V6_PLAN_PATH))

    def test_v7_failure_receipt_names_the_partial_created_file(self):
        files,_=first_live_source_files()
        self.exercise(v7.generator,files,v7.PROFILE_PATHS["first_live"])


if __name__=="__main__":
    unittest.main()
