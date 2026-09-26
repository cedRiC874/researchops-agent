"""Bounded single-object batch protocol; fake processes are explicit fault injection."""
import hashlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from researchops_external_closure import git_objects as subject
from researchops_external_closure import git_objects_v2 as successor
from researchops_external_closure.errors import ExternalClosurePrimitiveError
from tests.test_external_closure_git_objects import GIT, Repository


class Input:
    def __init__(self):self.data=b'';self.closed=False;self.short=False
    def write(self,data):
        self.data+=data
        return len(data)-1 if self.short else len(data)
    def close(self):self.closed=True


class Output(io.BytesIO):
    def __init__(self,data):super().__init__(data);self.read_limits=[];self.line_limits=[]
    def readline(self,limit):self.line_limits.append(limit);return super().readline(limit)
    def read(self,limit):self.read_limits.append(limit);return super().read(limit)


class Process:
    def __init__(self,data,exit_code=0):
        self.stdin=Input();self.stdout=Output(data);self.returncode=None
        self.exit_code=exit_code;self.waits=[];self.kills=0;self.timeout_once=False
    def poll(self):return self.returncode
    def kill(self):self.kills+=1;self.returncode=-9
    def wait(self,timeout):
        self.waits.append(timeout)
        if self.timeout_once:
            self.timeout_once=False
            raise subprocess.TimeoutExpired('synthetic',timeout)
        if self.returncode is None:self.returncode=self.exit_code
        return self.returncode


class Timer:
    def __init__(self,interval,callback):
        self.interval=interval;self.callback=callback;self.daemon=False
        self.starts=0;self.cancels=0;self.joins=[]
    def start(self):self.starts+=1
    def cancel(self):self.cancels+=1
    def join(self,timeout):self.joins.append(timeout)
    def is_alive(self):return False


