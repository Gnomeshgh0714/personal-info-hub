#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字形守卫测试（glyph_guard.py 的 H-1/H-2/L-4 行为锁定）。

用法：python3 collector/tests/test_glyph_guard.py
退出码 0 = 全部通过；1 = 有断言不符。
纯标准库，不写任何盘上产物（H-1 用进程内 monkeypatch 模拟 git 故障）。

与 regression_guard.py / test_oral_extract.py 平级独立，各自可跑。
锁定三条修复：
  H-1  git 故障 / 跨 commit 空清单 → main() 返回 3（守卫自身失败，不放行）
  H-2  --commit/--range/--staged 按 ref 取 blob（_blob_prefix / git_show_blob）
  L-4  '# glyph-ok' 抑制收窄：裸标记不抑制；列词标记只屏蔽列出且位于标记之前的词
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))

# 动态加载 glyph_guard.py
spec = importlib.util.spec_from_file_location(
    'GG', os.path.join(HERE, 'glyph_guard.py'))
GG = importlib.util.module_from_spec(spec)
spec.loader.exec_module(GG)

FAIL = []


def chk(name, ok, extra=''):
    """对齐 regression_guard / test_oral_extract 风格的断言输出。"""
    print(('PASS  ' if ok else 'FAIL  ') + name + (('  | ' + extra) if extra and not ok else ''))
    if not ok:
        FAIL.append(name)


# 从词表编程取损坏词，避免在测试里重打形近字
_BAD = list(GG.KNOWN_BAD.keys())
_W0 = _BAD[0]
_W1 = _BAD[1] if len(_BAD) > 1 else _BAD[0]

# ==========================================================================
# L-4：_parse_glyph_ok 标记解析
# ==========================================================================
print('=== L-4：# glyph-ok 标记解析 ===')

_m = GG._parse_glyph_ok('正文 ' + _W0 + ' 尾巴')
chk('无标记行返回 None', _m is None, repr(_m))

_m = GG._parse_glyph_ok('正文 # glyph-ok')
chk('裸标记 allowed=None（不抑制）',
    _m is not None and _m[1] is None, repr(_m))

_m = GG._parse_glyph_ok('正文 # glyph-ok:' + _W0 + ',' + _W1)
chk('列词标记 allowed=词集',
    _m is not None and _m[1] == {_W0, _W1}, repr(_m))

# ==========================================================================
# L-4：scan_bytes 抑制粒度
# ==========================================================================
print('\n=== L-4：scan_bytes 抑制粒度 ===')

# 裸标记 + 同行两处损坏：两处都应报 SOFT（旧行为会整行放行）
_line = (_W0 + '与' + _W1 + '同时出现 # glyph-ok').encode('utf-8')
_h, _s, _sup = GG.scan_bytes(_line, 't')
chk('裸标记不抑制：两处损坏均入 soft',
    len(_s) == 2 and len(_sup) == 0, 'soft=%d sup=%d' % (len(_s), len(_sup)))

# 列词标记只屏蔽列出的词：未列出的另一处仍报
_line = (_W0 + '与' + _W1 + '同时出现 # glyph-ok:' + _W0).encode('utf-8')
_h, _s, _sup = GG.scan_bytes(_line, 't')
_sup_rules = [r for _, r, _ in _sup]
_s_rules = [r for _, r, _ in _s]
chk('列词标记只抑制列出词（' + _W0 + ' 被抑制）',
    len(_sup) == 1 and _W0 in _sup_rules[0], repr(_sup_rules))
chk('列词标记不放过未列出词（' + _W1 + ' 仍报 SOFT）',
    len(_s) == 1 and _W1 in _s_rules[0], repr(_s_rules))

# 命中在标记之后：不抑制（只对标记之前生效）
_line = ('# glyph-ok:' + _W0 + ' 之后又出现 ' + _W0).encode('utf-8')
_h, _s, _sup = GG.scan_bytes(_line, 't')
chk('标记之后的命中不被抑制',
    len(_s) >= 1, 'soft=%d sup=%d' % (len(_s), len(_sup)))

# 干净文本：零命中
_h, _s, _sup = GG.scan_bytes('级别评定通过，特别奖学金到账'.encode('utf-8'), 't')
chk('无损坏词文本零命中',
    len(_h) == 0 and len(_s) == 0 and len(_sup) == 0,
    'h=%d s=%d sup=%d' % (len(_h), len(_s), len(_sup)))

# ==========================================================================
# H-2：_blob_prefix 取 ref（range 取右端）
# ==========================================================================
print('\n=== H-2：_blob_prefix ref 选取 ===')

chk('staged → 索引前缀 ":"', GG._blob_prefix('staged', None) == ':',
    repr(GG._blob_prefix('staged', None)))
chk('commit → "<ref>:"', GG._blob_prefix('commit', 'abc123') == 'abc123:',
    repr(GG._blob_prefix('commit', 'abc123')))
chk('commit 无 ref → HEAD:', GG._blob_prefix('commit', None) == 'HEAD:',
    repr(GG._blob_prefix('commit', None)))
