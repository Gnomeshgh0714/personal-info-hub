#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tex 简历锚点迁移（口径 C，一次性）。

背景：简历 tex 条目的稳定身份原先靠 dedup_key 里的「名称」判别位，改名 / 跨节移位
会撕裂 id。本迁移给三份 tex 的每个条目上方插一行整行注释锚点 `% @pi: PI-XXXX`，
把身份位换成锚点（口径 C）：new_key = `锚点|PI|路径`，改名 / 跨节移位都不裂。

A（插 tex 锚点）+ B（改 cursor 三表 key）必须同进程连续完成：中间窗口若跑 collect，
锚点已入 tex 但 cursor 还是旧 key，会给这 34 条集体分配新 id、撕裂历史。

核心不变量：
  - 只改 key、不改 id、不改任何 rec 字段；三表（id映射/版本计数/记录哈希）值原样搬，
    序号与文件（mtime 签名）两表不动。
  - 锚点值一律从 cursor 当时态反查（id映射[old_key]），禁止硬编码任何 PI 清单；
    目标 tex 文件清单也从 cursor 的 tex key 反查得出，禁止硬编码文件名。
  - 插完锚点立即用打了补丁的 collect 解析器重解析，断言绑定序列 == 期望；任何不符
    （绑定序列≠期望 / diff 出现 - 行 / 锚点数≠条目数）→ 从 copy2 备份还原 tex 与
    cursor 两侧、sys.exit，绝不带病落盘。

零漂移：锚点是整行 `% @pi:` 注释、列 0 插入。TeX 遇 % 跳到行尾连换行一起吃掉，输出
完全不变（改前改后 pdftotext -layout 文本一致为准，不用字节 cmp——xelatex PDF 内嵌
时间戳常不等）。整行注释而非行尾注释：行尾注释会多吃一个空格、可能扰动垂直间距。

落盘位置：tex 备份与 tex_anchor_plan.json 台账放 collector/tests/fixtures/tex-backup/
<时间戳>/（被 git 追踪）；collector/state/ 是 gitignored、不入库，不能作还原源。tex
源文件本身在根仓被 .gitignore 忽略、未 tracked，git checkout 还原不了 → copy2 备份是
唯一还原源。