class SingleBatchTests(unittest.TestCase):
    def setUp(self):
        self.payload=b'raw\x00\xff\nbytes'
        self.oid=hashlib.sha1(b'blob '+str(len(self.payload)).encode()+b'\0'+self.payload).hexdigest()
        self.header=f'{self.oid} blob {len(self.payload)}\n'.encode()
        self.reader=object.__new__(subject._Reader)
        self.reader.executable='git.exe';self.reader.repository=Path('synthetic-unused')
        self.reader.environment={};self.reader.deadline=120.0
        self.reader.cache={};self.reader.total_bytes=0;self.reader.stopped=False
        self.now=0.0;self.timers=[]
    def invoke(self,process,*,hook=None):
        def create(*args,**kwargs):
            if hook:hook()
            return process
        def timer(interval,callback):
            value=Timer(interval,callback);self.timers.append(value);return value
        with patch.object(subject.time,'monotonic',side_effect=lambda:self.now), \
             patch.object(subject.subprocess,'Popen',side_effect=create) as popen, \
             patch.object(subject.threading,'Timer',side_effect=timer):
            try:return self.reader.read(self.oid,'blob')
            finally:self.last_launches=popen.call_count;self.last_call=popen.call_args
    def reject(self,data,code):
        process=Process(data)
        with self.assertRaises(ExternalClosurePrimitiveError) as caught:self.invoke(process)
        self.assertEqual(caught.exception.code,'external_closure_git_'+code)
        self.assertEqual(self.last_launches,1);self.assertTrue(self.reader.stopped)
        self.assertTrue(process.stdin.closed);self.assertTrue(process.stdout.closed)
        self.assertEqual(self.reader.cache,{})
        return process
    def test_valid_binary_payload_exact_stdin_single_process_and_cleanup(self):
        process=Process(self.header+self.payload+b'\n')
        self.assertEqual(self.invoke(process),self.payload)
        self.assertEqual(self.last_launches,1)
        self.assertEqual(self.last_call.args[0][-2:],['cat-file','--batch'])
        self.assertEqual(process.stdin.data,self.oid.encode()+b'\n')
        self.assertEqual(process.stdout.line_limits,[129]);self.assertEqual(process.stdout.read_limits,[len(self.payload)+2])
        self.assertTrue(process.stdin.closed and process.stdout.closed)
        self.assertEqual(process.kills,0);self.assertEqual(self.reader.total_bytes,len(self.payload))
        self.assertEqual(self.timers[0].interval,10.0)
        self.assertEqual((self.timers[0].starts,self.timers[0].cancels,len(self.timers[0].joins)),(1,1,1))
    def test_cache_does_not_spawn_again_or_double_charge(self):
        process=Process(self.header+self.payload+b'\n');self.invoke(process)
        with patch.object(subject.time,'monotonic',return_value=1),patch.object(subject.subprocess,'Popen',side_effect=AssertionError('no second process')):
            self.assertEqual(self.reader.read(self.oid,'blob'),self.payload)
        self.assertEqual(self.reader.total_bytes,len(self.payload))
    def test_missing_with_exit_zero_is_still_unavailable(self):
        process=self.reject(self.oid.encode()+b' missing\n','object_unavailable')
        self.assertEqual(process.stdout.read_limits,[])
    def test_wrong_oid_rejects_before_payload(self):
        process=self.reject(b'f'*40+b' blob 1\nx\n','object_hash_invalid')
        self.assertEqual(process.stdout.read_limits,[])
    def test_wrong_type_rejects_before_payload(self):
        process=self.reject(self.header.replace(b' blob ',b' tree ')+self.payload+b'\n','object_type_invalid')
        self.assertEqual(process.stdout.read_limits,[])
    def test_header_limit_is_bounded(self):
        process=self.reject(b'x'*1000+b'\n','batch_header_invalid')
        self.assertEqual(process.stdout.line_limits,[129]);self.assertEqual(process.stdout.read_limits,[])
    def test_malformed_header_variants_reject(self):
        for ending in (b'blob -1\n',b'blob 01\n',b'blob 1e6\n',b'blob 1\r\n',b'blob 999999999999999999999\n',b'weird 1\n',b'blob 1 extra\n'):
            with self.subTest(ending=ending):
                self.reader.stopped=False
                process=self.reject(self.oid.encode()+b' '+ending,'batch_header_invalid')
                self.assertEqual(process.stdout.read_limits,[])
    def test_size_limit_prevents_body_read(self):
        process=self.reject(f'{self.oid} blob {subject._MAX_OBJECT_BYTES+1}\n'.encode(),'object_size_limit')
        self.assertEqual(process.stdout.read_limits,[])
    def test_total_limit_prevents_body_read(self):
        self.reader.total_bytes=subject._MAX_TOTAL_BYTES
        process=self.reject(self.header,'total_size_limit')
        self.assertEqual(process.stdout.read_limits,[])
    def test_payload_short_read_rejects(self):self.reject(self.header+self.payload[:-1],'object_size_invalid')
    def test_bad_trailer_rejects(self):self.reject(self.header+self.payload+b'X','batch_framing_invalid')
    def test_trailing_output_rejects_with_bounded_read(self):
        process=self.reject(self.header+self.payload+b'\nexcess','output_limit')
        self.assertEqual(process.stdout.read_limits,[len(self.payload)+2])
    def test_content_hash_is_independently_recomputed(self):
        self.reject(self.header+b'X'*len(self.payload)+b'\n','object_hash_invalid')
    def test_process_failure_cannot_accept_valid_payload(self):
        with self.assertRaises(ExternalClosurePrimitiveError) as caught:self.invoke(Process(self.header+self.payload+b'\n',exit_code=1))
        self.assertEqual(caught.exception.code,'external_closure_git_object_unavailable')
        self.assertEqual(self.reader.cache,{})
    def test_snapshot_deadline_shortens_timer_without_reset(self):
        self.reader.deadline=0.25
        self.invoke(Process(self.header+self.payload+b'\n'))
        self.assertEqual(self.timers[0].interval,0.25);self.assertEqual(self.reader.deadline,0.25)
    def test_spawn_time_is_charged_before_sending(self):
        self.reader.deadline=0.25;process=Process(self.header+self.payload+b'\n')
        with self.assertRaises(ExternalClosurePrimitiveError) as caught:self.invoke(process,hook=lambda:setattr(self,'now',0.3))
        self.assertEqual(caught.exception.code,'external_closure_git_timeout')
        self.assertEqual(process.stdin.data,b'');self.assertEqual(self.timers,[])
        self.assertEqual(process.kills,1)
    def test_wait_timeout_is_not_retried_as_an_object_request(self):
        process=Process(self.header+self.payload+b'\n');process.timeout_once=True
        with self.assertRaises(ExternalClosurePrimitiveError) as caught:self.invoke(process)
        self.assertEqual(caught.exception.code,'external_closure_git_timeout')
        self.assertEqual(self.last_launches,1);self.assertEqual(process.kills,1)
        self.assertEqual(self.reader.cache,{})
    def test_timer_expiration_kills_owned_process_and_stops_reader(self):
        process=Process(self.header+self.payload+b'\n')
        def expire(timer):timer.starts+=1;timer.callback()
        with patch.object(Timer,'start',expire):
            with self.assertRaises(ExternalClosurePrimitiveError) as caught:self.invoke(process)
        self.assertEqual(caught.exception.code,'external_closure_git_timeout')
        self.assertEqual(process.kills,1);self.assertTrue(self.reader.stopped)
    def test_short_stdin_write_rejects(self):
        process=Process(self.header+self.payload+b'\n');process.stdin.short=True
        with self.assertRaises(ExternalClosurePrimitiveError) as caught:self.invoke(process)
        self.assertEqual(caught.exception.code,'external_closure_git_read_failed')
        self.assertEqual(process.stdout.line_limits,[])
    def test_cleanup_failure_prevents_next_launch(self):
        process=Process(self.header+self.payload+b'\n')
        original_close=process.stdout.close
        process.stdout.close=Mock(side_effect=OSError('synthetic close failure'))
        try:
            with self.assertRaises(ExternalClosurePrimitiveError) as caught:self.invoke(process)
            self.assertEqual(caught.exception.code,'external_closure_git_cleanup_failed')
            with patch.object(subject.subprocess,'Popen',side_effect=AssertionError('no next launch')):
                with self.assertRaisesRegex(ExternalClosurePrimitiveError,'reader_stopped'):self.reader.read(self.oid,'blob')
        finally:original_close()
    def test_failed_stdin_close_is_not_retried(self):
        process=Process(self.header+self.payload+b'\n')
        process.stdin.close=Mock(side_effect=OSError('synthetic uncertain close'))
        with self.assertRaises(ExternalClosurePrimitiveError) as caught:self.invoke(process)
        self.assertEqual(caught.exception.code,'external_closure_git_cleanup_failed')
        self.assertEqual(process.stdin.close.call_count,1)
        self.assertEqual(process.stdout.line_limits,[])
    def test_timer_not_reaped_cannot_accept_payload(self):
        with patch.object(Timer,'is_alive',return_value=True):
            with self.assertRaises(ExternalClosurePrimitiveError) as caught:self.invoke(Process(self.header+self.payload+b'\n'))
        self.assertEqual(caught.exception.code,'external_closure_git_cleanup_failed')
        self.assertEqual(self.reader.cache,{})
    def test_expired_cached_reader_cannot_bypass_snapshot_deadline(self):
        self.reader.cache[self.oid]=('blob',self.payload)
        self.now=121
        with self.assertRaises(ExternalClosurePrimitiveError) as caught:self.invoke(Process(b''))
        self.assertEqual(caught.exception.code,'external_closure_git_timeout');self.assertEqual(self.last_launches,0)
    def test_original_numeric_limits_are_unchanged(self):
        self.assertEqual((subject._COMMAND_SECONDS,subject._SNAPSHOT_SECONDS),(10.0,120.0))
        self.assertEqual((subject._MAX_OBJECT_BYTES,subject._MAX_TOTAL_BYTES,subject._MAX_OBJECTS,subject._MAX_PATHS),(8*1024*1024,32*1024*1024,1024,256))


