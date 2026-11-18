#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字形损坏守卫：commit 前扫描文本文件，拦截编码级与已知形近字损坏。

用法：
    python3 collector/tests/glyph_guard.py FILE...
    python3 collector/tests/glyph_guard.py --staged
    python3 collector/tests/glyph_guard.py --commit REF
    python3 collector/tests/glyph_guard.py --range A..B

退出码：0=clean, 1=HARD(确定性损坏), 2=SOFT(KNOWN_BAD 词表命中), 3=守卫自身失败(git 故障/跨 commit 空清单)
纯标准库，不写任何盘上产物。

建议（不强制）在 git pre-commit hook 中调用：
    #!/bin/sh
    python3 collector/tests/glyph_guard.py --staged || exit 1
git 不追踪 .git/hooks 里的钩子，clone 后需各自安装、换机器需重装，故只建议不强制。
不接入 SessionStart/Stop hook（fail-open 采集链路与阻断性质语义冲突）。

与 regression_guard.py 的关系：
- regression_guard 检测采集逻辑正确性（量化值/日期/模板/dedup_key）
- glyph_guard 检测文本编码完整性（字形损坏）
- 两者平级共存于 collector/tests/，不互相依赖，各自独立可跑
- 改 collect.py 前跑 regression_guard，commit 前跑 glyph_guard
"""
import os
import re
import subprocess
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------------
# KNOWN_BAD 形近字词表（SOFT，exit 2）
# --------------------------------------------------------------------------
# 维护纪律：
#   1. 每次发现新的形近字损坏，先修文件、再往词表补一对
#   2. 词表放在脚本内部（本常量），不单独建文件
#   3. 条目格式：'损坏形态': '正确形态'
#   4. 只正向匹配损坏形态（不检查正确形态是否存在）
#   5. 命中即真损坏，零误报
#   6. 词表定义行用 '# glyph-ok:损坏词' 抑制自身（只屏蔽列出的词，且命中须出现在标记之前；裸 # glyph-ok 不抑制任何命中）
KNOWN_BAD = {
    '扇描': '扫描',       # glyph-ok:扇描
    '绍对': '绝对',       # glyph-ok:绍对
    '重扇': '重扫',       # glyph-ok:重扇
    # 新发现的损坏对在此追加
}

# --------------------------------------------------------------------------
# 确定性损坏检测规则（HARD，exit 1）
# --------------------------------------------------------------------------
HARD_CHECKS = [
    ('U+FFFD 替换符', re.compile('\ufffd')),
    ('私用区 U+E000-F8FF', re.compile('[\ue000-\uf8ff]')),
    ('CJK 兼容表意 U+F900-FAFF', re.compile('[\uf900-\ufaff]')),
    ('康熙部首 U+2F00-2FDF', re.compile('[\u2f00-\u2fdf]')),
    ('CJK 部首补充 U+2E80-2EFF', re.compile('[\u2e80-\u2eff]')),
    ('CJK 兼容形式 U+FE30-FE4F', re.compile('[\ufe30-\ufe4f]')),
    ('C0 控制符', re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f]')),
    ('DEL U+007F', re.compile('\x7f')),
    ('C1 控制符 U+0080-009F', re.compile('[\x80-\x9f]')),
    ('零宽空格 U+200B', re.compile('\u200b')),
    ('零宽非连接 U+200C', re.compile('\u200c')),
    ('BOM 文件中 U+FEFF', re.compile('\ufeff')),
    ('Unicode 非字符 U+FDD0-FDEF', re.compile('[\ufdd0-\ufdef]')),
    ('Unicode 非字符 U+xFFFE/FFFF', re.compile(
        '[\ufffe\uffff\U0001fffe\U0001ffff\U0002fffe\U0002ffff'
        '\U0003fffe\U0003ffff\U0004fffe\U0004ffff\U0005fffe\U0005ffff'
        '\U0006fffe\U0006ffff\U0007fffe\U0007ffff\U0008fffe\U0008ffff'
        '\U0009fffe\U0009ffff\U000afffe\U000affff\U000bfffe\U000bffff'
        '\U000cfffe\U000cffff\U000dfffe\U000dffff\U000efffe\U000effff'
        '\U000ffffe\U000fffff\U0010fffe\U0010ffff]')),
]

# --------------------------------------------------------------------------
# 跳过的文件扩展名（二进制，不做字形检查）
# --------------------------------------------------------------------------
SKIP_EXT = {
    '.pdf', '.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff',
    '.zip', '.rar', '.7z', '.gz', '.tar', '.bz2', '.bundle', '.pack',
    '.xlsx', '.xlsm', '.xls', '.docx', '.doc', '.pptx', '.ppt',
    '.mp4', '.mov', '.mp3', '.wav', '.sqlite', '.db',
    '.pyc', '.pyo', '.so', '.dylib', '.dll', '.exe', '.app',
    '.woff', '.woff2', '.ttf', '.otf', '.eot',
}


def is_text_file(path):
    """判断是否为文本文件（按扩展名排除二进制）。"""
    _, ext = os.path.splitext(path)
    return ext.lower() not in SKIP_EXT


def _parse_glyph_ok(line):
    """解析行内 '# glyph-ok' 抑制标记，返回 (marker_pos, allowed_words) 或 None。

    allowed_words 为 None 表示裸标记（不抑制任何命中）；为 set 表示只抑制列出的词。
    抑制只对「标记之前出现、且词在列词表内」的命中生效（L-4 收窄粒度）。
    """
    pos = line.find('# glyph-ok')
    if pos < 0:
        return None
    rest = line[pos + len('# glyph-ok'):]
    if rest.startswith(':'):
        words = set()
        for w in rest[1:].replace(',', ' ').split():
            w = w.strip()
            if w:
                words.add(w)
        return (pos, words)
    return (pos, None)


def scan_bytes(raw, display_name):
    """扫描字节内容，返回 (hard_hits, soft_hits, suppressed_hits)。

    每项为 (line_no, rule_name, context_snippet)。
    工作树文件与 git blob 共用此检测逻辑（H-2）。
    """
    hard, soft, suppressed = [], [], []
    try:
        text = raw.decode('utf-8', errors='strict')
    except UnicodeDecodeError as e:
        return [(1, 'UTF-8 解码失败', str(e))], [], []

    # NFC 归一化检查（全文级）
    nfc = unicodedata.normalize('NFC', text)
    if nfc != text:
        for i, (a, b) in enumerate(zip(text, nfc)):
            if a != b:
                line_no = text[:i].count(chr(10)) + 1
                hard.append((line_no, 'NFC 归一化偏差',
                             'U+%04X -> U+%04X at char %d' % (ord(a), ord(b), i)))
                break
        else:
            hard.append((1, 'NFC 归一化偏差',
                         '长度 %d -> %d' % (len(text), len(nfc))))

    # 逐行检查 HARD_CHECKS 和 KNOWN_BAD
    lines = text.split(chr(10))
    for line_no, line in enumerate(lines, 1):
        for rule_name, pattern in HARD_CHECKS:
            # BOM 在文件头位置 0 是合法的，只检测非文件头位置
            if rule_name == 'BOM 文件中 U+FEFF' and line_no == 1:
                m = pattern.search(line, 1)
            else:
                m = pattern.search(line)
            if m:
                ctx = line[max(0, m.start() - 15):m.end() + 15]
                hard.append((line_no, rule_name, ctx))

        # KNOWN_BAD 词表匹配（收窄抑制：'# glyph-ok:词' 只屏蔽列出且位于标记之前的词）
        marker = _parse_glyph_ok(line)
        for bad, good in KNOWN_BAD.items():
            if bad in line:
                bidx = line.index(bad)
                ctx = line[max(0, bidx - 10):bidx + len(bad) + 10]
                rule = 'KNOWN_BAD「%s」(应为「%s」)' % (bad, good)
                if (marker is not None and marker[1] is not None
                        and bidx < marker[0] and bad in marker[1]):
                    suppressed.append((line_no, rule, ctx))
                else:
                    soft.append((line_no, rule, ctx))

    return hard, soft, suppressed


def scan_file(path):
    """扫描工作树单个文件，返回 (hard_hits, soft_hits, suppressed_hits)。"""
    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except OSError as e:
        return [('?', '读取失败', str(e))], [], []
    return scan_bytes(raw, path)


def _run_git(args, repo_root):
    """执行 git 命令，返回 (stdout_str, error_or_None)。error 非 None 即 git 故障。"""
    try:
        out = subprocess.run(['git', '-c', 'core.quotepath=false'] + args,
                             capture_output=True, text=True, timeout=30,
                             cwd=repo_root)
    except Exception as e:
        return '', 'git 调用异常：%r' % (e,)
    if out.returncode != 0:
        return '', 'git 返回码 %d：%s' % (out.returncode, out.stderr.strip())
    return out.stdout, None


def git_changed_files(mode, ref=None):
    """从 git 获取改动文件列表，返回 (files, error)。

    mode: 'staged' | 'commit' | 'range'；ref: commit/branch/range 字符串。
    error 非 None 表示 git 故障（守卫自身失败，语义独立于「无改动」）。
    """
    repo_root = os.path.dirname(os.path.dirname(HERE))
    if mode == 'staged':
        args = ['diff', '--cached', '--name-only', '--diff-filter=ACMR']
    elif mode == 'commit':
        args = ['diff-tree', '--no-commit-id', '-r', '--name-only',
                '--diff-filter=ACMR', ref or 'HEAD']
    elif mode == 'range':
        args = ['diff', '--name-only', '--diff-filter=ACMR', ref]
    else:
        return [], '未知 mode：%r' % (mode,)
    stdout, error = _run_git(args, repo_root)
    if error:
        return [], error
    return [f for f in stdout.strip().split(chr(10)) if f], None


def _blob_prefix(mode, ref):
    """构造 git show 的 blob 前缀：staged 用 ':'（索引），commit/range 用 '<ref>:'。

    range A..B / A...B 取右端 ref（改动落点）的 blob。
    """
    if mode == 'staged':
        return ':'
    if mode == 'commit':
        return '%s:' % (ref or 'HEAD')
    if mode == 'range':
        r = ref or 'HEAD'
        for sep in ('...', '..'):
            if sep in r:
                return '%s:' % r.split(sep)[-1]
        return '%s:' % r
    return None


def git_show_blob(prefix, path, repo_root):
    """取 ref 的 blob 字节，返回 (raw_bytes, error, missing)。

    missing=True 表示文件在该 ref 不存在（已删除/新增前），非故障，不静默剔除。
    """
    spec = prefix + path
    try:
        out = subprocess.run(['git', '-c', 'core.quotepath=false', 'show', spec],
                             capture_output=True, timeout=30, cwd=repo_root)
    except Exception as e:
        return b'', 'git show 异常：%r' % (e,), False
    if out.returncode != 0:
        err = out.stderr.decode('utf-8', 'replace').strip()
        low = err.lower()
        if 'does not exist' in low or 'not in' in low:
            return b'', None, True
        return b'', 'git show 返回码 %d：%s' % (out.returncode, err), False
    return out.stdout, None, False


def _scan_and_print(display, raw):
    """扫描字节并打印逐文件结果，返回 (hard, soft, suppressed)。"""
    hard, soft, suppressed = scan_bytes(raw, display)
    if not hard and not soft and not suppressed:
        print('PASS  %s (%d 行, %d 字节)' % (display, raw.count(bytes((10,))), len(raw)))
    else:
        for ln, rule, ctx in hard:
            print('FAIL  %s:L%s  %s  context: "%s"' % (display, ln, rule, ctx[:60]))
        for ln, rule, ctx in soft:
            print('WARN  %s:L%s  %s  context: "%s"' % (display, ln, rule, ctx[:60]))
        for ln, rule, ctx in suppressed:
            print('SUPPRESSED  %s:L%s  %s  context: "%s"' % (display, ln, rule, ctx[:60]))
    return hard, soft, suppressed


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__.strip())
        return 0

    repo_root = os.path.dirname(os.path.dirname(HERE))
    git_mode = None
    git_ref = None
    if args[0] == '--staged':
        git_mode = 'staged'
    elif args[0] == '--commit':
        git_mode = 'commit'
        git_ref = args[1] if len(args) > 1 else 'HEAD'
    elif args[0] == '--range':
        git_mode = 'range'
        git_ref = args[1] if len(args) > 1 else 'HEAD~1..HEAD'

    total_hard, total_soft, total_suppressed = [], [], []
    deleted = []
    scanned = 0

    if git_mode:
        rel_files, error = git_changed_files(git_mode, git_ref)
        if error:
            print('glyph_guard: 守卫自身失败（git 故障）：%s' % error, file=sys.stderr)
            return 3
        if not rel_files:
            # 跨 commit/范围为空本身即异常（正常范围必有改动）
            print('glyph_guard: 守卫自身失败（%s 模式返回空清单，ref=%r）'
                  % (git_mode, git_ref), file=sys.stderr)
            return 3
        prefix = _blob_prefix(git_mode, git_ref)
        print('=== glyph_guard: 扫描 %s@%s 的 %d 个改动文件（按 ref 取 blob）==='
              % (git_mode, prefix, len(rel_files)))
        for rel in rel_files:
            if not is_text_file(rel):
                continue
            raw, error, missing = git_show_blob(prefix, rel, repo_root)
            if error:
                print('glyph_guard: 守卫自身失败（取 blob %s）：%s'
                      % (rel, error), file=sys.stderr)
                return 3
            if missing:
                print('SKIP  %s（在 ref %s 不存在，已删除，跳过）' % (rel, prefix))
                deleted.append(rel)
                continue
            scanned += 1
            hard, soft, sup = _scan_and_print(rel, raw)
            total_hard += [(rel, ln, r) for ln, r, _ in hard]
            total_soft += [(rel, ln, r) for ln, r, _ in soft]
            total_suppressed += [(rel, ln, r) for ln, r, _ in sup]
    else:
        # H-1: FILE 模式不再静默剔除不存在的文件参数——与 git 模式「已删除→SKIP+计数」
        #      对齐：不存在者打印 SKIP 并计入「删除跳过 N 个」；若传入文件参数全部不存在
        #      （无可扫描且确有缺失），判为守卫自身失败 exit 3（而非旧行为 return 0）。
        #      存在但非文本（二进制扩展名）者仍按原样静默跳过、不计入 deleted。
        files = []
        for f in args:
            if not os.path.isfile(f):
                print('SKIP  %s（文件不存在，跳过）' % f)
                deleted.append(f)
                continue
            if not is_text_file(f):
                continue
            files.append(f)
        if not files:
            if deleted:
                print('glyph_guard: 守卫自身失败（FILE 模式 %d 个文件参数全部不存在）'
                      % len(deleted), file=sys.stderr)
                return 3
            print('glyph_guard: 无可扫描文件')
            return 0
        print('=== glyph_guard: 扫描 %d 个工作树文件 ===' % len(files))
        for p in files:
            try:
                display = os.path.relpath(p, repo_root)
            except ValueError:
                display = p
            try:
                with open(p, 'rb') as fh:
                    raw = fh.read()
            except OSError as e:
                print('FAIL  %s  读取失败: %s' % (display, e))
                total_hard.append((display, '?', '读取失败'))
                continue
            scanned += 1
            hard, soft, sup = _scan_and_print(display, raw)
            total_hard += [(display, ln, r) for ln, r, _ in hard]
            total_soft += [(display, ln, r) for ln, r, _ in soft]
            total_suppressed += [(display, ln, r) for ln, r, _ in sup]

    print('')
    print('扫描 %d 个文件，删除跳过 %d 个' % (scanned, len(deleted)))
    if total_hard or total_soft:
        print('===== %d HARD + %d SOFT + %d SUPPRESSED ====='
              % (len(total_hard), len(total_soft), len(total_suppressed)))
        for p, ln, r in total_hard:
            print(' - [HARD] %s:L%s %s' % (p, ln, r))
        for p, ln, r in total_soft:
            print(' - [SOFT] %s:L%s %s' % (p, ln, r))
        for p, ln, r in total_suppressed:
            print(' - [SUPPRESSED] %s:L%s %s' % (p, ln, r))
    elif total_suppressed:
        print('===== CLEAN（%d 处命中被 # glyph-ok 抑制，见上 SUPPRESSED 行，可复核） ====='
              % len(total_suppressed))
    else:
        print('===== CLEAN =====')

    if total_hard:
        return 1
    if total_soft:
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
