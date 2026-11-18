#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T20 待核验「机械预筛」：只算转正名单 + 写 cursor「已核验」授权表 + 生成边界清单。

职责边界（铁律）：本脚本 **不碰 jsonl / 六视图 / 待核验队列**。转正行由采集器
自己在下一轮 `collect.py --incremental` 的 run() 闸门追加（版本+1、状态=已核验），
保证 append-only 的执行者唯一 = 采集器。本脚本只往 cursor「已核验」表写授权：
    dedup_key → h0 = record_hash(回源重抽的「待核验」态记录)
下轮闸门重算 fresh rec 的 h0，逐字命中授权才升状态；内容一变则不命中 + pop 授权
（自动降级回待核验）。所以 --apply 永远不会「误转正」——闸门是最终权威。

语义边界：「已核验」= 机器回源比对逐字吻合且不含任何数字断言，**不等于内容属实**；
事实认定仍归用户。含数字 / 需OCR / 盲区格式(.xlsx/.docx) / 排名类 / 主源冲突 /
回源不吻合一律不自动转正。

判据（保守档，①-⑧ + 前置 R0）与原因码映射：
    入口：判据① 状态==待核验（需OCR/无源/主源冲突态不入口，故无 R1/R4/R5 码）
    R0  前置：id 已退役（cursor 无该 dedup_key）
    R2  判据②：证据文件不存在或不可读
    R3  判据④：含数字（量化值 或 无源量化值 非空）—— 严禁虚假量化
    R6  判据⑥：盲区扩展名 {.xlsx,.docx}（回源重抽不可靠）
    R8  判据⑧：主源冲突组非空（需人工裁决）
    R7  判据⑦：_RANK_GRADE_RE 命中「名称+成果」（排名/成绩类，保守档拦截）
    R9  判据③：回源六字段不逐字吻合（附两侧差异明细）
    PASS 全过 → 可机械转正
评估序（首个不满足者定原因码，廉价判据在前、昂贵回源在最后）：
    R0 → R2 → R3 → R6 → R8 → R7 → R9 → PASS
判据⑦两档：保守档（含⑦，默认）R7 拦截；标准档（去⑦）R7 不作为拦截判据，
    R7 条目继续走 R9 → 落 R9 或 PASS。脚本同时算两档，供用户拍板。

用法：
    python3 collector/prescreen_verify.py --dry-run   # 只打印，不写盘、不写 cursor
    python3 collector/prescreen_verify.py --apply      # 写 cursor「已核验」表 + 边界清单.md
