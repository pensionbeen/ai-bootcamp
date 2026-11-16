"""Offline tests of real spawn workers; temporary files stay in staging."""
import multiprocessing
import os
import tempfile
import time
import unittest
from pathlib import Path
from request_deadline import RequestDeadlineExceeded, RequestWorkerError, run_with_deadline

ROOT = Path(__file__).resolve().parent
SECRET = 'private-api-token-must-not-appear-in-error'

def echo(payload):
    return {'pid': os.getpid(), 'payload': payload}

def delayed_write(started, late):
    Path(started).write_text(str(os.getpid()), encoding='utf-8')
    time.sleep(3)
    Path(late).write_text('worker was not terminated', encoding='utf-8')
    return {'finished': True}

def crash():
    os._exit(23)

def sensitive_error():
    raise ValueError(SECRET)

class RequestDeadlineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=ROOT, prefix='deadline-offline-')
        self.folder = Path(temporary.name).resolve()
        self.assertTrue(self.folder.is_relative_to(ROOT))
        self.addCleanup(temporary.cleanup)
        self.work = self.folder / 'worker-results'
        self.work.mkdir()
        self.other = self.work / 'another-request.json'
        self.other.write_text('{"another_request": true}', encoding='utf-8')
        self.other_bytes = self.other.read_bytes()
        self.previous_children = {p.pid for p in multiprocessing.active_children()}

    def assert_clean(self):
        self.assertEqual({p.pid for p in multiprocessing.active_children()} - self.previous_children, set())
        self.assertEqual(self.other.read_bytes(), self.other_bytes)
        self.assertEqual({p.name for p in self.work.iterdir()}, {self.other.name})

    def test_success_round_trip(self):
        payload = {'text': '원문 조건\n' * 20000, 'number': 2.67, 'flag': False}
        result = run_with_deadline(echo, {'payload': payload}, self.work, seconds=10)
        self.assertNotEqual(result['pid'], os.getpid())
        self.assertEqual(result['payload'], payload)
        self.assert_clean()

    def test_timeout_kills_child_and_prevents_late_write(self):
        started, late = self.folder / 'started.txt', self.folder / 'late.txt'
        begin = time.monotonic()
        with self.assertRaisesRegex(RequestDeadlineExceeded, '^request_deadline_exceeded$'):
            run_with_deadline(delayed_write, {'started': str(started), 'late': str(late)}, self.work, seconds=2)
        elapsed = time.monotonic() - begin
        self.assertTrue(started.is_file(), 'Worker did not start before the test deadline')
        self.assertNotEqual(int(started.read_text(encoding='utf-8')), os.getpid())
        self.assertGreaterEqual(elapsed, 1.9)
        self.assertLess(elapsed, 7.0)
        self.assertFalse(late.exists())
        self.assert_clean()
        time.sleep(3.2)
        self.assertFalse(late.exists(), 'Timed-out child continued after the parent returned')
        self.assert_clean()

    def test_abrupt_exit_is_error(self):
        with self.assertRaisesRegex(RequestWorkerError, '^request_worker_failed$'):
            run_with_deadline(crash, {}, self.work, seconds=10)
        self.assert_clean()

    def test_exception_text_is_scrubbed(self):
        with self.assertRaises(RequestWorkerError) as caught:
            run_with_deadline(sensitive_error, {}, self.work, seconds=10)
        self.assertEqual(str(caught.exception), 'request_worker_failed')
        self.assertNotIn(SECRET, str(caught.exception))
        self.assert_clean()

if __name__ == '__main__':
    unittest.main()