模块级纯函数 is_migrated / anchor_key / insert_anchors / strip_anchors 供护栏 import；
import 本模块不触发任何迁移（迁移逻辑在 main() / __main__ 守卫下）。
"""
import os
import sys
import json
import time
import shutil
import difflib
import argparse
import importlib.util

try:
    import fcntl
except ImportError:  # 非 POSIX 兜底（本仓在 macOS，正常有 fcntl）
    fcntl = None

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HERE, 'state')
CURSOR = os.path.join(STATE_DIR, 'cursor.json')
COLLECT = os.path.join(HERE, 'collect.py')
FIXTURES = os.path.join(HERE, 'tests', 'fixtures')
BACKUP_ROOT = os.path.join(FIXTURES, 'tex-backup')

# 目标目录：简历 tex 所在处。文件清单不硬编码，从 cursor 的 tex key 反查。
TARGET_DIR = os.path.join(os.path.dirname(HERE), 'examples', 'demo-sources', 'resume')

ANCHOR_LINE = '%% @pi: %s'        # 整行注释、列 0 插入（零漂移）；%% 转义出字面 %
ANCHOR_PREFIX = '锚点'             # 口径 C key 的字面前缀
MIGRATE_VERSION = 3                # 迁移后 key口径版本


# --------------------------------------------------------------------------
# 纯函数（供护栏 import；不触发迁移）
# --------------------------------------------------------------------------
def is_migrated(cursor: dict) -> bool:
    """幂等判据：key口径版本 >= 3 视为已迁移。"""
    try:
        return int(cursor.get('key口径版本', 0)) >= MIGRATE_VERSION
    except (TypeError, ValueError):
        return False


def anchor_key(锚点: str, 路径: str) -> str:
    """口径 C 锚点 key：`锚点|PI|路径`（与 collect.dedup_key 带锚点分支字节一致）。"""
    return '%s|%s|%s' % (ANCHOR_PREFIX, 锚点, 路径)


def insert_anchors(lines: list, inserts: list) -> list:
    """在每个 (行号1based, PI) 上方插入一行 `% @pi: PI`；自底向上插，保行号有效。

    纯函数：只依赖入参，不改文件。返回新列表。
    """
    out = list(lines)
    for 行号, pi in sorted(inserts, key=lambda x: -x[0]):
        out.insert(行号 - 1, ANCHOR_LINE % pi)
    return out


def strip_anchors(lines: list, anchor_re) -> list:
    """剥掉所有锚点注释行，用于「纯插入、零修改」证明：strip(new) 应 == old。"""
    return [l for l in lines if not anchor_re.match(l)]


# --------------------------------------------------------------------------
# collect 解析器加载 + 条目派生（复刻 extract_resume_tex 的分节/散段合并逻辑）
# --------------------------------------------------------------------------
def load_collect():
    spec = importlib.util.spec_from_file_location('_collect_anchor_src', COLLECT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def derive_entries(C, path: str, text: str) -> list:
    """把一份 tex 派生成逻辑条目（与 collect.extract_resume_tex 一一对应）。

    返回 [{起行, 分类, 名称, 锚点}]，按起行升序。锚点来自 parse_resume_tex 的
    pending_anchor 绑定（迁移前全空、迁移后为 PI）。散段按节合并、起行取最小、
    锚点跟随最早起行——与 extract_resume_tex 的 bucket 逻辑一致。
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    parsed = C.parse_resume_tex(text)
    buckets: dict = {}     # 节 -> {起行, 分类, 锚点}
    out: list = []
    for e in parsed:
        节 = (e.get('节') or '').strip()
        分类 = C.SECTION_CATEGORY.get(节, '实习经历')
        if e.get('散段') or not e['名称']:
            if 节 in C.SKIP_SECTIONS:
                continue
            b = buckets.setdefault(节, {'起行': e['起行'], '分类': 分类,
                                        '锚点': e.get('锚点', '')})
            if e['起行'] < b['起行']:
                b['起行'] = e['起行']
                b['锚点'] = e.get('锚点', '')
        else:
            out.append({'起行': e['起行'], '分类': 分类,
                        '名称': e['名称'], '锚点': e.get('锚点', '')})
    for 节, b in buckets.items():
        名称 = '%s（%s）' % (C.SECTION_CATEGORY.get(节, '简历条目'), stem)
        out.append({'起行': b['起行'], '分类': b['分类'],
                    '名称': 名称, '锚点': b['锚点']})
    out.sort(key=lambda x: x['起行'])
    return out


