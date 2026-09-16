#!/usr/bin/env python3
import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1] / "app"

def read_first(*paths):
    for path in paths:
        if path.exists():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError(paths)

MAIN_TEXT = (ROOT / "nuanyu_web.py").read_text(encoding="utf-8")
JS_TEXT = read_first(ROOT / "main.js", ROOT / "static/js/main.js")
HTML_TEXT = read_first(ROOT / "main.html", ROOT / "templates/main.html")
CSS_TEXT = read_first(ROOT / "main.css", ROOT / "static/css/main.css")
TTS_TEXT = read_first(ROOT / "fibo_tts.py", pathlib.Path("/userdata_fibo/tts_client/fibo_tts.py"))


def load_frame_parser():
    tree = ast.parse(MAIN_TEXT)
    selected = []
    wanted_assignments = {"VOICE_CMD_MAP", "VOICE_FRAME_SIZE", "VOICE_FRAME_PREFIX", "VOICE_FRAME_SUFFIX"}
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in wanted_assignments for target in node.targets
        ):
            selected.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == "extract_voice_commands":
            selected.append(node)
    namespace = {}
    exec(compile(ast.Module(body=selected, type_ignores=[]), "voice_parser", "exec"), namespace)
    return namespace["extract_voice_commands"]


class SleepWakeContractTest(unittest.TestCase):
    def test_uart_parser_handles_noise_fragmentation_and_multiple_frames(self):
        parse = load_frame_parser()
        buffer = bytearray(b"\x00\xa5")
        self.assertEqual(parse(buffer), [])
        buffer.extend(b"\x02\x5a\xa5\x03\x5a")
        self.assertEqual(parse(buffer), [
            ("A5 02 5A", "wakeup"),
            ("A5 03 5A", "study"),
        ])
        self.assertEqual(buffer, bytearray())

    def test_sleep_is_backend_enforced(self):
        self.assertIn('if not text or is_assistant_sleeping():', MAIN_TEXT)
        self.assertIn('if cmd == "wakeup":', MAIN_TEXT)
        self.assertIn('is_assistant_sleeping() and source != "voice"', MAIN_TEXT)
        self.assertIn('fibo_tts_set_muted(sleeping)', MAIN_TEXT)
        self.assertIn('threading.Thread(target=self.voice_loop', MAIN_TEXT)

    def test_ui_exposes_sleep_and_blocks_interaction(self):
        self.assertIn('id="sleepButton"', HTML_TEXT)
        self.assertIn('id="sleepCurtain"', HTML_TEXT)
        self.assertIn('class="icon-button voice-button"', HTML_TEXT)
        self.assertIn('voice-button-label">语音输入', HTML_TEXT)
        self.assertIn('aria-label="陪伴角色，点击让它动一动"', HTML_TEXT)
        self.assertIn('currentStatus.assistant_sleeping', JS_TEXT)
        self.assertIn('"结束录音"', JS_TEXT)
        self.assertIn('function playRandomMotion()', JS_TEXT)
        self.assertIn('function scheduleIdleDrift(delay)', JS_TEXT)
        self.assertIn('--idle-drift-x', JS_TEXT)
        self.assertIn('@keyframes playful-hop', CSS_TEXT)
        self.assertIn('@keyframes playful-wiggle', CSS_TEXT)
        self.assertIn('.sun-character:focus { outline: none; }', CSS_TEXT)
        self.assertIn('min-height: 36px;', CSS_TEXT)
        self.assertIn('content: "☾";', CSS_TEXT)
        self.assertIn('class="sun-eye-sleep left"', HTML_TEXT)
        self.assertIn('M170 245c14 16 32 16 45 0', HTML_TEXT)
        self.assertIn('sleep-z sleep-z-near', HTML_TEXT)

    def test_tts_has_hard_mute(self):
        self.assertIn('def fibo_tts_set_muted(muted):', TTS_TEXT)
        self.assertIn('if _tts_muted.is_set():', TTS_TEXT)


if __name__ == "__main__":
    unittest.main()
