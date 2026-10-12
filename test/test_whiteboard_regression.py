"""回归测试：验证 .drawio 接入不影响现有 _judge_type/_pull_file 流程。"""
import os
import sys
import shutil
import tempfile

# 基于文件位置定位，避免依赖硬编码的绝对路径（换机器 / 换盘符也不会失效）
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))   # .../test
ROOT_DIR = os.path.dirname(SCRIPT_DIR)                    # 项目根目录

sys.path.insert(0, ROOT_DIR)

from pull import YoudaoNotePull, FileType


def test_judge_type():
    p = YoudaoNotePull()

    class FakeAPI:
        def get_file_by_id(self, file_id):
            class R:
                content = b'<?xml version="1.0"'
            return R()

    p.youdaonote_api = FakeAPI()

    assert p._judge_type("id", ".drawio") == FileType.DRAWIO, "DRAWIO 判定失败"
    assert p._judge_type("id", ".md") == FileType.MARKDOWN, "MD 判定失败"
    assert p._judge_type("id", ".note") == FileType.XML, "XML 判定失败"
    assert p._judge_type("id", ".txt") == FileType.OTHER, "OTHER 判定失败"
    print("test_judge_type OK")


def test_gen_drawio_companion_md():
    p = YoudaoNotePull()
    tmp = tempfile.mkdtemp()
    try:
        # 模拟 drawio 桌面版导出的 .drawio（SVG+base64 mxfile）
        drawio_path = os.path.join(tmp, "流程图.drawio")
        with open(drawio_path, "wb") as f:
            f.write(b"data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=")
        p._gen_drawio_companion_md(drawio_path)
        # 验证 companion md
        md_path = os.path.join(tmp, "流程图.md")
        assert os.path.exists(md_path), f"未生成 companion md: {md_path}"
        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "流程图.drawio" in content, "未引用原 .drawio 文件名"
        assert "drawio 桌面版 SVG" in content, "未标注为 SVG 内嵌 mxfile"
        # 验证 .drawio 本体未被修改
        with open(drawio_path, "rb") as f:
            assert f.read() == b"data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=", ".drawio 本体被破坏！"
        print("test_gen_drawio_companion_md OK (drawio 本体保持不变)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_gen_drawio_companion_md_no_svg():
    """测试原始 drawio XML 格式（不以 SVG 开头）"""
    p = YoudaoNotePull()
    tmp = tempfile.mkdtemp()
    try:
        drawio_path = os.path.join(tmp, "raw.drawio")
        with open(drawio_path, "wb") as f:
            f.write(b'<mxfile host="test"><diagram name="p1"/></mxfile>')
        p._gen_drawio_companion_md(drawio_path)
        md_path = os.path.join(tmp, "raw.md")
        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "raw.drawio" in content
        assert "drawio XML" in content, "未识别为原始 drawio XML"
        print("test_gen_drawio_companion_md_no_svg OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_existing_note_still_works():
    """确认现有 .note XML→md 转换不受影响"""
    from core.covert import YoudaoNoteConvert
    p = YoudaoNotePull()
    tmp = tempfile.mkdtemp()
    try:
        # 复制 test.note 验证 XML 转换仍工作
        src = os.path.join(SCRIPT_DIR, "test.note")
        dst = os.path.join(tmp, "test.note")
        shutil.copy2(src, dst)
        # XML 头 5 字节 = b'<?xml'
        with open(dst, "rb") as f:
            head = f.read(5)
        assert head == b"<?xml", f"test.note 头不是 XML: {head}"
        # 直接调用 covert_xml_to_markdown
        YoudaoNoteConvert.covert_xml_to_markdown(dst)
        # 验证 .md 已生成，.note 已被替换或删除
        md_path = os.path.join(tmp, "test.md")
        assert os.path.exists(md_path), "XML→md 转换失败"
        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "无序列表 1" in content, "列表项未转换"
        assert "一级标题" in content, "标题未转换"
        print("test_existing_note_still_works OK (XML 转换未受影响)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_existing_json_still_works():
    """确认现有 .note JSON→md 转换不受影响"""
    from core.covert import YoudaoNoteConvert
    tmp = tempfile.mkdtemp()
    try:
        src = os.path.join(SCRIPT_DIR, "test.json")
        dst = os.path.join(tmp, "test.json")
        shutil.copy2(src, dst)
        YoudaoNoteConvert.covert_json_to_markdown(dst)
        md_path = os.path.join(tmp, "test.md")
        assert os.path.exists(md_path), "JSON→md 转换失败"
        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "无序列表 1" in content, "JSON 列表项未转换"
        assert "一级标题" in content, "JSON 标题未转换"
        print("test_existing_json_still_works OK (JSON 转换未受影响)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_judge_type()
    test_gen_drawio_companion_md()
    test_gen_drawio_companion_md_no_svg()
    test_existing_note_still_works()
    test_existing_json_still_works()
    print("\n=== 全部回归测试通过 ===")
