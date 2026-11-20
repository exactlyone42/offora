import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

GATE = Path(__file__).resolve().parents[1] / 'hooks' / 'office_gate.py'


class Ready(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ready"}')

    def log_message(self, *args):
        pass


class GateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(('127.0.0.1', 0), Ready)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = 'http://127.0.0.1:%d' % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.sid = 's1'

    def tearDown(self):
        self.dir.cleanup()

    def run_gate(self, event, url=None, **payload):
        payload.setdefault('session_id', self.sid)
        payload.setdefault('cwd', self.dir.name)
        env = dict(os.environ, OFFORA_URL=url or self.url, OFFORA_GATE_DIR=self.dir.name + '/gate', PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run([sys.executable, '-B', str(GATE), event], input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def start(self):
        return self.run_gate('SessionStart', source='startup')

    def done(self, *names):
        for name in names:
            self.run_gate('PostToolUse', tool_name='mcp__offora__' + name)

    def denied(self, tool='Bash'):
        out = self.run_gate('PreToolUse', tool_name=tool)
        return out.get('hookSpecificOutput', {}).get('permissionDecision') == 'deny'

    def test_start_injects_rules_and_registration_demand(self):
        text = self.start()['hookSpecificOutput']['additionalContext']
        self.assertIn('office_describe', text)
        self.assertIn('office_update_summary', text)

    def test_other_tools_denied_until_registered(self):
        self.start()
        self.assertTrue(self.denied('Bash'))
        self.assertFalse(self.denied('mcp__offora__office_describe'))
        self.assertFalse(self.denied('ToolSearch'))
        self.done('office_describe')
        self.assertTrue(self.denied('Bash'))
        self.done('office_update_summary')
        self.assertFalse(self.denied('Bash'))

    def test_repo_registration_required_inside_git_repo(self):
        subprocess.run(['git', 'init', '-q', self.dir.name], check=True)
        subprocess.run(['git', '-C', self.dir.name, 'remote', 'add', 'origin', 'git@example.com:a/b.git'], check=True)
        text = self.start()['hookSpecificOutput']['additionalContext']
        self.assertIn('git@example.com:a/b.git', text)
        self.done('office_describe', 'office_update_summary')
        self.assertTrue(self.denied('Bash'))
        self.done('office_set_repo')
        self.assertFalse(self.denied('Bash'))

    def test_stop_blocks_without_report_then_allows(self):
        self.start()
        self.done('office_describe', 'office_update_summary')
        out = self.run_gate('Stop')
        self.assertEqual(out['decision'], 'block')
        self.assertIn('office_read_since', out['reason'])
        self.done('office_read_since')
        self.assertEqual(self.run_gate('Stop'), {})

    def test_new_turn_requires_new_report(self):
        self.start()
        self.done('office_describe', 'office_update_summary', 'office_read_messages')
        self.assertEqual(self.run_gate('Stop'), {})
        self.run_gate('UserPromptSubmit', prompt='next')
        self.assertEqual(self.run_gate('Stop')['decision'], 'block')
        self.assertFalse(self.denied('Bash'))

    def test_stop_hook_active_does_not_loop(self):
        self.start()
        self.assertEqual(self.run_gate('Stop', stop_hook_active=True), {})

    def test_server_down_fails_open(self):
        down = 'http://127.0.0.1:9'
        self.assertEqual(self.run_gate('SessionStart', url=down), {})
        self.assertEqual(self.run_gate('PreToolUse', url=down, tool_name='Bash'), {})
        self.assertEqual(self.run_gate('Stop', url=down), {})

    def test_repeated_denials_release_the_gate(self):
        self.start()
        results = [self.denied('Bash') for _ in range(8)]
        self.assertEqual(results[:6], [True] * 6)
        self.assertEqual(results[6:], [False, False])
        self.assertEqual(self.run_gate('Stop'), {})

    def test_sessions_are_isolated(self):
        self.start()
        self.done('office_describe', 'office_update_summary')
        self.assertFalse(self.denied('Bash'))
        self.run_gate('SessionStart', session_id='other', source='startup')
        out = self.run_gate('PreToolUse', session_id='other', tool_name='Bash')
        self.assertEqual(out['hookSpecificOutput']['permissionDecision'], 'deny')


if __name__ == '__main__':
    unittest.main()
