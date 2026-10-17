import json
import sys
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
from unittest.mock import Mock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import codex_backend as backend


class CodexTests(unittest.TestCase):
    def tearDown(self):
        backend.configure({})

    def test_opt_in(self):
        backend.configure({})
        self.assertFalse(backend.enabled())
        backend.configure({'planning_engine': 'codex'})
        self.assertTrue(backend.enabled())

    @patch.object(backend, 'executable', return_value='codex.cmd')
    @patch.object(backend.subprocess, 'run')
    def test_login_status_no_account_details(self, run, exe):
        run.return_value = subprocess.CompletedProcess([], 0, '', 'Logged in using ChatGPT')
        self.assertEqual(backend.login_status('codex')['detail'], 'ChatGPT login')
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps({
            'loggedIn': True, 'authMethod': 'claude.ai', 'email': 'private@example.org'}), '')
        result = backend.login_status('claude')
        self.assertTrue(result['logged_in'])
        self.assertNotIn('private', str(result))

    @patch.object(backend, 'executable', return_value='codex.cmd')
    @patch.object(backend, '_run')
    def test_call_images_and_output(self, run, exe):
        def fake(args, **kw):
            self.assertIn('--image', args)
            self.assertIn('read-only', args)
            self.assertEqual(args[-1], '-')
            self.assertIn('secret prompt', kw['input'])
            self.assertNotIn('secret prompt', str(args))
            Path(args[args.index('--output-last-message')+1]).write_text('{"steps": []}')
            return subprocess.CompletedProcess(args, 0, '', '')
        run.side_effect = fake
        self.assertEqual(backend.call('secret prompt', images=['shot.png'], record=False), '{"steps": []}')

    @patch.object(backend, 'executable', return_value='codex.cmd')
    @patch.object(backend, '_run')
    def test_failure_does_not_fallback(self, run, exe):
        run.return_value = subprocess.CompletedProcess([], 1, '', 'credential details')
        with self.assertRaisesRegex(RuntimeError, 'Codex call failed'):
            backend.call('test', record=False)

    def test_author_routed_to_codex(self):
        from core.authoring import _claude
        backend.configure({'planning_engine': 'codex'})
        with patch.object(backend, 'call', return_value='{}') as call:
            self.assertEqual(_claude('test', 'opus', 30, None), '{}')
            self.assertEqual(call.call_args.kwargs['timeout'], 30)

    def test_settings_validated(self):
        from app import _clean_value
        self.assertEqual(_clean_value('planning_engine', 'codex'), (True, 'codex'))
        self.assertFalse(_clean_value('planning_engine', 'bad')[0])
        self.assertEqual(_clean_value('codex_model', ''), (True, ''))

    def test_quick_uses_selected_engine(self):
        from core.quick import complete
        with patch.object(backend, 'call', return_value='{"text":"hello"}') as call:
            text, how = complete('task', {'planning_engine': 'codex'})
            self.assertIn('hello', text)
            self.assertTrue(how.startswith('codex'))
            call.assert_called_once()

    def test_stop_terminates_call(self):
        proc = Mock()
        proc.poll.return_value = None
        sdk = SimpleNamespace(should_stop=lambda: True)
        with patch.dict('sys.modules', {'tools_sdk': sdk}), \
             patch.object(backend.subprocess, 'Popen', return_value=proc), \
             patch.object(backend.subprocess, 'run') as kill:
            with self.assertRaisesRegex(RuntimeError, 'stopped by user'):
                backend._run(['codex'], input='test', timeout=10, capture_output=True)
            if backend.os.name == 'nt':
                kill.assert_called_once()

    def test_takeover_uses_guarded_executor(self):
        from app import Agent
        from core import authoring, pipeline_bridge
        engine = Mock()
        script = object()
        lib = Mock()
        lib.local.return_value = {'demo/task': script}
        engine.fill_params.return_value = {'text': 'hi'}
        report = SimpleNamespace(ok=True, stopped=False, summary=lambda: 'done')
        engine.execute.return_value = SimpleNamespace(report=report)
        app = SimpleNamespace(settings={'planning_engine': 'codex'}, _engine=engine,
                              _say=Mock(), _last_result={'error': True})
        with patch('app.tools_sdk.should_stop', return_value=False), \
             patch.object(pipeline_bridge, 'library', return_value=lib), \
             patch.object(authoring, 'author', return_value=('demo/task', 'written')):
            Agent._drive_codex(app, 'type hi')
        engine.fill_params.assert_called_once_with(script, 'type hi', ask_missing=True)
        engine.execute.assert_called_once_with('demo/task', {'text': 'hi'}, 'type hi')
        self.assertFalse(app._last_result['error'])

    def test_missing_parameter_asks_user(self):
        from core.engine import ScriptEngine
        from core.channel import channel, Reply, Answer
        engine = ScriptEngine({}, Mock())
        script = SimpleNamespace(name='demo/form', description='Open form',
                                 params={'origin': 'Ankara', 'passenger_gender': ''})
        with patch('core.quick.complete', return_value=(
                '{"origin":"Ankara","passenger_gender":null}', 'test')), \
             patch.object(channel, 'ask', return_value=Reply(Answer.TEXT, text='test-value')) as ask, \
             patch('tools_sdk.should_stop', return_value=False):
            result = engine.fill_params(script, 'open form', ask_missing=True)
        self.assertEqual(result, {'origin': 'Ankara', 'passenger_gender': 'test-value'})
        self.assertFalse(engine.awaiting_parameters)
        self.assertFalse(ask.call_args.kwargs['approve_deny'])

    def test_missing_parameter_timeout_does_not_invent_value(self):
        from core.engine import ScriptEngine
        from core.channel import channel, Reply, Answer
        engine = ScriptEngine({}, Mock())
        script = SimpleNamespace(name='demo/form', description='Open form',
                                 params={'passenger_gender': ''})
        with patch('core.quick.complete', return_value=('{"passenger_gender":null}', 'test')), \
             patch.object(channel, 'ask', return_value=Reply(Answer.TIMEOUT)), \
             patch('tools_sdk.should_stop', return_value=False):
            self.assertIsNone(engine.fill_params(script, 'open form', ask_missing=True))
        self.assertTrue(engine.awaiting_parameters)

    def test_typed_text_verification_uses_editor_value(self):
        from core import pipeline_bridge
        pipeline_bridge.ensure_importable()
        from script.runner import Runner
        from script.schema import parse
        text = 'Codex GUI test - merhaba dünya, çığ şüphe.'
        step = parse({'name': 'test/type', 'steps': [
            {'do': 'type', 'value': text, 'expect': text}]}).steps[0]
        runner = SimpleNamespace(act=SimpleNamespace(focused_text=lambda: text),
                                 _sel=lambda sel, values, where: sel, _await=Mock())
        with patch('script.runner.time.sleep'):
            Runner._verify(runner, step, {}, 'test/type.steps[0]')
        runner._await.assert_not_called()
        runner.act.focused_text = lambda: 'wrong text'
        runner._await.return_value = None
        with patch('script.runner.time.sleep'), self.assertRaises(Exception):
            Runner._verify(runner, step, {}, 'test/type.steps[0]')
        runner._await.assert_called_once()


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(CodexTests)
    result = unittest.TextTestRunner().run(suite)
    failed = len(result.failures) + len(result.errors)
    print(f"{result.testsRun-failed}/{result.testsRun} passed")
    sys.exit(not result.wasSuccessful())
