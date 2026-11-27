import sys
import types
import pytest

# Provide light-weight PySide6 stubs so tests can import main.py without GUI deps
if 'PySide6' not in sys.modules:
    class _DynamicModule(types.ModuleType):
        def __init__(self, name):
            super().__init__(name)
            self.__path__ = []

        def __getattr__(self, name):
            if name == '__all__':
                return []
            if name == '__path__':
                return []
            dummy = type(name, (), {})
            setattr(self, name, dummy)
            return dummy

    fake = _DynamicModule('PySide6')
    qw = _DynamicModule('PySide6.QtWidgets')
    qc = _DynamicModule('PySide6.QtCore')
    qg = _DynamicModule('PySide6.QtGui')
    qwc = _DynamicModule('PySide6.QtWebChannel')
    qwe = _DynamicModule('PySide6.QtWebEngineWidgets')

    class _Signal:
        def __init__(self, *args, **kwargs):
            pass
        def connect(self, *a, **k):
            pass
        def emit(self, *a, **k):
            pass

    def _slot(*args, **kwargs):
        def dec(fn):
            return fn
        return dec

    class _DynamicMeta(type):
        def __getattr__(cls, name):
            return 0

    class _Qt(metaclass=_DynamicMeta):
        pass

    qc.Qt = _Qt
    qc.Signal = _Signal
    qc.Slot = _slot
    fake.QtWidgets = qw
    fake.QtCore = qc
    fake.QtGui = qg
    fake.QtWebChannel = qwc
    fake.QtWebEngineWidgets = qwe
    sys.modules['PySide6'] = fake
    sys.modules['PySide6.QtWidgets'] = qw
    sys.modules['PySide6.QtCore'] = qc
    sys.modules['PySide6.QtGui'] = qg
    sys.modules['PySide6.QtWebChannel'] = qwc
    sys.modules['PySide6.QtWebEngineWidgets'] = qwe

from main import parse_pnach_text, build_pnach


def test_preserve_bracket_header_verbatim():
    text = "[Cheats/8-bit Patch]\npatch=1,EE,00123456,extended,00FF00FF\n"
    pd = parse_pnach_text(text)
    out = build_pnach(pd)
    assert "[Cheats/8-bit Patch]" in out


def test_split_header_with_remainder():
    text = "[50/60 FPS] author-asasega\npatch=1,EE,00999999,extended,00000001\n"
    pd = parse_pnach_text(text)
    # comments should include the bracket header as its own line and the remainder as separate comment
    assert "[50/60 FPS]" in pd.comments
    assert any("author-asasega" in c for c in pd.comments)
    out = build_pnach(pd)
    assert "[50/60 FPS]" in out
    assert "author-asasega" in out


def test_split_three_merged_cheats_by_hints_and_prefixes():
    # Three distinct patches, no blank lines, each with different addresses and inline hints
    text = (
        "// first hint\n"
        "patch=1,EE,20AAAAAA,extended,00000001\n"
        "// second hint\n"
        "patch=1,EE,10BBBBBB,extended,00000002\n"
        "// third hint\n"
        "patch=1,EE,00CCCCCC,extended,00000003\n"
    )
    pd = parse_pnach_text(text)
    out = build_pnach(pd)
    # Expect multiple [Cheats/...] sections in output
    cheats_headers = [ln for ln in out.splitlines() if ln.startswith("[Cheats/")]
    assert len(cheats_headers) >= 2
    # ensure each hint text is present somewhere in the output
    assert "first hint" in out
    assert "second hint" in out
    assert "third hint" in out


def test_single_line_inline_hint_splitting():
    # single inline hint on one patch line should be recognized
    text = (
        "patch=1,EE,20AAAAAA,extended,00000001 // speed up\n"
        "patch=1,EE,20AAAAAB,extended,00000002 // speed up\n"
        "patch=1,EE,20AAAAAC,extended,00000003 // different\n"
    )
    pd = parse_pnach_text(text)
    out = build_pnach(pd)
    assert "speed up" in out
    assert "different" in out


def test_nested_bracket_headers_and_mixed_comments():
    text = (
        "[Group A]\n"
        "// note: start\n"
        "patch=1,EE,20AAA001,extended,00000001\n"
        "[Subgroup] author=dev\n"
        "; semicolon comment\n"
        "patch=1,EE,20AAA002,extended,00000002\n"
    )
    pd = parse_pnach_text(text)
    out = build_pnach(pd)
    # ensure both bracket headers are preserved
    assert "[Group A]" in out
    assert "[Subgroup]" in out
    # ensure semicolon comment is preserved somewhere
    assert any('semicolon' in ln for ln in out.splitlines())