chk('range A..B → 右端 B:', GG._blob_prefix('range', 'ad25dcd..HEAD') == 'HEAD:',
    repr(GG._blob_prefix('range', 'ad25dcd..HEAD')))
chk('range A...B → 右端 B:', GG._blob_prefix('range', 'v1...v2') == 'v2:',
    repr(GG._blob_prefix('range', 'v1...v2')))

# ==========================================================================
# H-2：git_show_blob 取 blob 字节 / missing 非故障
# ==========================================================================
print('\n=== H-2：git_show_blob ===')

_raw, _err, _miss = GG.git_show_blob('HEAD:', 'collector/tests/glyph_guard.py', REPO_ROOT)
chk('取 HEAD blob 成功且非空',
    _err is None and _miss is False and len(_raw) > 0,
    'err=%r miss=%r len=%d' % (_err, _miss, len(_raw)))
chk('blob 可 utf-8 解码', _raw.decode('utf-8', 'strict') is not None)

_raw2, _err2, _miss2 = GG.git_show_blob('HEAD:', 'no/such/file_xyz_不存在.txt', REPO_ROOT)
chk('不存在的 blob 报 missing（非故障）',
    _miss2 is True and _err2 is None, 'err=%r miss=%r' % (_err2, _miss2))

# ==========================================================================
# H-1：git_changed_files 返回 (files, error)
# ==========================================================================
print('\n=== H-1：git_changed_files 故障语义 ===')

_files, _err = GG.git_changed_files('range', 'HEAD..HEAD')
chk('空范围 → files=[] 且 error=None（非故障，由 main 判 3）',
    _files == [] and _err is None, 'files=%r err=%r' % (_files, _err))

_orig_run = GG.subprocess.run


def _boom(*a, **k):
    raise OSError('模拟 git 故障')


GG.subprocess.run = _boom
_files, _err = GG.git_changed_files('range', 'X..Y')
GG.subprocess.run = _orig_run
chk('git 异常 → error 非 None（不再压成空清单放行）',
    _err is not None and _files == [], 'files=%r err=%r' % (_files, _err))

# ==========================================================================
# H-1：main() 退出码 3
# ==========================================================================
print('\n=== H-1：main() 返回 3 ===')

_old_argv = sys.argv
try:
    sys.argv = ['glyph_guard.py', '--range', 'HEAD..HEAD']
    _rc = GG.main()
    chk('main() 空范围 → 返回 3', _rc == 3, 'rc=%r' % _rc)

    GG.subprocess.run = _boom
    sys.argv = ['glyph_guard.py', '--range', 'HEAD~1..HEAD']
    _rc = GG.main()
    GG.subprocess.run = _orig_run
    chk('main() git 故障 → 返回 3', _rc == 3, 'rc=%r' % _rc)
finally:
    sys.argv = _old_argv
    GG.subprocess.run = _orig_run

# ==========================================================================
# H-1：FILE 模式不存在文件参数 → exit 3 / 混合模式 SKIP 计入 deleted
# ==========================================================================
print('\n=== H-1：FILE 模式不存在文件参数 ===')

import io as _io
import contextlib as _cl

_old_argv2 = sys.argv
try:
    # 单个不存在文件 → 守卫自身失败 3（旧行为 return 0）
    sys.argv = ['glyph_guard.py', '/nonexistent/t28_no_such_file.py']
    _b = _io.StringIO()
    with _cl.redirect_stdout(_b):
        _rc = GG.main()
    _o = _b.getvalue()
    chk('FILE 单个不存在文件 → 返回 3', _rc == 3, 'rc=%r' % _rc)
    chk('FILE 不存在文件打印 SKIP 行', 'SKIP' in _o and '文件不存在' in _o, repr(_o[:90]))

    # 全部不存在 → 3
    sys.argv = ['glyph_guard.py', '/nonexistent/t28_a.py', '/nonexistent/t28_b.py']
    _b = _io.StringIO()
    with _cl.redirect_stdout(_b):
        _rc = GG.main()
    chk('FILE 全部不存在 → 返回 3', _rc == 3, 'rc=%r' % _rc)

    # 混合（存在且 CLEAN + 不存在）→ 存在者照常扫返回 0，不存在者计入「删除跳过 1 个」
    _exist = os.path.join(HERE, 'glyph_guard.py')
    sys.argv = ['glyph_guard.py', _exist, '/nonexistent/t28_ghost.md']
    _b = _io.StringIO()
    with _cl.redirect_stdout(_b):
        _rc = GG.main()
    _o = _b.getvalue()
    chk('FILE 混合模式存在者照常扫描 → 返回 0', _rc == 0, 'rc=%r' % _rc)
    chk('FILE 混合模式不存在者计入「删除跳过 1 个」', '删除跳过 1 个' in _o, repr(_o[-140:]))
finally:
    sys.argv = _old_argv2

# ==========================================================================
# 汇总
# ==========================================================================
print('')
print('===== %d 项不符 =====' % len(FAIL))
for f in FAIL:
    print(' - ' + f)
sys.exit(1 if FAIL else 0)
