from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_PATH = SKILL_DIR / "scripts" / "apply_static_visual_overrides.py"
sys.path.insert(0, str(SCRIPT_PATH.parent))
SPEC = importlib.util.spec_from_file_location("apply_static_visual_overrides", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"无法加载：{SCRIPT_PATH}")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SubjectAliasTests(unittest.TestCase):
    def test_aliases_merge_people_and_preserve_audio(self) -> None:
        facts = {
            "schema_version": 1,
            "no_speech_confirmed": False,
            "subjects": [
                {
                    "label": "<高马尾模特>",
                    "kind": "character",
                    "original_static_description": "高马尾女性",
                },
                {
                    "label": "<化妆师>",
                    "kind": "operator",
                    "original_static_description": "化妆师的手",
                },
                {
                    "label": "<产品>",
                    "kind": "product",
                    "original_static_description": "眼线笔",
                },
            ],
            "segments": [
                {
                    "index": 1,
                    "source_start_seconds": 0,
                    "source_end_seconds": 4,
                    "duration_seconds": 4,
                    "shots": [
                        {
                            "index": 1,
                            "start_seconds": 0,
                            "end_seconds": 4,
                            "shot_scale": "近景",
                            "camera": "固定机位",
                            "composition": "<高马尾模特>居中，<化妆师>在右侧",
                            "visible_body_range": "<高马尾模特>面部与<化妆师>双手",
                            "subject_action": "<高马尾模特>闭眼",
                            "operator_product_action": "<化妆师>使用<产品>",
                            "entry_exit": "无主体进出场",
                            "scene_light": "柔和室内光",
                            "beats": [
                                {
                                    "index": 1,
                                    "start_seconds": 0,
                                    "end_seconds": 4,
                                    "action": "<化妆师>为<高马尾模特>画眼线",
                                }
                            ],
                            "audio": "{保持原台词}",
                        }
                    ],
                }
            ],
        }
        aliases = {
            "<高马尾模特>": "<角色卡人物>",
            "<化妆师>": "<角色卡人物>",
        }
        updated, bindings = MODULE.apply_subject_aliases(
            facts,
            {"<高马尾模特>": [1], "<化妆师>": [], "<产品>": []},
            aliases,
            {"<角色卡人物>": "@图片1定义为<角色卡人物>。"},
        )

        self.assertEqual(
            [subject["label"] for subject in updated["subjects"]],
            ["<角色卡人物>", "<产品>"],
        )
        self.assertEqual(updated["subjects"][0]["kind"], "character")
        shot = updated["segments"][0]["shots"][0]
        self.assertNotIn("高马尾", shot["composition"])
        self.assertNotIn("化妆师", shot["composition"])
        self.assertEqual(shot["audio"], "{保持原台词}")
        self.assertEqual(bindings["<角色卡人物>"], [1])


if __name__ == "__main__":
    unittest.main()
