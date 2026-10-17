"""Opt-in real Codex + headed Chrome test on two isolated local web origins.

Uses a test DOM adapter for the production runner; it does not test Windows UIA.
Run from agent/: ../.venv/Scripts/python.exe ../pipeline/tests/live_observe.py
"""
import json
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'agent'), str(ROOT / 'pipeline')]
from core import codex_backend
from playwright.sync_api import sync_playwright
from script.schema import parse, check_params
from script.library import Resolved
from script.runner import Runner
from script.helper import OpusHelper
from vision.elements import Element, Scene

OUT = ROOT / 'agent/workspace/observe-test'
OUT.mkdir(parents=True, exist_ok=True)
PAGES = [
    '<title>Observation A</title><h1>Reference desk</h1>'
    '<button id="reveal" onclick="document.getElementById(\'reference\').hidden=false">Show reference</button>'
    '<p id="reference" hidden>FETHIYE-29-2200</p>',
    '<title>Observation B</title><h1>Reference checker</h1>'
    '<label for="code">Reference</label><input id="code" aria-label="Reference">'
    '<button id="check" onclick="document.getElementById(\'result\').textContent='
    'document.getElementById(\'code\').value===\'FETHIYE-29-2200\'?\'Reference verified\':\'Wrong reference\'">Check reference</button>'
    '<p id="result"></p>']


def serve(html):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(('<style>body{font:24px Arial;padding:50px}button,input{font-size:24px;margin:15px}</style>' + html).encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Watcher:
    hwnd = None
    def __init__(self, page):
        self.page = page
    def scene(self, **kw):
        rows = self.page.locator('button,input,p,h1').evaluate_all('''els => els
          .filter(e => e.getClientRects().length && !e.hidden)
          .map(e => {let r=e.getBoundingClientRect();return {id:e.id,
            name:e.getAttribute('aria-label')||e.innerText||'',
            kind:e.tagName==='INPUT'?'edit':e.tagName==='BUTTON'?'button':'text',
            x:r.x,y:r.y,w:r.width,h:r.height}})''')
        return Scene(elements=[Element(r['kind'], r['name'], int(r['x']), int(r['y']),
            int(r['w']), int(r['h']), 'dom', r['kind']!='text', automation_id=r['id'])
            for r in rows], frame=None, ms=0)
    text_scene = scene


class Actuator:
    def __init__(self, page): self.page = page
    def launch(self, app, url): self.page.goto(url); return 'opened test origin'
    def foreground_window(self): return 0
    def click(self, el, **kw): self.page.locator('#' + el.automation_id).click(); return True
    def type_text(self, text, el): self.page.locator('#' + el.automation_id).fill(text); return True
    def press_key(self, key): self.page.keyboard.press(key.title()); return True


def main():
    servers = [serve(p) for p in PAGES]
    started = time.monotonic()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', headless=False)
            page = browser.new_page(viewport={'width': 1280, 'height': 800})
            helper = OpusHelper(budget=8, stuck_dir=OUT, log=lambda s: print(s, flush=True))
            helper.claude = None
            def shot():
                path = OUT / f'round-{helper.calls}.png'
                page.screenshot(path=str(path))
                return path
            helper._screenshot = shot
            codex_backend.configure({'planning_engine': 'codex'})
            def call(prompt, screenshot):
                text = codex_backend.call(prompt, images=[screenshot], timeout=100, record=False)
                with (OUT/'model.jsonl').open('a', encoding='utf8') as f:
                    f.write(json.dumps({'prompt': prompt, 'reply': text, 'screenshot': str(screenshot)}, ensure_ascii=False)+'\n')
                return text, None
            helper.caller = call
            script = parse({'name': 'test/two-origins', 'steps': [
                {'do': 'open', 'url': f'http://127.0.0.1:{servers[0].server_port}'},
                {'do': 'observe', 'goal': 'Reveal the reference using the visible control. Stop when reference text is visible.',
                 'expect': {'id': 'reference'}, 'rounds': 4},
                {'do': 'capture', 'target': {'id': 'reference'}, 'save_as': 'reference'},
                {'do': 'open', 'url': f'http://127.0.0.1:{servers[1].server_port}'},
                {'do': 'observe', 'goal': 'Enter the observed reference {reference}, then check it. Stop when Reference verified appears.',
                 'expect': 'Reference verified', 'rounds': 4}]})
            check_params(script)
            run = Runner(Resolved(script, {script.name: script}, script.trust),
                         Watcher(page), Actuator(page), helper=helper, confirm=lambda *a: True)
            report = run.run()
            page.screenshot(path=str(OUT/'final.png'))
            result = {'ok': report.ok, 'seconds': round(time.monotonic()-started, 2),
                      'model_calls': helper.calls, 'summary': report.summary(),
                      'observations': report.observations, 'final_text': page.locator('body').inner_text()}
            (OUT/'result.json').write_text(json.dumps(result, indent=2), encoding='utf8')
            print(json.dumps(result, indent=2), flush=True)
            browser.close()
            return 0 if report.ok and 'Reference verified' in result['final_text'] else 1
    finally:
        for server in servers: server.shutdown()


if __name__ == '__main__':
    sys.exit(main())
