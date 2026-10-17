"""Observation-driven stages: no live account, network or model required."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from script.schema import parse, ScriptError, check_params
from script.library import Resolved, to_json
from script.runner import Runner
from script.helper import OpusHelper
from vision.elements import Element, Scene


def element(name, kind='button'):
    return Element(kind, name, 10, 10, 100, 30, 'uia', True)


class Watcher:
    hwnd = None
    def __init__(self):
        self.page = [element('Search routes')]
    def scene(self, **kw):
        return Scene(elements=self.page, frame=None, ms=0)
    text_scene = scene


class Helper:
    parse = OpusHelper.parse
    log = staticmethod(lambda *args: None)
    def __init__(self, replies):
        self.replies = iter(replies)
        self.inputs = []
    def rescue(self, **kw):
        self.inputs.append(kw)
        return next(self.replies)


def runner(steps, replies):
    script = parse({'name': 'test/stages', 'steps': steps})
    check_params(script)
    watcher = Watcher()
    clicks = []
    def click(el, **kw):
        clicks.append(el.name)
        watcher.page = ([element('Fethiye 22:00', 'text')] if el.name == 'Search routes'
                        else [element('Hotels found', 'text')])
        return True
    act = SimpleNamespace(click=click)
    helper = Helper(replies)
    run = Runner(Resolved(script, {script.name: script}, script.trust), watcher, act,
                 helper=helper, confirm=lambda *a: True)
    return run, helper, clicks


class ObservationTests(unittest.TestCase):
    def test_two_stages_share_observed_output_and_history(self):
        steps = [
            {'do': 'observe', 'goal': 'Find night route', 'expect': 'Fethiye 22:00'},
            {'do': 'capture', 'target': 'Fethiye 22:00', 'save_as': 'journey'},
            {'do': 'observe', 'goal': 'Find hotels for {journey}', 'expect': 'Hotels found'}]
        run, helper, clicks = runner(steps, [
            {'steps': [{'do': 'click', 'target': 'Search routes'}]},
            {'steps': [{'do': 'click', 'target': 'Fethiye 22:00'}]}])
        with patch('script.runner.SETTLE_S', 0):
            report = run.run()
        self.assertTrue(report.ok, report.summary())
        self.assertEqual(clicks, ['Search routes', 'Fethiye 22:00'])
        self.assertIn('Fethiye 22:00', helper.inputs[1]['goal'])
        self.assertIn('Earlier', str(helper.inputs[1]['history']))
        self.assertEqual(len(report.checkpoints), 2)
        self.assertEqual(report.observations['journey']['source'], 'uia')

    def test_false_done_never_completes_checkpoint(self):
        run, helper, clicks = runner([
            {'do': 'observe', 'goal': 'Find route', 'expect': 'Missing', 'rounds': 2}],
            [{'done': True}, {'done': True}])
        self.assertFalse(run.run().ok)
        self.assertEqual(clicks, [])
        self.assertIn('Completion rejected', str(helper.inputs[1]['history']))

    def test_failed_later_stage_does_not_replay_completed_stage(self):
        run, helper, clicks = runner([
            {'do': 'observe', 'goal': 'Find route', 'expect': 'Fethiye 22:00'},
            {'do': 'observe', 'goal': 'Find hotel', 'expect': 'Hotels found', 'rounds': 2}], [
            {'steps': [{'do': 'click', 'target': 'Search routes'}]},
            {'steps': [{'do': 'click', 'target': 'Nonexistent control'}]},
            {'steps': [{'do': 'click', 'target': 'Fethiye 22:00'}]}])
        with patch('script.runner.SETTLE_S', 0):
            report = run.run()
        self.assertTrue(report.ok, report.summary())
        self.assertEqual(clicks.count('Search routes'), 1)
        self.assertIn('FAILED', str(helper.inputs[2]['history']))

    def test_capture_cannot_replace_input_parameter(self):
        with self.assertRaises(ScriptError):
            check_params(parse({'name': 'test/overwrite', 'params': {'origin': 'Ankara'},
                'steps': [{'do': 'capture', 'target': 'Other origin', 'save_as': 'origin'}]}))

    def test_stop_prevents_model_and_actions(self):
        run, helper, clicks = runner([
            {'do': 'observe', 'goal': 'Find route', 'expect': 'Fethiye 22:00'}], [])
        run.should_stop = lambda: True
        self.assertTrue(run.run().stopped)
        self.assertEqual(helper.inputs, [])
        self.assertEqual(clicks, [])

    def test_whole_batch_is_validated_before_first_click(self):
        helper = Helper([])
        self.assertEqual(helper.parse({'steps': [
            {'do': 'click', 'target': 'Search routes'}, {'do': 'invalid'}]}, 'test'), [])

    def test_no_recursive_observation(self):
        self.assertEqual(Helper([]).parse({'steps': [
            {'do': 'assist', 'goal': 'loop'}]}, 'test'), [])

    def test_unknown_output_rejected_before_execution(self):
        with self.assertRaises(ScriptError):
            check_params(parse({'name': 'test/unknown', 'steps': [
                {'do': 'type', 'value': '{missing}'}]}))

    def test_capture_round_trip(self):
        script = parse({'name': 'test/capture', 'steps': [
            {'do': 'capture', 'target': 'Observed text', 'save_as': 'result'},
            {'do': 'type', 'value': '{result}'}]})
        check_params(script)
        self.assertEqual(to_json(script)['steps'][0]['save_as'], 'result')

    def test_observe_requires_completion_check(self):
        with self.assertRaises(ScriptError):
            parse({'name': 'test/invalid', 'steps': [{'do': 'observe', 'goal': 'Do stuff'}]})


if __name__ == '__main__':
    unittest.main()