@unittest.skipUnless(GIT,'local Git installation unavailable')
class SharedReaderIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.repo=Repository(Path(self.temp.name)/'repo')
    def test_legacy_and_successor_path_limits_and_single_cache_remain_distinct(self):
        blob=self.repo.object('blob',b'synthetic shared reader\n')
        names=tuple(f'file{i:03}' for i in range(320))
        tree=self.repo.tree(*(('100644',name,blob) for name in names));commit=self.repo.commit(tree)
        legacy=subject.read_git_object_snapshot(self.repo.root,commit,names[:256],expected_tree_oid=tree)
        self.assertEqual(len(legacy.blobs),256);self.assertEqual(legacy.objects_read,3)
        with self.assertRaisesRegex(ExternalClosurePrimitiveError,'paths_invalid'):
            subject.read_git_object_snapshot(self.repo.root,commit,names[:257])
        with patch.object(subject,'_Reader',wraps=subject._Reader) as readers:
            result=successor.read_git_object_snapshot(self.repo.root,commit,names,expected_tree_oid=tree)
        self.assertEqual(readers.call_count,1);self.assertEqual(len(result.blobs),320)
        self.assertEqual(result.objects_read,3)
        with self.assertRaisesRegex(ExternalClosurePrimitiveError,'paths_invalid'):
            successor.read_git_object_snapshot(self.repo.root,commit,names+('extra',))
    def test_successor_selected_total_still_counts_repeated_payloads(self):
        blob=self.repo.object('blob',b'x'*(8*1024*1024))
        names=tuple(f'large{i}' for i in range(5))
        tree=self.repo.tree(*(('100644',name,blob) for name in names));commit=self.repo.commit(tree)
        with self.assertRaisesRegex(ExternalClosurePrimitiveError,'selected_total_bytes_limit'):
            successor.read_git_object_snapshot(self.repo.root,commit,names,expected_tree_oid=tree)
