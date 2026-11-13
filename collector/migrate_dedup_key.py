#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dedup_key 口径迁移（一次性，方案 A2）。

只处理 collector/state/cursor.json，不碰 jsonl、不碰视图。核心不变量：
  「只改 key、不改 id、不改任何 rec 字段」——三表（id映射/版本计数/记录哈希）
  按新口径重算 key，值原样搬；序号与文件（mtime 签名）两表不动。

新口径（与 collect.py dedup_key 一致）：
  路径 endswith('.tex')  →  原样（分类|名称|路径），简历分节名称仍是判别位
  否则                    →  分类|路径|单条，一文件一条、路径即身份

关键约束：
  - 旧 key split('|') 硬断言恰好 3 段，异常即 sys.exit 停机（不猜）。
  - 完全从 cursor 当时态推导，禁止硬编码任何 key 数量断言（cursor 是移动靶）。
  - 合并组只允许目标对 {PI-0564, PI-0567}；出现任何预期外合并组立即 sys.exit。
  - 合并落地：保留 PI-0567、退役 PI-0564；PI-0564 旧 key 原值写墓碑。
  - 孤儿 PI-0188/PI-0189 保留并按同规则重写，存在性硬断言防丢失。
  - tmp + os.replace 原子落盘；flock 独占锁 + 写前 stat 比对 mtime 防并发。

