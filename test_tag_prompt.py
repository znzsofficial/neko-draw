import ast
import pathlib
import unittest

SOURCE = pathlib.Path(__file__).with_name("plugin.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
FUNC = next(
    node
    for cls in TREE.body
    if isinstance(cls, ast.ClassDef) and cls.name == "NekoDraw"
    for node in cls.body
    if isinstance(node, ast.FunctionDef) and node.name == "_is_tag_prompt"
)
FUNC.decorator_list = []
NAMESPACE = {}
exec(compile(ast.Module(body=[FUNC], type_ignores=[]), "plugin.py", "exec"), NAMESPACE)
is_tag_prompt = NAMESPACE["_is_tag_prompt"]

ROSE = (
    "洛瑟琳德，齐脖红色短发，红色眼眸，头上别着一只黑色小鲨鱼发夹，微翘碎发。"
    "镜头脸部近景特写，眼神迷离慵懒，嘴角带着一抹戏谑撩人的坏笑，脸颊微红，精致的锁骨与肩膀，高质量精美动漫插画。"
)


class TagPromptTest(unittest.TestCase):
    def test_chinese_description_with_commas_is_allowed(self):
        self.assertFalse(is_tag_prompt(ROSE))

    def test_danbooru_tags_are_rejected(self):
        self.assertTrue(is_tag_prompt("masterpiece, best quality, 1girl, solo, red hair, smile, blush, indoors"))
        self.assertTrue(is_tag_prompt("红发, 红瞳, 短发, 发夹, 碎发, 特写, 坏笑, 脸红"))

    def test_short_sentence_is_allowed(self):
        self.assertFalse(is_tag_prompt("画一张她坐在窗边的图"))


if __name__ == "__main__":
    unittest.main()
