import os
import pathlib
import sys
import unittest

# The importable package lives in <repo>/app (app/src, app/static, ...).
_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from src.persona.user_config import (
    build_user_persona_prompt,
    public_presets,
    sanitize_persona_config,
)


ROOT = pathlib.Path(__file__).resolve().parents[1] / "app"


class UserPersonaConfigTest(unittest.TestCase):
    def test_custom_config_is_sanitized_and_bounded(self):
        config = sanitize_persona_config({
            "persona_id": "custom",
            "personality": " 温柔   可靠 ",
            "language_style": "自然简短",
            "catchphrases": "慢慢来，交给我吧；慢慢来\n我们试试",
        })

        self.assertEqual(config["personality"], "温柔 可靠")
        self.assertEqual(
            config["catchphrases"],
            ["慢慢来", "交给我吧", "我们试试"],
        )

    def test_presets_are_available_for_frontend(self):
        presets = {item["persona_id"]: item for item in public_presets()}

        self.assertEqual(set(presets), {"default", "calm_boy", "gentle_girl"})
        self.assertIn("沉稳", presets["calm_boy"]["personality"])

    def test_catchphrases_are_low_frequency_and_never_forced(self):
        config = sanitize_persona_config({
            "personality": "温柔",
            "language_style": "自然",
            "catchphrases": ["慢慢来", "交给我吧"],
        })
        prompts = [
            build_user_persona_prompt(config, "turn-%d" % index)
            for index in range(1000)
        ]
        selected = [prompt for prompt in prompts if "本轮若语境自然" in prompt]

        self.assertGreater(len(selected), 150)
        self.assertLess(len(selected), 300)
        self.assertTrue(all("最多使用一次" in prompt for prompt in selected))
        self.assertTrue(all("不合适就不要使用" in prompt for prompt in selected))
        self.assertTrue(any("本轮不要使用口头禅" in prompt for prompt in prompts))

    def test_main_prompt_appends_user_persona_after_service_override(self):
        source = (ROOT / "nuanyu_web.py").read_text(encoding="utf-8")
        override_pos = source.index("# Phase 2: PersonaService override")
        persona_pos = source.index("build_user_persona_prompt", override_pos)
        sensor_pos = source.index("# Append after PersonaService", persona_pos)

        self.assertLess(override_pos, persona_pos)
        self.assertLess(persona_pos, sensor_pos)

    def test_frontend_separates_character_and_voice_settings(self):
        html = (ROOT / "templates" / "main.html").read_text(encoding="utf-8")
        js = (ROOT / "static" / "js" / "main.js").read_text(encoding="utf-8")

        self.assertIn('id="characterSettingsTitle"', html)
        self.assertIn('id="voiceSettingsTitle"', html)
        self.assertIn('id="personalityInput"', html)
        self.assertIn('id="catchphrasesInput"', html)
        self.assertIn('id="streamVoiceGroup"', html)
        self.assertIn('id="surgeVoiceSettingsGroup"', html)
        self.assertIn("personality: personality", js)
        self.assertIn("catchphrases: splitCatchphrases", js)


if __name__ == "__main__":
    unittest.main()