模块级纯函数 new_key() / is_migrated() 供 regression_guard G10 import，
import 本模块不会触发任何迁移（迁移逻辑在 main() / __main__ 守卫下）。
"""
import os
import sys
import json
import time
import shutil

try:
    import fcntl
except ImportError:  # 非 POSIX 兜底（本仓在 macOS，正常有 fcntl）
    fcntl = None

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(HERE, 'state')
CURSOR = os.path.join(STATE_DIR, 'cursor.json')
TOMBSTONE = os.path.join(STATE_DIR, 'key_migration_tombstone.json')

TARGET_MERGE = frozenset({'PI-0564', 'PI-0567'})
KEEPER = 'PI-0567'      # 合并后保留的 id
RETIRED = 'PI-0564'     # 合并后退役的 id
ORPHANS = ('PI-0188', 'PI-0189')  # 只在 id映射、不在版本计数/记录哈希的孤儿


# --------------------------------------------------------------------------
# 纯函数（供 G10 fixture import；不触发迁移）
# --------------------------------------------------------------------------
def new_key(old_key: str) -> str:
    """旧口径 key（分类|名称|路径，恰好 3 段）→ 新口径 key。

    非 3 段一律 sys.exit 停机（不猜、不静默截断）。
    tex 路径原样返回（字节不变）；其余重写为 分类|路径|单条。
    """
    parts = old_key.split('|')
    if len(parts) != 3:
        sys.exit('旧 key 段数异常（应为 3 段）：%r' % old_key)
    分类, 名称, 路径 = parts
    if 路径.endswith('.tex'):
        return old_key
    return '%s|%s|单条' % (分类, 路径)


def is_migrated(cursor: dict) -> bool:
    """幂等判据：key口径版本 >= 2 视为已迁移。"""
    try:
        return int(cursor.get('key口径版本', 0)) >= 2
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------
# 迁移主体
# --------------------------------------------------------------------------
def main() -> None:
    if not os.path.exists(CURSOR):
        sys.exit('cursor.json 不存在：%s' % CURSOR)

    fh = open(CURSOR, 'r', encoding='utf-8')
    if fcntl is not None:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX)
        except OSError as e:
            sys.exit('无法取得 cursor 独占锁（可能有并发写入）：%r' % e)
    m0 = os.stat(CURSOR).st_mtime
    cursor = json.load(fh)

    # 幂等守卫
    if is_migrated(cursor):
        print('检测到 key口径版本=%s，已迁移，幂等跳过（不重复迁移）。'
              % cursor.get('key口径版本'))
        fh.close()
        return

    old_idmap = cursor['id映射']
    old_vercount = cursor.get('版本计数', {})
    old_rechash = cursor.get('记录哈希', {})
    old_seq = cursor['序号']

    # 1. 逐 old_key 算 new_key，按 new_key 分组
    groups = {}
    for ok in old_idmap:
        nk = new_key(ok)  # 非 3 段在此 sys.exit
        groups.setdefault(nk, []).append(ok)

    # 2. 找合并组（同一 new_key 承载 >1 个 old_key）
    merges = {nk: oks for nk, oks in groups.items() if len(oks) > 1}
    if not merges:
        sys.exit('未检测到任何合并组：目标对 PI-0564/PI-0567 未碰撞，'
                 'cursor 当时态与方案不符，停机人工核查。')
    for nk, oks in merges.items():
        ids = frozenset(old_idmap[ok] for ok in oks)
        if ids != TARGET_MERGE:
            sys.exit('预期外合并组 new_key=%r ids=%r（只允许 %r），'
                     '停机人工裁决，不自作主张合并。'
                     % (nk, sorted(ids), sorted(TARGET_MERGE)))

    # 3. 重建三表（值原样搬；合并组保留 KEEPER、退役 RETIRED）
    new_idmap, new_vercount, new_rechash = {}, {}, {}
    tombstone_entries = []
    for nk, oks in groups.items():
        if len(oks) == 1:
            ok = oks[0]
            rid = old_idmap[ok]
            new_idmap[nk] = rid
            if ok in old_vercount:
                new_vercount[nk] = old_vercount[ok]
            if ok in old_rechash:
                new_rechash[nk] = old_rechash[ok]
        else:
            keeper_ok = [ok for ok in oks if old_idmap[ok] == KEEPER]
            retired_ok = [ok for ok in oks if old_idmap[ok] == RETIRED]
            if len(keeper_ok) != 1 or len(retired_ok) != 1:
                sys.exit('合并组内 KEEPER/RETIRED 定位异常：keeper=%r retired=%r'
                         % (keeper_ok, retired_ok))
            keeper_ok, retired_ok = keeper_ok[0], retired_ok[0]
            new_idmap[nk] = KEEPER
            if keeper_ok in old_vercount:
                new_vercount[nk] = old_vercount[keeper_ok]
            if keeper_ok in old_rechash:
                new_rechash[nk] = old_rechash[keeper_ok]
            tombstone_entries.append({
                '退役_id': RETIRED,
                '退役_old_key': retired_ok,
                '退役_版本计数': old_vercount.get(retired_ok),
                '退役_记录哈希': old_rechash.get(retired_ok),
                '合并入_new_key': nk,
                '合并入_id': KEEPER,
                'keeper_old_key': keeper_ok,
                'keeper_版本计数': old_vercount.get(keeper_ok),
                'keeper_记录哈希': old_rechash.get(keeper_ok),
            })

    # 4. 关系断言（从当时态推导，禁止硬编码绝对数量）
    n_net = sum(len(oks) - 1 for oks in merges.values())  # 合并净减
    assert len(new_idmap) == len(old_idmap) - n_net, \
        'id映射 数量关系异常：%d != %d - %d' % (len(new_idmap), len(old_idmap), n_net)
    assert len(new_vercount) == len(old_vercount) - n_net, \
        '版本计数 数量关系异常：%d != %d - %d' % (len(new_vercount), len(old_vercount), n_net)
    assert len(new_rechash) == len(old_rechash) - n_net, \
        '记录哈希 数量关系异常：%d != %d - %d' % (len(new_rechash), len(old_rechash), n_net)

    # 序号与文件表不动
    assert cursor['序号'] == old_seq, '序号被意外改动'

    # tex 分支字节不变（对称差为空）
    tex_old = {ok for ok in old_idmap if ok.split('|')[2].endswith('.tex')}
    tex_new = {nk for nk in new_idmap if nk.split('|')[2].endswith('.tex')}
    assert tex_old == tex_new, 'tex key 集合发生变化（应字节不变）'

    # 孤儿存在性硬断言
    new_ids = set(new_idmap.values())
    for orph in ORPHANS:
        assert orph in new_ids, '孤儿 %s 在迁移后丢失' % orph

    # id 值域集合不变（合并不新增/不复用 id，只减 RETIRED 的独立 key）
    assert new_ids == set(old_idmap.values()) - {RETIRED}, \
        'id 值域集合异常：迁移后应恰为旧值域减去退役 id'

    # 5. 写盘前比对 mtime（并发检测）
    m1 = os.stat(CURSOR).st_mtime
    if m1 != m0:
        fh.close()
        sys.exit('cursor 在迁移窗口被外部修改（mtime %s→%s），停机。' % (m0, m1))

    ts = time.strftime('%Y%m%d-%H%M%S')
    bak = CURSOR + '.bak-' + ts
    shutil.copy2(CURSOR, bak)

    # 落盘新 cursor（序号/文件两表原样保留）
    cursor['id映射'] = new_idmap
    cursor['版本计数'] = new_vercount
    cursor['记录哈希'] = new_rechash
    cursor['key口径版本'] = 2
    cursor['key迁移时间'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')

    tmp = CURSOR + '.tmp-migrate'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cursor, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, CURSOR)

    # 墓碑
    tomb = {
        '说明': 'dedup_key 口径迁移（key口径版本=2）退役 key 墓碑。'
                '回滚时用于还原退役 id 的 cursor 原值（版本计数是唯一还原来源）。',
        '迁移时间': cursor['key迁移时间'],
        'cursor备份': os.path.basename(bak),
        '退役条目': tombstone_entries,
    }
    tt = TOMBSTONE + '.tmp'
    with open(tt, 'w', encoding='utf-8') as f:
        json.dump(tomb, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tt, TOMBSTONE)

    fh.close()

    # 6. 实测报告
    print('===== dedup_key 迁移完成 =====')
    print('cursor 备份：%s' % bak)
    print('id映射：%d → %d（净减 %d）' % (len(old_idmap), len(new_idmap), n_net))
    print('版本计数：%d → %d' % (len(old_vercount), len(new_vercount)))
    print('记录哈希：%d → %d' % (len(old_rechash), len(new_rechash)))
    print('tex key 字节不变：%d 条' % len(tex_new))
    print('序号：%d（保持不变）' % cursor['序号'])
    print('文件表：%d（未动）' % len(cursor.get('文件', {})))
    print('合并组：%d 组' % len(merges))
    for e in tombstone_entries:
        print('  退役 %s（旧 key=%r，版本计数=%s，记录哈希=%s）'
              '→ 合并入 %s（new_key=%r）'
              % (e['退役_id'], e['退役_old_key'], e['退役_版本计数'],
                 e['退役_记录哈希'], e['合并入_id'], e['合并入_new_key']))
    print('孤儿保留：%s' % '、'.join(ORPHANS))
    print('key口径版本=2；墓碑：%s' % TOMBSTONE)


if __name__ == '__main__':
    main()