"""
import argparse
import importlib.util
import json
import os
import shutil
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location('C', os.path.join(HERE, 'collect.py'))
C = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(C)

BOUNDARY_PATH = os.path.join(C.BASE_DIR, '待核验边界清单.md')

REASON = {
    'R0': '前置R0：id 已退役（cursor 无该 dedup_key）',
    'R2': '判据②：证据文件不存在或不可读',
    'R3': '判据④：含数字（量化值/无源量化值非空）——严禁虚假量化，不自动转正',
    'R6': '判据⑥：盲区扩展名 {.xlsx,.docx}（回源重抽不可靠）',
    'R7': '判据⑦：_RANK_GRADE_RE 命中「名称+成果」（排名/成绩类，保守档拦截）',
    'R8': '判据⑧：主源冲突组非空（需人工裁决）',
    'R9': '判据③：回源六字段不逐字吻合（附两侧差异明细）',
    'PASS': '判据①-⑧ + R0 全过 → 可机械转正',
}
WATERFALL = ('R0', 'R2', 'R3', 'R6', 'R7', 'R8', 'R9', 'PASS')
SIX = ('名称', '成果', '时间', '角色', '分类', '证据定位')
NO_AUTO_CAT = ('教育背景', '获奖荣誉')   # 报告需确认为 0；非判据，靠 ④/⑦ 经验性拦截


def load_src_map():
    with open(C.SOURCES_PATH, encoding='utf-8') as f:
        cfg = json.load(f)
    return {(s.get('名称') or ''): s for s in (cfg.get('源清单') or [])}


def reextract(cache, src_map, 源清单, path, stats):
    """按 (源清单,路径) 缓存回源重抽；返回 (status, payload)。
    status ∈ OK/NO_SRC/ERR。OK 时 payload = 该文件重抽出的 rec 列表。"""
    ck = (源清单, path)
    if ck in cache:
        return cache[ck]
    src = src_map.get(源清单)
    if src is None:
        cache[ck] = ('NO_SRC', None)
        return cache[ck]
    fn = C.EXTRACTORS.get(src.get('提取器', 'doc_auto'), C.extract_doc_auto)
    try:
        cache[ck] = ('OK', fn(src, path, 'full', stats))
    except Exception as e:                                  # noqa: BLE001
        cache[ck] = ('ERR', '%s: %s' % (type(e).__name__, e))
    return cache[ck]


def compare_six(rec, fresh):
    return [(f, rec.get(f), fresh.get(f)) for f in SIX if rec.get(f) != fresh.get(f)]


def evaluate(rec, cursor, id2key, src_map, cache, stats):
    """对一条「待核验」记录跑判据瀑布。返回 dict：
    key/rc(保守档原因码)/rs(标准档原因码)/diff(R9差异)/fresh(回源命中rec)/h0(授权值)。"""
    # R0 前置：id 未退役 —— 用 cursor['id映射'] 反查权威 key。jsonl 不落「锚点」字段，
    # 简历 tex 的 key 是「锚点|PI|路径」，只能由 id 反查、不能按字段重建（否则误判 R0）。
    # 该 key 即闸门下一轮实际使用的 key，授权写在此 key 上才能被闸门命中。
    key = id2key.get(rec.get('id'))
    res = {'key': key, 'rc': None, 'rs': None, 'diff': None, 'fresh': None, 'h0': None}
    if key is None:
        res['rc'] = res['rs'] = 'R0'
        return res
    path = (rec.get('证据路径') or [''])[0]
    # R2 判据②：证据文件存在可读
    if not path or not os.path.isfile(path):
        res['rc'] = res['rs'] = 'R2'
        return res
    try:
        with open(path, 'rb') as f:
            f.read(1)
    except OSError:
        res['rc'] = res['rs'] = 'R2'
        return res
    # R3 判据④：含数字（量化值 / 无源量化值 皆须空）
    if rec.get('量化值') or rec.get('无源量化值'):
        res['rc'] = res['rs'] = 'R3'
        return res
    # R6 判据⑥：盲区扩展名
    if os.path.splitext(path)[1].lower() in C.PRESCREEN_BLIND_EXT:
        res['rc'] = res['rs'] = 'R6'
        return res
    # R8 判据⑧：主源冲突组非空
    if rec.get('主源冲突组'):
        res['rc'] = res['rs'] = 'R8'
        return res
    # R7 判据⑦：排名/成绩类（保守档拦截；标准档不拦）
    文 = (rec.get('名称') or '') + (rec.get('成果') or '')
    m7 = C._RANK_GRADE_RE.search(文)
    r7 = bool(m7)
    res['r7_kw'] = m7.group(0) if m7 else ''
    # R9 判据③：回源六字段逐字吻合（仅 R0/R2/R3/R6/R8 幸存者才重抽，昂贵放最后）
    status, payload = reextract(cache, src_map, rec.get('源清单', ''), path, stats)
    fresh = None
    if status == 'NO_SRC':
        res['diff'] = [('源清单', rec.get('源清单'), '未在 sources.json 找到，无法回源')]
    elif status == 'ERR':
        res['diff'] = [('回源异常', '', payload)]
    else:
        cands = [r for r in payload
                 if (r.get('名称') or '') == (rec.get('名称') or '')
                 and r.get('状态') == C.STATUS_PENDING]
        bestdiff = None
        for c in cands:
            d = compare_six(rec, c)
            if not d:
                fresh = c
                bestdiff = []
                break
            if bestdiff is None or len(d) < len(bestdiff):
                bestdiff = d
                fresh = None
        if fresh is None:
            res['diff'] = bestdiff if bestdiff else \
                [('名称', rec.get('名称'), '回源无同名「待核验」条目')]
    r9 = fresh is None
    if fresh is not None:
        res['fresh'] = fresh
        res['h0'] = C.record_hash(fresh)   # 授权值 = 回源「待核验」态哈希，与闸门次轮重算对齐
    # 保守档：R7 先于 R9（廉价在前）
    res['rc'] = 'R7' if r7 else ('R9' if r9 else 'PASS')
    # 标准档：去 R7
    res['rs'] = 'R9' if r9 else 'PASS'
    return res


def 画像(pairs):
    """转正名单画像：版本分布 / 扩展名分布 / 量化值全空 / 主源冲突全空。"""
    ver = Counter(str(r.get('版本')) for r, _ in pairs)
    ext = Counter(os.path.splitext((r.get('证据路径') or [''])[0])[1].lower() or '(无)'
                  for r, _ in pairs)
    cat = Counter(r.get('分类', '?') for r, _ in pairs)
    数字全空 = all(not r.get('量化值') and not r.get('无源量化值') for r, _ in pairs)
    冲突全空 = all(not r.get('主源冲突组') for r, _ in pairs)
    return ver, ext, cat, 数字全空, 冲突全空


def build_boundary(待核验, results, mode):
    """按原因码分节生成边界清单文本行。mode='保守'|'标准'。"""
    code_of = (lambda res: res['rc']) if mode == '保守' else (lambda res: res['rs'])
    分组 = {c: [] for c in WATERFALL}
    for rec, res in zip(待核验, results):
        分组[code_of(res)].append((rec, res))
    lines = ['# 待核验边界清单（机械预筛 %s档）' % mode, '',
             '> 生成时间：%s' % time.strftime('%Y-%m-%dT%H:%M:%S%z'),
             '> 由 `collector/prescreen_verify.py` 生成。**不自动转正的条目**按原因码分节列此，',
             '> 供人工核验/裁决。语义边界：「已核验」= 机器回源逐字吻合且不含数字断言，',
             '> **不等于内容属实**；事实认定仍归用户。',
             '> 判据④的数字门控只作用于结构化 `量化值`/`无源量化值` 字段；`名称`/`成果` 自由文本',
             '> 中的数字不单独门控，其可溯源性由判据③六字段逐字回源吻合保证，属实性仍归用户认定。', '',
             '## 判据瀑布计数', '',
             '| 原因码 | 含义 | 条数 |', '|--------|------|------|']
    for c in WATERFALL:
        lines.append('| %s | %s | %d |' % (c, REASON[c], len(分组[c])))
    lines += ['', '合计入口（状态==待核验）：%d' % len(待核验), '']
    for c in WATERFALL:
        if c == 'PASS':
            continue
        if not 分组[c]:
            continue
        lines += ['---', '', '## %s（%d 条）— %s' % (c, len(分组[c]), REASON[c]), '']
        if c == 'R9':
            lines += ['| # | id | 分类 | 名称 | 差异字段 | 库内值 | 回源值 |',
                      '|---|----|------|------|----------|--------|--------|']
            i = 0
            for rec, res in 分组[c]:
                for (f, a, b) in (res['diff'] or []):
                    i += 1
                    lines.append('| %d | %s | %s | %s | %s | %s | %s |' % (
                        i, rec.get('id'), rec.get('分类'),
                        _cell(rec.get('名称')), _cell(f), _cell(a), _cell(b)))
        elif c == 'R7':
            lines += ['| # | id | 分类 | 名称 | 命中词 | 证据路径 |',
                      '|---|----|------|------|--------|----------|']
            for i, (rec, res) in enumerate(分组[c], 1):
                lines.append('| %d | %s | %s | %s | %s | `%s` |' % (
                    i, rec.get('id'), _cell(rec.get('分类')), _cell(rec.get('名称')),
                    _cell(res.get('r7_kw', '')),
                    _cell(_short((rec.get('证据路径') or [''])[0]))))
        else:
            lines += ['| # | id | 分类 | 名称 | 证据路径 |',
                      '|---|----|------|------|----------|']
            for i, (rec, res) in enumerate(分组[c], 1):
                lines.append('| %d | %s | %s | %s | `%s` |' % (
                    i, rec.get('id'), _cell(rec.get('分类')), _cell(rec.get('名称')),
                    _cell(_short((rec.get('证据路径') or [''])[0]))))
        lines.append('')
    return lines


def _cell(v):
    s = '' if v is None else str(v)
    return s.replace('|', '\\|').replace('\n', ' ')[:80]


def _short(p):
    return p.replace(C.BASE_DIR, '…') if p else ''


def main():
    ap = argparse.ArgumentParser(description='T20 待核验机械预筛（只算名单+写cursor授权表+边界清单）')
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--dry-run', action='store_true', help='只打印，不写盘、不写 cursor')
    g.add_argument('--apply', action='store_true', help='写 cursor「已核验」表 + 边界清单.md')
    ap.add_argument('--mode', choices=('保守', '标准'), default='保守',
                    help='判据⑦取舍：保守(含⑦,默认) / 标准(去⑦)。--apply 时决定授权集')
    args = ap.parse_args()

    records = C.load_latest(C.JSONL_PATH)
    cursor = C.load_cursor()
    cursor_mtime0 = os.path.getmtime(C.CURSOR_PATH) if os.path.isfile(C.CURSOR_PATH) else None
    src_map = load_src_map()
    cache, stats = {}, C.Stats()

    id2key = {}                       # id → 权威 dedup_key（id 唯一，首个即权威）
    for _k, _v in cursor.get('id映射', {}).items():
        id2key.setdefault(_v, _k)
    待核验 = [r for r in records if r.get('状态') == C.STATUS_PENDING]
    results = [evaluate(r, cursor, id2key, src_map, cache, stats) for r in 待核验]

    cc = Counter(res['rc'] for res in results)   # 保守档
    cs = Counter(res['rs'] for res in results)   # 标准档
    pass_c = [(r, res) for r, res in zip(待核验, results) if res['rc'] == 'PASS']
    pass_s = [(r, res) for r, res in zip(待核验, results) if res['rs'] == 'PASS']

    print('=' * 72)
    print('T20 机械预筛  模式=%s  %s' % (args.mode, '(--dry-run 只打印，不写盘)'
                                          if args.dry_run else '(--apply)'))
    print('入口：状态==待核验 %d 条（jsonl 聚合后唯一 id %d 条）' % (len(待核验), len(records)))
    print('-' * 72)
    print('判据瀑布（保守档 含⑦ / 标准档 去⑦）：')
    print('  %-6s %-8s %-8s  %s' % ('码', '保守', '标准', '含义'))
    for c in WATERFALL:
        print('  %-6s %-8d %-8d  %s' % (c, cc.get(c, 0), cs.get(c, 0), REASON[c]))
    print('  %-6s %-8d %-8d' % ('入口', len(待核验), len(待核验)))
    print('-' * 72)
    边_c = len(待核验) - len(pass_c)
    边_s = len(待核验) - len(pass_s)
    print('两档取舍（判据⑦）：')
    print('  保守档(含⑦)：转正 %d / 边界 %d' % (len(pass_c), 边_c))
    print('  标准档(去⑦)：转正 %d / 边界 %d' % (len(pass_s), 边_s))
    print('  差额 = R7 命中且其余判据全过 = %d 条' % (len(pass_s) - len(pass_c)))
    print('-' * 72)

    # 转正名单画像（保守档）
    ver, ext, cat, 数字全空, 冲突全空 = 画像(pass_c)
    print('转正名单画像（保守档 %d 条）：' % len(pass_c))
    print('  版本分布：%s' % dict(sorted(ver.items())))
    print('  扩展名分布：%s' % dict(sorted(ext.items())))
    print('  分类分布：%s' % dict(sorted(cat.items())))
    print('  量化值/无源量化值全空：%s   主源冲突组全空：%s' % (数字全空, 冲突全空))
    print('-' * 72)

    # ===== 自洽性硬断言（任一触发 → 异常停手，不写盘）=====
    异常 = []
    if not 数字全空:
        异常.append('转正名单出现含数字条目（判据④失效）')
    if not 冲突全空:
        异常.append('转正名单出现主源冲突条目（判据⑧失效）')
    禁转 = [(r, res) for r, res in pass_c if r.get('分类') in NO_AUTO_CAT]
    if 禁转:
        异常.append('教育背景/获奖荣誉出现转正 %d 条：%s'
                    % (len(禁转), [r.get('id') for r, _ in 禁转]))
    anchor_pass = [(r, res) for r, res in pass_c if (res['key'] or '').startswith('锚点|')]
    if anchor_pass:
        异常.append('简历 tex（锚点条目）落 PASS %d 条：%s'
                    % (len(anchor_pass), [r.get('id') for r, _ in anchor_pass]))
    # 计数自洽：各档之和 == 入口
    if sum(cc.get(c, 0) for c in WATERFALL) != len(待核验):
        异常.append('保守档瀑布计数不自洽：%d != %d'
                    % (sum(cc.get(c, 0) for c in WATERFALL), len(待核验)))
    if sum(cs.get(c, 0) for c in WATERFALL if c != 'R7') != len(待核验):
        异常.append('标准档瀑布计数不自洽（标准档无 R7）')
    if 异常:
        print('!!! 自洽性硬断言触发（%d 项）—— 停手，不写盘：' % len(异常))
        for a in 异常:
            print('   - ' + a)
        print('=' * 72)
        return 3
    print('自洽性硬断言：全部通过（数字全空/冲突全空/教育获奖零转正/tex零PASS/计数自洽）')

    # 简历 tex（锚点条目）落点（报告用：用户预期这 34 条落 R3/R8、不落 PASS）
    anchor_all = [(r, res) for r, res in zip(待核验, results)
                  if (res['key'] or '').startswith('锚点|')]
    print('简历 tex（锚点条目 %d 条）保守档落点：%s'
          % (len(anchor_all), dict(Counter(res['rc'] for _, res in anchor_all))))
    # 非简历类 .tex（论文作品等）落 PASS 提示：判据未排除 .tex，透明呈报供用户定夺
    textex_pass = [(r, res) for r, res in pass_c
                   if (r.get('证据路径') or [''])[0].endswith('.tex')]
    if textex_pass:
        print('提示：非简历类 .tex（论文作品）落 PASS %d 条：%s（判据未排除 .tex；如需拦截请示下）'
              % (len(textex_pass), [r.get('id') for r, _ in textex_pass]))

    # R7 命中逐条（报告重点）
    r7 = [(r, res) for r, res in zip(待核验, results) if res['rc'] == 'R7']
    print('-' * 72)
    print('R7（判据⑦排名类）命中 %d 条 —— 逐条：' % len(r7))
    for r, res in r7:
        print('   %s [%s] %s | 命中词=%r | %s'
              % (r.get('id'), r.get('分类'), _cell(r.get('名称')),
                 res.get('r7_kw', ''), _short((r.get('证据路径') or [''])[0])))

    # cursor「已核验」表预览
    授权 = {res['key']: res['h0'] for r, res in
            (pass_c if args.mode == '保守' else pass_s)}
    print('-' * 72)
    print('cursor「已核验」授权表预览（%s档 %d 键，dedup_key → h0）：' % (args.mode, len(授权)))
    for i, (k, h) in enumerate(list(授权.items())[:8]):
        print('   %s → %s' % (k, h))
    if len(授权) > 8:
        print('   …（另 %d 键）' % (len(授权) - 8))

    # 边界清单
    blines = build_boundary(待核验, results, args.mode)
    print('-' * 72)
    print('边界清单（%s档，按原因码分节）摘要：' % args.mode)
    for c in WATERFALL:
        if c == 'PASS':
            continue
        n = cc.get(c, 0) if args.mode == '保守' else cs.get(c, 0)
        if n:
            print('   %s：%d 条 — %s' % (c, n, REASON[c]))

    if args.dry_run:
        print('=' * 72)
        print('[dry-run] 未写任何盘：cursor 未改、边界清单.md 未落、jsonl/视图/队列未碰。')
        print('[dry-run] 完整边界清单文本共 %d 行（--apply 时写入 %s）。'
              % (len(blines), _short(BOUNDARY_PATH)))
        print('=' * 72)
        return 0

    # ===== --apply：flock + mtime 校验 + 备份 + tmp/replace 写 cursor「已核验」表 =====
    import fcntl
    os.makedirs(C.STATE_DIR, exist_ok=True)
    lock_path = C.CURSOR_PATH + '.lock'
    with open(lock_path, 'w') as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            cur_mtime = os.path.getmtime(C.CURSOR_PATH) if os.path.isfile(C.CURSOR_PATH) else None
            if cur_mtime != cursor_mtime0:
                print('[ABORT] cursor 在预筛期间被改动（mtime %r → %r），拒绝写入。'
                      % (cursor_mtime0, cur_mtime))
                return 4
            if os.path.isfile(C.CURSOR_PATH):
                bak = C.CURSOR_PATH + '.bak-' + time.strftime('%Y%m%dT%H%M%S')
                shutil.copy2(C.CURSOR_PATH, bak)
                print('[apply] 已备份 cursor → %s' % os.path.basename(bak))
            fresh_cursor = C.load_cursor()
            fresh_cursor.setdefault('已核验', {})
            fresh_cursor['已核验'].update(授权)
            tmp = C.CURSOR_PATH + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(fresh_cursor, f, ensure_ascii=False, indent=1, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, C.CURSOR_PATH)
            with open(BOUNDARY_PATH, 'w', encoding='utf-8') as f:
                f.write('\n'.join(blines) + '\n')
            print('[apply] 已写 cursor「已核验」表 %d 键 + 边界清单 → %s'
                  % (len(授权), _short(BOUNDARY_PATH)))
            print('[apply] 下一步：python3 collector/collect.py --incremental 由闸门追加转正行。')
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
    print('=' * 72)
    return 0


if __name__ == '__main__':
    sys.exit(main())