def build_plan(C, cursor: dict) -> dict:
    """从 cursor 当时态反查，构建三份 tex 的锚点插入计划（禁硬编码 PI / 文件名）。

    返回 {路径: {'entries': [{起行, 分类, 名称, old_key, 锚点}], 'lines': [...]}}。
    每条 old_key = dedup_key(分类,名称,[路径],'')，锚点 = id映射[old_key]。
    双射硬断言：派生 old_key 集合 == cursor 该文件的 tex key 集合。
    """
    idmap = cursor['id映射']
    # 目标 tex 路径从 cursor 反查（全路径前缀匹配，避开同 basename 异路径诱饵）
    目标路径 = sorted({k.split('|')[-1] for k in idmap
                       if k.split('|')[-1].startswith(TARGET_DIR)
                       and k.split('|')[-1].endswith('.tex')})
    if not 目标路径:
        sys.exit('[FATAL] cursor 里找不到 %s 下的 tex key，停机人工核查。' % TARGET_DIR)

    plan = {}
    for path in 目标路径:
        with open(path, 'r', encoding='utf-8', newline='') as f:
            text = f.read()
        if '\r\n' in text:
            sys.exit('[FATAL] %s 换行约定非 \\n，停机。' % path)
        lines = text.split('\n')
        entries = derive_entries(C, path, text)
        cursor_keys_for_file = {k for k in idmap
                                if k.split('|')[-1] == path}
        derived_keys = set()
        for e in entries:
            old_key = C.dedup_key(e['分类'], e['名称'], [path], '')
            if old_key not in idmap:
                sys.exit('[FATAL] 派生 old_key 不在 cursor：%r（%s）——解析器与库不一致，'
                         '停机人工核查，不猜。' % (old_key, os.path.basename(path)))
            e['old_key'] = old_key
            e['锚点'] = idmap[old_key]        # 反查，禁硬编码
            if not _valid_pi(e['锚点']):
                sys.exit('[FATAL] 反查到的锚点值非法：%r（old_key=%r）' % (e['锚点'], old_key))
            derived_keys.add(old_key)
        # 双射：派生 == cursor（无缺失、无多余）
        if derived_keys != cursor_keys_for_file:
            缺 = sorted(cursor_keys_for_file - derived_keys)
            余 = sorted(derived_keys - cursor_keys_for_file)
            sys.exit('[FATAL] %s 派生条目与 cursor tex key 不双射：缺 %d %r / 余 %d %r'
                     % (os.path.basename(path), len(缺), 缺, len(余), 余))
        plan[path] = {'entries': entries, 'lines': lines}
    return plan


def _valid_pi(v) -> bool:
    import re
    return bool(re.match(r'^PI-\d{4}$', str(v)))


# --------------------------------------------------------------------------
# 校验：纯插入 + 重解析绑定序列
# --------------------------------------------------------------------------
def verify_file(C, path: str, old_lines: list, entries: list, cursor: dict) -> list:
    """插锚点后重解析，断言绑定序列 == 期望。返回 new_lines。任何不符 sys.exit。"""
    inserts = [(e['起行'], e['锚点']) for e in entries]
    new_lines = insert_anchors(old_lines, inserts)

    # 1) 纯插入证明：剥掉锚点行应还原 old_lines（零修改、零删除）
    if strip_anchors(new_lines, C._ANCHOR_RE) != old_lines:
        sys.exit('[FATAL] %s 插入非纯增行（strip 后 != 原文），停机。' % os.path.basename(path))

    # 2) 锚点数 == 条目数
    n_anchor = sum(1 for l in new_lines if C._ANCHOR_RE.match(l))
    if n_anchor != len(entries):
        sys.exit('[FATAL] %s 锚点数 %d != 条目数 %d，停机。'
                 % (os.path.basename(path), n_anchor, len(entries)))

    # 3) 重解析绑定序列 == 期望（每条 old_key 反查的 PI == 重解析后绑定的锚点）
    idmap = cursor['id映射']
    new_text = '\n'.join(new_lines)
    re_entries = derive_entries(C, path, new_text)
    if len(re_entries) != len(entries):
        sys.exit('[FATAL] %s 重解析条目数 %d != 期望 %d，停机。'
                 % (os.path.basename(path), len(re_entries), len(entries)))
    for got, exp in zip(re_entries, entries):
        got_old = C.dedup_key(got['分类'], got['名称'], [path], '')
        if got_old != exp['old_key']:
            sys.exit('[FATAL] %s 重解析 old_key 序列错位：%r != 期望 %r，停机。'
                     % (os.path.basename(path), got_old, exp['old_key']))
        if got['锚点'] != exp['锚点']:
            sys.exit('[FATAL] %s 锚点绑定不符：old_key=%r 绑定=%r 期望=%r，停机。'
                     % (os.path.basename(path), got_old, got['锚点'], exp['锚点']))
        # 新 key（带锚点）必须 == 锚点|PI|路径，且 PI == cursor 反查值
        nk = C.dedup_key(got['分类'], got['名称'], [path], got['锚点'])
        if nk != anchor_key(exp['锚点'], path) or nk != C.dedup_key(
                got['分类'], got['名称'], [path], idmap[exp['old_key']]):
            sys.exit('[FATAL] %s new_key 与口径 C 不符：%r，停机。' % (os.path.basename(path), nk))
    return new_lines


