import ast
import pathlib
import types
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1] / "app"


def load_get_assistant_name():
    tree = ast.parse((ROOT / "nuanyu_web.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "NuanyuCore":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "get_assistant_name":
                    module = ast.fix_missing_locations(ast.Module(body=[item], type_ignores=[]))
                    namespace = {"DEFAULT_ASSISTANT_NAME": "陪伴助手"}
                    exec(compile(module, "nuanyu_web.py", "exec"), namespace)
                    return namespace["get_assistant_name"]
    raise AssertionError("get_assistant_name not found")


class RoleNameContractTest(unittest.TestCase):
    def test_explicit_user_name_is_never_treated_as_a_legacy_default(self):
        get_assistant_name = load_get_assistant_name()

        for name in ("小培", "小陪", "小裴"):
            with self.subTest(name=name):
                robot = types.SimpleNamespace(memory={"assistant_name": name})
                self.assertEqual(get_assistant_name(robot), name)


if __name__ == "__main__":
    unittest.main()
