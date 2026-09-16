import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1] / "app"
MAIN_JS = (ROOT / "static" / "js" / "main.js").read_text(encoding="utf-8")
MAIN_HTML = (ROOT / "templates" / "main.html").read_text(encoding="utf-8")


class StreamVoiceUIContractTest(unittest.TestCase):
    def test_stream_mode_initialization_loads_voice_list(self):
        self.assertIn("window.refreshStreamVoices = fetchStreamVoices;", MAIN_JS)
        self.assertIn('backend === "stream" && typeof window.refreshStreamVoices', MAIN_JS)
        self.assertIn("window.refreshStreamVoices();", MAIN_JS)

    def test_settings_poll_preserves_human_readable_voice_label(self):
        self.assertIn('"Stream · " + currentStreamVoiceLabel', MAIN_JS)
        self.assertNotIn(
            'setText(ui.currentVoice, ui.ttsModeSelect ? ui.ttsModeSelect.value',
            MAIN_JS,
        )

    def test_persona_handler_does_not_bind_preview_buttons(self):
        # The persona click handler must stay scoped to the settings-panel
        # preset group; a bare ".persona-selector [data-persona]" selector also
        # matches the read-only preview buttons elsewhere in the page.
        self.assertIn(
            'document.querySelectorAll("#characterPresetSelector [data-persona]")',
            MAIN_JS,
        )
        self.assertNotIn(
            'document.querySelectorAll(".persona-selector [data-persona]")',
            MAIN_JS,
        )

    def test_script_cache_key_is_bumped(self):
        self.assertIn("main.js?v=20260725-persona-voice-2", MAIN_HTML)


if __name__ == "__main__":
    unittest.main()