def verify_diff_pure_add(old_lines: list, new_lines: list, path: str) -> list:
    """生成 unified diff，硬断言无 - 行（除 --- 头）。返回 diff 行列表。"""
    diff = list(difflib.unified_diff(old_lines, new_lines,
                                     fromfile='a/' + os.path.basename(path),
                                     tofile='b/' + os.path.basename(path),
                                     lineterm='', n=1))
    for d in diff:
        if d.startswith('-') and not d.startswith('---'):
            sys.exit('[FATAL] %s diff 出现删除行（应纯 + 行）：%r，停机。'
                     % (os.path.basename(path), d))
    return diff


# --------------------------------------------------------------------------
# cursor 三表改写（只改 key、值原样搬）
# --------------------------------------------------------------------------
def rewrite_cursor(cursor: dict, plan: dict) -> dict:
    """返回改写后的 cursor（内存副本，不落盘）。值原样搬，序号/文件两表不动。"""
    old_idmap = cursor['id映射']
    old_ver = cursor.get('版本计数', {})
    old_hash = cursor.get('记录哈希', {})

    rename = {}   # old_key -> new_key
    for path, info in plan.items():
        for e in info['entries']:
            nk = anchor_key(e['锚点'], path)
            rename[e['old_key']] = nk

    # new_key 互不碰撞、且不与任何保留 key 碰撞
    if len(set(rename.values())) != len(rename):
        sys.exit('[FATAL] new_key 出现碰撞，停机。')
    保留 = set(old_idmap) - set(rename)
    if set(rename.values()) & 保留:
        sys.exit('[FATAL] new_key 与保留 key 碰撞，停机。')

    new_idmap, new_ver, new_hash = {}, {}, {}
    for k, v in old_idmap.items():
        nk = rename.get(k, k)
        new_idmap[nk] = v                       # 值原样搬（id 不变）
    for k, v in old_ver.items():
        new_ver[rename.get(k, k)] = v           # 版本计数值不变
    for k, v in old_hash.items():
        new_hash[rename.get(k, k)] = v          # 记录哈希值不变

    # 数量关系断言（rename 是双射，三表键数不变）
    assert len(new_idmap) == len(old_idmap), 'id映射 键数变化'
    assert len(new_ver) == len(old_ver), '版本计数 键数变化'
    assert len(new_hash) == len(old_hash), '记录哈希 键数变化'

    out = dict(cursor)
    out['id映射'] = new_idmap
    out['版本计数'] = new_ver
    out['记录哈希'] = new_hash
    out['key口径版本'] = MIGRATE_VERSION
    out['key迁移时间'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    return out


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main() -> int:
    global TARGET_DIR
    ap = argparse.ArgumentParser(description='tex 简历锚点迁移（口径 C）')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--dry-run', action='store_true', help='只出计划+diff+cursor预览，不写盘（默认）')
    g.add_argument('--apply', action='store_true', help='同进程插锚点+改cursor，写盘')
    ap.add_argument('--target-dir', default=TARGET_DIR,
                    help='resume tex target dir (default: examples/demo-sources/resume/)')
    args = ap.parse_args()
    apply = args.apply            # 缺省即 dry-run（安全默认）
    TARGET_DIR = args.target_dir

    if not os.path.exists(CURSOR):
        sys.exit('[FATAL] cursor.json 不存在：%s' % CURSOR)
    C = load_collect()

    with open(CURSOR, 'r', encoding='utf-8') as f:
        cursor = json.load(f)

    if is_migrated(cursor):
        print('检测到 key口径版本=%s，已迁移，幂等跳过（不重复迁移）。'
              % cursor.get('key口径版本'))
        return 0

    plan = build_plan(C, cursor)
    n_entries = sum(len(info['entries']) for info in plan.values())

    # 逐文件插锚点 + 重解析验证（dry-run 也做，验证在内存、不写盘）
    new_texts = {}
    diffs = {}
    for path, info in plan.items():
        new_lines = verify_file(C, path, info['lines'], info['entries'], cursor)
        diffs[path] = verify_diff_pure_add(info['lines'], new_lines, path)
        new_texts[path] = '\n'.join(new_lines)

    new_cursor = rewrite_cursor(cursor, plan)

    print('===== tex 简历锚点迁移：%s =====' % ('APPLY（写盘）' if apply else 'DRY-RUN（不写盘）'))
    print('目标目录：%s' % TARGET_DIR)
    print('目标文件：%d 份；迁移条目：%d 条（全部从 cursor 反查锚点，禁硬编码）'
          % (len(plan), n_entries))
    print('key口径版本：%s → %d' % (cursor.get('key口径版本'), MIGRATE_VERSION))

    # --- 迁移计划 ---
    print('\n----- 迁移计划（%d 条）-----' % n_entries)
    for path in sorted(plan):
        info = plan[path]
        print('\n[%s] 行数 %d → %d，条目 %d'
              % (os.path.basename(path), len(info['lines']),
                 len(info['lines']) + len(info['entries']), len(info['entries'])))
        for i, e in enumerate(info['entries'], 1):
            new_ln = e['起行'] + (i - 1)   # 自底向上插后，本条锚点落在的新行号
            print('  %-7s above L%-4d→L%-4d  %s | %s'
                  % (e['锚点'], e['起行'], new_ln, e['分类'], e['名称'][:36]))

    # --- 三份 tex diff ---
    print('\n----- 三份 tex diff（必须全纯 + 行、无 - 行）-----')
    for path in sorted(diffs):
        print('\n### %s' % os.path.basename(path))
        for d in diffs[path]:
            print(d)

    # --- cursor 三表预览 ---
    print('\n----- cursor 三表改写预览（值不变、仅 key 改名、口径 %s→%d）-----'
          % (cursor.get('key口径版本'), MIGRATE_VERSION))
    old_idmap, new_idmap = cursor['id映射'], new_cursor['id映射']
    old_ver, new_ver = cursor.get('版本计数', {}), new_cursor['版本计数']
    old_hash, new_hash = cursor.get('记录哈希', {}), new_cursor['记录哈希']
    print('id映射：%d → %d 键（不变）；序号 %s → %s（不变）；文件表 %d（未动）'
          % (len(old_idmap), len(new_idmap), cursor.get('序号'),
             new_cursor.get('序号'), len(cursor.get('文件', {}))))
    print('版本计数：%d → %d 键（不变）' % (len(old_ver), len(new_ver)))
    print('记录哈希：%d → %d 键（不变）' % (len(old_hash), len(new_hash)))
    rename_pairs = []
    for path in sorted(plan):
        for e in plan[path]['entries']:
            nk = anchor_key(e['锚点'], path)
            rename_pairs.append((e['old_key'], nk, e['锚点'],
                                 old_ver.get(e['old_key']), old_hash.get(e['old_key'])))
    print('改名 %d 个 key（值原样搬：id/版本计数/记录哈希均不变）：' % len(rename_pairs))
    for ok, nk, pi, ver, h in rename_pairs:
        print('  %s\n     → %s   [版本计数=%s 记录哈希=%s 不变]'
              % (ok.split('|')[0] + '|' + ok.split('|')[1][:24] + '|' + os.path.basename(ok.split('|')[2]),
                 nk.split('|')[0] + '|' + pi + '|' + os.path.basename(nk.split('|')[2]), ver, h))

    if not apply:
        print('\n[dry-run] 未写任何盘：tex 未插锚点、cursor 未改、无备份、无台账。')
        print('[dry-run] 全部断言通过（纯 + 行 / 锚点数==条目数 / 重解析绑定序列==期望 / 三表键数不变）。')
        return 0

    # ============================ APPLY ============================
    # 备份（tex + cursor）到被 git 追踪的 fixtures 路径
    ts = time.strftime('%Y%m%d-%H%M%S')
    bak_dir = os.path.join(BACKUP_ROOT, ts)
    os.makedirs(bak_dir, exist_ok=True)
    for path in plan:
        shutil.copy2(path, os.path.join(bak_dir, os.path.basename(path)))
    cursor_bak = os.path.join(bak_dir, 'cursor.json')
    shutil.copy2(CURSOR, cursor_bak)

    written_tex = []
    try:
        # A：写三份 tex（tmp + os.replace 原子）
        for path in plan:
            mode = os.stat(path).st_mode & 0o777
            tmp = path + '.tmp-anchor'
            with open(tmp, 'w', encoding='utf-8', newline='') as f:
                f.write(new_texts[path])
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, path)
            written_tex.append(path)

        # B：改 cursor（flock 独占 + mtime 前后比对 + tmp/os.replace）
        fh = open(CURSOR, 'r', encoding='utf-8')
        if fcntl is not None:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX)
            except OSError as e:
                raise RuntimeError('无法取得 cursor 独占锁：%r' % e)
        m_now = json.load(fh)
        fh.close()
        if is_migrated(m_now):
            raise RuntimeError('cursor 在窗口内已被迁移（口径=%s），停机。'
                               % m_now.get('key口径版本'))
        tmp = CURSOR + '.tmp-anchor'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(new_cursor, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, CURSOR)

        # 台账
        ledger = {
            '说明': 'tex 简历锚点迁移（口径 C，key口径版本=3）。tex 与 cursor 的唯一还原源。',
            '迁移时间': new_cursor['key迁移时间'],
            '目标目录': TARGET_DIR,
            'cursor备份': 'cursor.json',
            '条目数': n_entries,
            '条目': [{'文件': os.path.basename(p), '锚点': e['锚点'],
                      '插入行_迁移前': e['起行'], 'old_key': e['old_key'],
                      'new_key': anchor_key(e['锚点'], p),
                      '分类': e['分类'], '名称': e['名称']}
                     for p in sorted(plan) for e in plan[p]['entries']],
        }
        lp = os.path.join(bak_dir, 'tex_anchor_plan.json')
        with open(lp, 'w', encoding='utf-8') as f:
            json.dump(ledger, f, ensure_ascii=False, indent=1, sort_keys=True)

    except Exception as e:                          # noqa: BLE001
        # 断言不符 / 写盘失败 → 从 copy2 备份还原 tex 与 cursor 两侧
        print('[FATAL] APPLY 失败，从备份还原两侧：%s' % e, file=sys.stderr)
        for path in written_tex:
            shutil.copy2(os.path.join(bak_dir, os.path.basename(path)), path)
        if os.path.exists(cursor_bak):
            shutil.copy2(cursor_bak, CURSOR)
        print('[还原] tex 与 cursor 已从 %s 还原；停机，不带病落盘。' % bak_dir, file=sys.stderr)
        return 1

    print('\n===== APPLY 完成 =====')
    print('备份+台账目录（被 git 追踪）：%s' % bak_dir)
    print('tex 插锚点：%d 份；cursor 改名：%d key；口径版本：%s → %d'
          % (len(written_tex), len(rename_pairs), cursor.get('key口径版本'), MIGRATE_VERSION))
    print('下一步（人工授权后）：跑 collect --incremental，预期恰追加 %d 行、版本全 2→3。' % n_entries)
    return 0


if __name__ == '__main__':
    sys.exit(main())
