#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量化值/日期/模板识别回归护栏（入库长期资产，改抽取规则前后都必须跑）。

用法：python3 collector/tests/regression_guard.py
退出码 0 = 全部通过；非 0 = 有断言不符（末尾列出）。本文件只读 collect.py，
不写任何盘上产物（模板识别段用 tempfile 造临时文件，进程退出由系统回收）。

为什么入库：AGENTS.md 约束「新规则必须同步加回归护栏用例」要能长期生效，护栏
本体就不能放在 .gitignore 排除的 state/ 里，否则换台机器或清理运行时状态就丢了。

覆盖三层：
A. 已复验通过的真事实与全部过滤规则，一条都不许回退；
B. 两个已修盲区：IELTS/雅思 一类考试分数、英文技术名 + 小数版本号；
C. 豁免不得反过来放走噪声（时刻记号、章节号、点分串、文件名序号）。

维护纪律：往 CASES 里加用例时，期望值必须先用 `git show HEAD:collector/collect.py`
导出的旧版实测对照，确认差异确实来自本次改动——凭空写期望值会制造假 FAIL。
"""
import importlib.util
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('C', os.path.join(HERE, '..', 'collect.py'))
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)


def q(t):
    seen = set()
    out = []
    for m in C.QUANT_RE.finditer(t):
        r = C._quant_match(t, m, seen)
        if r:
            out.append(r[0])
            seen.add(r[1])
    return out


CASES = [
    # ---------------- B1. IELTS / 考试分数盲区 ----------------
    ('英语（CET-6：600，IELTS：7.5）；粤语（母语水平）；日语（N4）', ['600', '7.5']),
    ('英语(CET-6:600,IELTS:7.5)', ['600', '7.5']),
    ('英语（CET-6:600，IELTS:7.5）', ['600', '7.5']),
    ('CET-6：600', ['600']),
    ('CET-6:600', ['600']),
    ('IELTS：7.5', ['7.5']),
    ('IELTS:7.5', ['7.5']),
    ('IELTS 7.5', ['7.5']),
    ('雅思 7.5 / 托福 105', ['7.5', '105']),
    ('雅思：7.5，GRE：325', ['7.5', '325']),
    ('TOEFL iBT 105', ['105']),
    ('Duolingo 160', ['160']),
    ('六级 600 分', ['600分']),
    ('专八良好', []),
    ('Score 4.5 / Rating 3.2 / Average 2.8', ['4.5', '3.2', '2.8']),
    ('成绩： 4.5', ['4.5']),
    ('得分：12.5 / 15.0', ['12.5', '15.0']),
    ('完成率： 3 / 4.5', ['3', '4.5']),
    ('GPA：3.8', ['3.8']),
    ('GPA： 3.8', ['3.8']),

    # ---------------- B2. 英文技术名 + 小数版本号 ----------------
    ('后端 Django 5.2 + DRF + PostgreSQL 15（21 张表、7 个解耦模块、JWT + RBAC 三角色权限）',
     ['21张', '7个']),
    ('小节：Terminal-Bench 2.0 · 前 15 行（共 137 行） / Terminal-Bench 4.0 · 前 10 行'
     '（共 23 行，站点当前默认版本）',
     ['15行', '137行', '10行', '23行']),
    ('前端 Vue 3 + TypeScript + Element Plus + ECharts（22 个组件）', ['22个']),
    ('Material Design 3 与 Bootstrap 5.3', []),
    ('Python 3.14 与 Node 22.1', []),
    ('SWE-bench Verified 与 Terminal-Bench 2.0 榜单', []),
    # 必须保住：Availability: 5 是真事实，不是版本号
    ('Availability: 5 days/week for 6 months, immediate start GPA: 3.8/4.0',
     ['5', '3.8', '4.0']),
    # 必须保住：数字后紧贴 ASCII 字母的度量写法（s 不在 _UNIT 白名单）
    ('flush 默认 5s 实测 6.3s 到账', ['5', '6.3']),
    ('measured 6.3s and 5s baseline', ['6.3', '5']),
    # 必须保住的真事实（已复验）
    ('夏令营 GPA & 3.8 / 4.0 （专业排名第7）', ['3.8', '4.0', '7']),
    ('2023级本科生 GPA & 3.8 / 4.0', ['2023级', '3.8', '4.0']),
    ('某大学社会学院 · 社会学硕士 GPA & 3.8 / 4.0 辅修 & 统计学', ['3.8', '4.0']),
    ('GPA: 3.8/4.0，专业排名 5/30。', ['3.8', '4.0', '5', '30']),
    ('取得了98分（排名第10）的优异成绩', ['98分', '10']),
    ('覆盖 172,801家企业、1,414个项目', ['172,801家', '1,414个']),
    ('回收 1,000+份问卷，累计 60,000+字', ['1,000+份', '60,000+字']),
    ('8,974 个 16--60 岁创业者观测', ['8,974个', '16']),
    ('约 12.3天，下降 0.8%，估值17.80亿元，只剩 34.2%',
     ['12.3天', '0.8%', '17.80亿元', '34.2%']),
    ('净惩罚 −0.259 / −0.087（约 9pp）', ['0.259', '0.087', '9pp']),
    ('1.2 模块地图（8 课 = AI engineering loop 六段）', ['8']),
    ('按 unit 计费：1 unit = 1 trace / 1 observation / 1 score 之和', ['1']),
    ('覆盖「投稿->分配->AI 初审->人工复审->编辑决策->作者修改」7 步核心链路，'
     '端到端测试 100% 通过', ['7步', '100%']),
    ('120+ 篇核心论文 × 6 种审稿策略 × 400+ 个领域术语', ['120+篇', '6种', '400+个']),
    ('21家客户、500+款产品、120+项指标、97%+ 准确率', ['21家', '500+款', '120+项', '97%+']),
    ('5套模型、3份报告、14个案例', ['5套', '3份', '14个']),

    # ---------------- C. 既有过滤规则不许被新豁免放走 ----------------
    # 时刻记号 / 时区偏移（F2）
    ('2026-11-11T10:56:24+0800 生成', []),
    ('15:21:07+08:00 落盘', []),
    ('采集时间 2026-11-11T10:56:24+0800；结束 15:21:07+08:00', []),
    # 章节号（多级、带单位、引导词、§）
    ('1.1 选型建议 / 1.2 各档位', []),
    ('2.3.1 实现细节', []),
    ('4.1 题目与三层递进问题', []),
    ('2.2 项目密钥与交接包', []),
    ('小节：1.1 工作概述 / 1.2 DCF现金流测算（核心工作）', []),
    ('某集团实习生经历详细材料 小节：2.1 工作概述 / 2.2 三大职责', []),
    ('目录：3.2 数据清洗', []),
    ('附录：2.1 补充说明', []),
    ('定性锚点来自内部报告 §8.2', []),
    ('验证器分歧审计（§3.4 · 图 5）', []),
    ('.git history 抄答案（§7.5 · 论文转引）', []),
    ('参见 §7 的结论', []),
    # 点分串（IP）
    ('内网 观测 平台（192.0.2.1）应用方案', []),
    # 假日期（F1）
    ('2026-01-15 立项，2026.06 结项，2026年3月 交付', []),
    # 文件名序号 / 列表编号 / 算术
    ('resume_v3.tex 与 analysis2.do', []),
    ('（1）目标 （2）范围', []),
    ('0. 目标 / 1. 项目资产地图', []),
    ('映射 1 + 2 + 6 构成最小可行集成', []),
    ('Recall@5 目标 ≥ 85%；parsep=0pt；GPT-4 与 HTML5', ['85%']),
    ('幻灯片 1 / 幻灯片 2 / 幻灯片 3', []),
    ('2005 年 8 月出生', []),
    # R1-3 已改判：这条在早期是「遗留噪声按实际值 ['1','20'] 锁定、只盯不得
    # 变得更差」，加了联系方式豁免规则后必须为空——
    # 1XX-XXXX-XXXX 的 1 与 20XX 的 20 都是遮罩占位符前的碎片，不是事实数字。
    ('联系电话 (86)1XX-XXXX-XXXX；20XX 学年', []),
    # 句中真数值不因章节号规则被误杀
    ('把响应时间从 1.1 提升到 2.4', ['1.1', '2.4']),
    ('38/38 测试全绿', ['38']),

    # ---------------- D. R1-3 抽取去噪（本批新增） ----------------
    # D1. 联系方式碎片：姓名 + 区号电话 + 邮箱，各段碎片都不是量化值。
    ('Zhang San (+86) 138 0000 0000 zhangsan@example.com', []),
    ('Zhang San (+86)138-0000-0000 zhangsan@example.com', []),
    ('联系方式：138 1234 5678，邮箱 abc@example.com', []),
    ('Tel: +86 138 0000 0000 / Mobile 159 8888 7777', []),
    ('电话：010-1234 5678，Email: zhangsan@example.com', []),
    # D2. 零误杀锁定：同一段里的真值 5（Availability: 5 days/week）与
    #     3.8 / 4.0（GPA）在联系方式碎片之后，绝不能被整段豁免连带杀掉。
    ('Zhang San (+86) 138 0000 0000 zhangsan@example.com Guangzhou, China '
     'Availability: 5 days/week for 6 months, immediate start GPA: 3.8/4.0',
     ['5', '3.8', '4.0']),
    ('邮箱 abc@example.com；Availability: 5 days/week', ['5']),
    ('联系电话 138 1234 5678；完成 21家客户 调研，回收 1,000+份问卷',
     ['21家', '1,000+份']),
    # D3. 文件名派生序号：无扩展名时也要挡（既有 _FILE_EXT_RE 只认「数字 + 扩展名点」）。
    ('全部例句逐字取自示例简历3 原文', []),
    ('版式与语言的双基准均为 01_主简历/示例简历3.tex：全部例句逐字取自示例简历3 原文', []),
    ('详见附件报告2 与 论文3 的结论', []),
    ('模板文件（简历模板3）解决版式，例句取自示例简历3', []),
    # D4. 零误杀锁定：文档名词在数字**之后**（3份报告）、带单位、带小数、
    #     排名序数（第7 / 第10）一律不受文件名派生规则影响。
    ('完成 3份报告 与 5套模型、14个案例', ['3份', '5套', '14个']),
    ('夏令营 GPA & 3.8 / 4.0 （专业排名第7）', ['3.8', '4.0', '7']),
    ('取得了98分（排名第10）的优异成绩', ['98分', '10']),
    ('2023级本科生 GPA & 3.8 / 4.0', ['2023级', '3.8', '4.0']),
    ('回收 1,000+份问卷，累计 60,000+字', ['1,000+份', '60,000+字']),

    # ---------------- A. 日期抽取（F1） ----------------
]

DATE_BAD = ['MinerU_markdown_2068969700064067584',
            'MinerU_markdown_2069229975317086208',
            'MinerU_markdown_2069230004568154112',
            '2026-1-009_报告演示_v3_',
            'A3_Terminal-Bench_官方榜快照']
DATE_GOOD = [('2026-09-29_工具Workshop学习笔记与应用方案_v1', '2026-09-29'),
             ('2026-10-09_知识工程汇报演示页_v1', '2026-10-09'),
             ('2026-08-21-个人简历-润色版', '2026-08-21'),
             ('2026-02-30_不存在的日期', ''),
             ('2026-13-01_月份越界', ''),
             ('report_20260115_final', '2026-01-15')]

FAIL = []


def chk(name, ok, extra=''):
    print(('PASS  ' if ok else 'FAIL  ') + name + (('  | ' + extra) if extra and not ok else ''))
    if not ok:
        FAIL.append(name)


print('=== 量化值抽取用例（%d 条） ===' % len(CASES))
bad = 0
for t, exp in CASES:
    got = q(t)
    if got != exp:
        bad += 1
        print('FAIL  %r\n        期望 %s\n        实得 %s' % (t[:64], exp, got))
        FAIL.append('量化用例 %r' % t[:40])
print('量化值用例：%d/%d 通过' % (len(CASES) - bad, len(CASES)))

print('\n=== 日期抽取用例 ===')
for stem in DATE_BAD:
    got = C.date_from_name(stem)
    chk('假日期留空 %s' % stem[-24:], got == '', '实得 %r' % got)
for stem, exp in DATE_GOOD:
    got = C.date_from_name(stem)
    chk('真日期 %s → %r' % (stem[-28:], exp), got == exp, '实得 %r' % got)

print('\n=== 模板识别（F3） ===')
# is_template_file 要读正文数占位符密度，所以造临时文件跑真函数，
# 不能只看 _template_stem（它返回字符串主干，不是布尔，且只是第一道名字闸门）。
import tempfile
_TMPD = tempfile.mkdtemp(prefix='regress3_')
_TMPL_BODY = '\n'.join('【%s填写】XXXXXXXX 此处填 your_name example@placeholder' % ('待' * (i % 3))
                        for i in range(40))
_REAL_BODY = '\n'.join('完成了行业标杆研究报告 %d 份，覆盖 21 家客户，端到端测试 100%% 通过。'
                        % i for i in range(40))
_TPL_CASES = [
    ('简历模板-demo.tex', _TMPL_BODY, False, True),
    ('简历模板-demo.pdf', '', True, True),      # 二进制只认强特征，无需正文
    ('template_v2.tex', _TMPL_BODY, False, True),
    ('占位说明.md', _TMPL_BODY, False, True),
    ('demo进度记录.md', _REAL_BODY, False, False),   # 名字带 demo 但正文无占位符
    ('2026-09-24_LLM可观测性工具对比_v1.md', _REAL_BODY, False, False),
    ('2026-09-24_LLM可观测性工具对比_v1.md', _TMPL_BODY, False, False),  # 有占位符但名字不含模板词
]
for name, body, is_bin, exp in _TPL_CASES:
    p = os.path.join(_TMPD, name)
    with open(p, 'w', encoding='utf-8') as f:
        f.write(body)
    got = C.is_template_file(p, name, is_bin, C.Stats())
    chk('is_template_file(%s, bin=%s) == %s' % (name, is_bin, exp), got == exp, '实得 %r' % got)

print('\n=== 场景警示规则（配置驱动） ===')
_RULES = [{'场景含': '保研',
           '文本匹配': r'排名|名次|专业前|绩点|GPA|核心课程|课程成绩|加权均分|平均分|\brank',
           '警示': '排名仅限保研场景、禁止流入求职简历表述'}]
for t, exp in [('GPA: 3.8/4.0，专业排名 5/30。', True),
               ('核心课程：计量经济学（92）、时间序列分析（92）', True),
               ('工具语言：英语（CET-6：600，IELTS：7.5）；粤语（母语水平）', False),
               ('负责市场可比数据清洗、校验与数据库维护', False),
               ('bge-reranker 精排 + RRF 融合', False),
               ('课程成绩与加权均分见附件', True)]:
    chk('scene_warning(保研, %r) 命中 == %s' % (t[:32], exp),
        bool(C.scene_warning('保研版', t, _RULES)) == exp)
chk('非保研场景不打警示', C.scene_warning('求职-润色版', 'GPA: 3.8/4.0，专业排名 5/30。', _RULES) == '')
chk('无规则不打警示', C.scene_warning('保研版', '专业排名 5/30', []) == '')
chk('坏正则跳过', C.scene_warning('保研版', '专业排名 5/30', [{'场景含': '保研', '文本匹配': '(', '警示': 'x'}]) == '')

print('')
print('=== dedup_key / next_id / record_hash（路径口径分治锁定） ===')
# G2 / G5 在旧 dedup_key 口径（名称入 key）下必 FAIL——它们是本次根治的正向 / 负面
# 锁定，刻意如此设计。迁移到路径口径后应转 PASS。若将来这两条重新 FAIL，说明
# dedup_key 被改回旧口径、或证据定位被塞回 key，属真回归，不是护栏坏了。
import copy as _copy

# G1 口径分治：tex 走 分类|名称|路径；非 tex 走 分类|路径|单条
_g1_tex = C.dedup_key('简历', '某实体', ['/r/res.tex'])
_g1_md = C.dedup_key('项目经历', '某标题', ['/p/v5.md'])
chk('G1a tex 口径=分类|名称|路径', _g1_tex == '简历|某实体|/r/res.tex', _g1_tex)
chk('G1b 非 tex 口径=分类|路径|单条', _g1_md == '项目经历|/p/v5.md|单条', _g1_md)

# G2 改名不裂 key（旧代码必 FAIL，正向锁定）：同路径同分类、仅名称变 → key 不变
_g2a = C.dedup_key('项目经历', '示例项目报告（四模块·判断先行版）', ['/p/v5.md'])
_g2b = C.dedup_key('项目经历', '示例项目报告（四模块）', ['/p/v5.md'])
chk('G2 非 tex 改名不裂 key（根治目标）', _g2a == _g2b, '%r != %r' % (_g2a, _g2b))

# G3 同路径同分类合并、异分类不合并
_g3a = C.dedup_key('项目经历', '别名A', ['/p/x.md'])
_g3b = C.dedup_key('项目经历', '别名B', ['/p/x.md'])
_g3c = C.dedup_key('实习经历', '别名A', ['/p/x.md'])
chk('G3a 同分类同路径合并', _g3a == _g3b)
chk('G3b 异分类不合并', _g3a != _g3c)

# G4 tex/非 tex 分治 + 非 tex 一文件一条不变量（详见 collect.py EXTRACTORS 注释）
chk('G4a 非 tex 提取器注册齐全',
    all(n in C.EXTRACTORS for n in ('doc_auto', 'skill_registry', 'readme_oneliner')))
chk('G4b tex 提取器 resume_tex 在注册表', 'resume_tex' in C.EXTRACTORS)
_g4a = C.dedup_key('简历', '实体甲', ['/r/a.tex'])
_g4b = C.dedup_key('简历', '实体乙', ['/r/a.tex'])
chk('G4c tex 同路径异名称→key 相异（名称仍是判别位）', _g4a != _g4b)

# G5 负面锁定——证据定位（L数字 / 节「」/ 工作表 「」/ HTML 首个）不得入 key（旧代码必 FAIL）
_g5_base = C.dedup_key('项目经历', '成果标题', ['/p/y.md'])
_g5_variants = [C.dedup_key('项目经历', '成果标题 ' + _loc, ['/p/y.md'])
                for _loc in ('L12', '节「概述」', '工作表 「Sheet1」', 'HTML 首个表格')]
chk('G5 证据定位后缀不入 key（非 tex）',
    all(v == _g5_base for v in _g5_variants),
    '%r' % ([v for v in _g5_variants if v != _g5_base][:1]))

# G6 孤儿 key 不复活：模板类 key 再命中复用原 id、不涨序号
_c6 = {'id映射': {}, '序号': 0}
_k6 = C.dedup_key('简历', '简历模板-demo', ['/t/简历模板-demo.pdf'])
_i6a = C.next_id(_c6, _k6)
_i6b = C.next_id(_c6, _k6)
chk('G6 孤儿 key 复用不涨序号',
    _i6a == _i6b and _c6['序号'] == 1, 'id=%r/%r 序号=%d' % (_i6a, _i6b, _c6['序号']))

# G7 next_id 单调不复用
_c7 = {'id映射': {}, '序号': 0}
_7a = C.next_id(_c7, 'k1')
_7b = C.next_id(_c7, 'k2')
_7c = C.next_id(_c7, 'k1')
chk('G7a 新 key 单调递增', _7a == 'PI-0001' and _7b == 'PI-0002', '%r %r' % (_7a, _7b))
chk('G7b 旧 key 复用不新分配', _7c == 'PI-0001' and _c7['序号'] == 2,
    '%r 序号=%d' % (_7c, _c7['序号']))

# G8 record_hash 排除字段锁定
_rec8 = {'id': 'PI-0001', '版本': 1, '采集时间': 't0', '扫描模式': 'full',
         '记录哈希': 'x', '名称': '标题', '成果': '内容', '分类': '项目经历',
         '证据路径': ['/p/z.md'], '状态': '待核验'}
_h8 = C.record_hash(_rec8)
for _f in ('id', '版本', '采集时间', '扫描模式', '记录哈希'):
    _r = _copy.deepcopy(_rec8)
    _r[_f] = 'CHANGED'
    chk('G8 排除字段 %s 改动不影响哈希' % _f, C.record_hash(_r) == _h8)
for _f in ('名称', '成果', '状态'):
    _r = _copy.deepcopy(_rec8)
    _r[_f] = 'CHANGED'
    chk('G8 内容字段 %s 改动必变哈希' % _f, C.record_hash(_r) != _h8)

# G9 端到端（合成 cursor，不读盘）：改名 → 同 id、版本+1，而非裂新 id
_c9 = {'id映射': {}, '序号': 0, '记录哈希': {}, '版本计数': {}}
_p9 = '/tmp/g9_报告.md'
_kA = C.dedup_key('项目经历', '报告标题A', [_p9])
_idA = C.next_id(_c9, _kA)
_recA = {'id': _idA, '名称': '报告标题A', '成果': '内容', '分类': '项目经历',
         '证据路径': [_p9], '版本': 1}
_hA = C.record_hash(_recA)
_c9['版本计数'][_kA] = 1
_c9['记录哈希'][_kA] = _hA
_kB = C.dedup_key('项目经历', '报告标题B', [_p9])
_idB = C.next_id(_c9, _kB)
chk('G9a 改名后同 id（不裂）', _idA == _idB, '%r vs %r' % (_idA, _idB))
chk('G9b 改名后 key 不变', _kA == _kB)
_recB = _copy.deepcopy(_recA)
_recB['名称'] = '报告标题B'
_recB['id'] = _idB
_hB = C.record_hash(_recB)
chk('G9c 改名后哈希变（触发版本+1）', _hB != _c9['记录哈希'].get(_kB))

# G10 迁移脚本 new_key() 纯函数 fixture（migrate_dedup_key.py，import 不触发迁移）
_mspec = importlib.util.spec_from_file_location(
    'M', os.path.join(HERE, '..', 'migrate_dedup_key.py'))
_M = importlib.util.module_from_spec(_mspec)
_mspec.loader.exec_module(_M)
chk('G10a new_key md 3段→末段单条',
    _M.new_key('项目经历|标题|/p/v5.md') == '项目经历|/p/v5.md|单条',
    _M.new_key('项目经历|标题|/p/v5.md'))
chk('G10b new_key tex→原样', _M.new_key('简历|实体|/r/a.tex') == '简历|实体|/r/a.tex')
_g10_exit = False
try:
    _M.new_key('只有|两段')
except SystemExit:
    _g10_exit = True
chk('G10c new_key 非 3 段→SystemExit', _g10_exit)
chk('G10d is_migrated(key口径版本=2)→幂等跳过', _M.is_migrated({'key口径版本': 2}) is True)
chk('G10e is_migrated(空)→False', _M.is_migrated({}) is False)
for _cls, _nm, _pa in [('项目经历', '标题T', '/p/v5.md'), ('简历', '实体E', '/r/a.tex')]:
    _runtime = C.dedup_key(_cls, _nm, [_pa])
    _migrated = _M.new_key('%s|%s|%s' % (_cls, _nm, _pa))
    chk('G10f dedup_key 与 new_key 口径一致 %s' % _pa,
        _runtime == _migrated, '%r vs %r' % (_runtime, _migrated))

# --------------------------------------------------------------------------
# L-3：_auth_path_from_key（dedup_key 逆操作）——--incremental 授权重扫的路径解析。
# 约定一个键只含一个 '/'-前缀段；名称含 '|' 会切出多段致授权错位 / 静默失效，故断言
# 段数==1，不等则告警并返回 None（跳过该键，显式失败优于静默失效）。合成 fixture，
# 不耦合 gitignored 的 live cursor.json（移动靶）。
# --------------------------------------------------------------------------
print('\n=== L-3 授权路径解析（_auth_path_from_key 段数断言） ===')
chk('L-3a 锚点键→末段路径',
    C._auth_path_from_key('锚点|resumeentry-a1|/r/resume_v3.tex') == '/r/resume_v3.tex')
chk('L-3b 名称键(tex)→末段路径',
    C._auth_path_from_key('简历|某实体|/r/res.tex') == '/r/res.tex')
chk('L-3c 单条键→中段路径',
    C._auth_path_from_key('教育背景|/z/diploma.pdf|单条') == '/z/diploma.pdf')
chk('L-3d 名称含|切出多段→None(跳过+告警)',
    C._auth_path_from_key('实习经历|某|/evil/司|/y/report.md') is None)
chk('L-3e 零路径段→None(跳过+告警)',
    C._auth_path_from_key('实习经历|某公司|单条') is None)

# --------------------------------------------------------------------------
# G11 系列：tex 简历锚点根治（口径 C）正向锁定。
# G11o / G11p / G11d 在旧解析器（parse_resume_tex 无 pending_anchor、dedup_key 无
# 第 4 参、record_hash 未排除锚点）下必 FAIL——它们是本次根治的正向锁定，刻意如此
# 设计。改完 collect.py 后应全部转 PASS；将来若重新 FAIL，说明锚点解析被回退，属真
# 回归。fixture 全部自建合成 tex，不依赖真实简历文件（源文件是移动靶）。
# --------------------------------------------------------------------------
print('\n=== G11 tex 简历锚点根治（口径 C）正向锁定 ===')
_G11_SENTINEL = '<无锚点字段>'


def _g11_anchor(tex):
    """解析合成 tex，返回首个条目的锚点字段（旧解析器无此字段则返回哨兵）。"""
    es = C.parse_resume_tex(tex)
    return es[0].get('锚点', _G11_SENTINEL) if es else _G11_SENTINEL


# G11o 锚点解析：% @pi 整行注释绑定到紧邻其下的 \resumeentry 条目
_G11O_TEX = '\n'.join([
    '\\begin{document}',
    '% ========== 实习经历 ==========',
    '\\resumesection{实习经历}',
    '% @pi: PI-0002',
    '\\resumeentry{某公司}{2026 年 7 月 -- 至今}',
    '\\begin{resumelist}',
    '  \\item 独立完成 3 份行业标杆研究报告，覆盖 21 家客户。',
    '\\end{resumelist}',
    '\\end{document}',
])
chk('G11o 锚点解析：% @pi 绑定到紧邻 \\resumeentry 条目',
    _g11_anchor(_G11O_TEX) == 'PI-0002', '实得 %r' % _g11_anchor(_G11O_TEX))

# G11p pending_anchor 暂存：锚点绑定到无 \resumeentry 的技能散段隐式条目
_G11P_TEX = '\n'.join([
    '\\begin{document}',
    '% ========== 技能 ==========',
    '\\resumesection{技能}',
    '\\begin{resumelist}',
    '% @pi: PI-0012',
    '  \\item 熟练运用 Python、R 与 Stata，擅长空间分析与数据可视化。',
    '\\end{resumelist}',
    '\\end{document}',
])
chk('G11p pending_anchor 暂存：锚点绑定到散段隐式条目',
    _g11_anchor(_G11P_TEX) == 'PI-0012', '实得 %r' % _g11_anchor(_G11P_TEX))

# G11p2 跨节清暂存：锚点后紧跟节标记（banner/section）则清空，不泄漏到下一节首条
_G11P2_TEX = '\n'.join([
    '\\begin{document}',
    '% @pi: PI-0009',
    '% ========== 项目经历 ==========',
    '\\resumesection{项目经历}',
    '\\resumeentry{某项目}{2025 年}',
    '\\begin{resumelist}',
    '  \\item 交付 9 页响应式官网。',
    '\\end{resumelist}',
    '\\end{document}',
])
chk('G11p2 跨节清暂存：锚点被节标记隔断则不绑定（防泄漏）',
    _g11_anchor(_G11P2_TEX) == '', '实得 %r' % _g11_anchor(_G11P2_TEX))

# G11d dedup_key 锚点口径（第 4 参）：有锚点 → 锚点|PI|路径（3 段，不含分类/名称）
try:
    _g11d = C.dedup_key('实习经历', '某公司', ['/r/a.tex'], 'PI-0002')
except TypeError:
    _g11d = '<第4参不被接受>'
chk('G11d dedup_key 锚点口径=锚点|PI|路径', _g11d == '锚点|PI-0002|/r/a.tex',
    '实得 %r' % _g11d)
# G11d2 向后兼容：无锚点（第 4 参默认空）时 tex 仍走旧 fallback，一字不改（新旧都 PASS）
_g11d2 = C.dedup_key('实习经历', '某公司', ['/r/a.tex'])
chk('G11d2 无锚点 tex 退回旧 key（向后兼容）',
    _g11d2 == '实习经历|某公司|/r/a.tex', '实得 %r' % _g11d2)

# G11h record_hash 排除锚点：锚点是身份不是内容，增删锚点字段不改哈希
_rec11 = {'id': 'PI-0002', '版本': 2, '名称': '某公司', '成果': '内容',
          '分类': '实习经历', '证据路径': ['/r/a.tex'], '状态': '待核验'}
_h11_no = C.record_hash(_rec11)
_rec11b = _copy.deepcopy(_rec11)
_rec11b['锚点'] = 'PI-0002'
chk('G11h record_hash 排除锚点（带/不带锚点同哈希）',
    C.record_hash(_rec11b) == _h11_no,
    '%r vs %r' % (C.record_hash(_rec11b), _h11_no))

# G11f 锚点正则：严格版收 % @pi: PI-0002（中英冒号/大小写/空格），宽松版捕获格式错误
_AR = getattr(C, '_ANCHOR_RE', None)
_AL = getattr(C, '_ANCHOR_LOOSE', None)
if _AR is None or _AL is None:
    chk('G11f _ANCHOR_RE/_ANCHOR_LOOSE 已定义', False, '缺失')
else:
    chk('G11f1 严格锚点中英冒号+大小写+空格都收',
        bool(_AR.match('% @pi: PI-0002')) and bool(_AR.match('%@pi：pi-0007'))
        and bool(_AR.match('  %  @PI :  PI-0035  ')))
    chk('G11f2 严格锚点拒收错位（非 4 位/缺冒号/带尾注）',
        (not _AR.match('% @pi: PI-002')) and (not _AR.match('% @pi PI-0002'))
        and (not _AR.match('% @pi: PI-0002 尾注')))
    chk('G11f3 宽松命中但严格不符 → 可判定格式错误',
        bool(_AL.match('% @pi PI-0002')) and not _AR.match('% @pi PI-0002'))

# G11s 锚点序列化：带锚点的 rec 经 ordered 重建后仍保留锚点字段
# 模拟 run() 的 ordered 构建逻辑（BASE_FIELDS + SCENE_FIELDS + 锚点条件 + 记录哈希）
_rec11s = {'id': 'PI-0002', '版本': 3, '分类': '实习经历', '名称': '某公司',
           '时间': '', '角色': '', '成果': '内容', '量化值': [],
           '无源量化值': None, '来源盘': 'demo', '来源工作区': '示例工作区',
           '源清单': '个人简历', '证据路径': ['/r/a.tex'], '证据定位': '节L1-L5',
           '采集时间': '2026-01-01T00:00:00+08:00', '采集方式': 'auto',
           '扫描模式': 'incremental', '状态': '待核验', '主源冲突组': None,
           '锚点': 'PI-0002', '记录哈希': 'abc123'}
_ordered11s = {}
for _k in C.BASE_FIELDS:
    _ordered11s[_k] = _rec11s.get(_k)
for _k in C.SCENE_FIELDS:
    if _rec11s.get(_k):
        _ordered11s[_k] = _rec11s[_k]
if _rec11s.get('锚点'):
    _ordered11s['锚点'] = _rec11s['锚点']
_ordered11s['记录哈希'] = _rec11s['记录哈希']
chk('G11s 锚点序列化：带锚点 rec 经 ordered 重建后保留锚点字段',
    '锚点' in _ordered11s and _ordered11s['锚点'] == 'PI-0002',
    'ordered keys=%r' % sorted(_ordered11s.keys()))

# ===================== V 系列：T20 待核验转正闸门 =====================
# 锁住 run() 内部转正闸门：授权命中转正、内容变自动降级(pop)、盲区/数字/OCR/教育
# 类别零转正（无授权即不转正）、次轮幂等、write_queue 已核验分区 + 人工裁决区不变。
# 闸门内联在 run() 里，这里用忠实复刻 _gate() 做纯函数级验证；V-G0 源码锁保证复刻
# 与 collect.py 实际闸门段同步（改闸门必改此处，否则 V-G0 FAIL 提醒）。

def _mkrec(**over):
    r = {'id': 'PI-9001', '版本': 1, '分类': '项目经历', '名称': '某项目',
         '时间': '2026-01', '角色': '开发', '成果': '完成模块',
         '量化值': [], '无源量化值': None, '来源盘': 'demo', '来源工作区': '示例工作区',
         '源清单': '项目', '证据路径': ['/r/p/v5.md'], '证据定位': '节L1-L5',
         '采集时间': '2026-01-01T00:00:00+08:00', '采集方式': 'auto',
         '扫描模式': 'incremental', '状态': C.STATUS_PENDING, '主源冲突组': None}
    r.update(over)
    return r


def _cursor():
    return {'记录哈希': {}, '已核验': {}, 'id映射': {}, '版本计数': {}, '序号': 0}


def _gate(rec, key, cursor):
    """忠实复刻 collect.py run() T20 闸门段（rec['id'] 赋值后 → pop 授权）。
    返回 (rec, 闸门命中, 是否追加)。与源码同步维护。"""
    闸门命中 = False
    if rec.get('状态') == C.STATUS_PENDING:
        h0 = C.record_hash(rec)
        if cursor.get('已核验', {}).get(key) == h0:
            rec['状态'] = C.STATUS_VERIFIED
            闸门命中 = True
    h = C.record_hash(rec)
    rec['记录哈希'] = h
    是否追加 = True
    if cursor['记录哈希'].get(key) == h:
        是否追加 = False                                   # 未变 → skip（幂等核心）
    else:
        cursor['记录哈希'][key] = h
        if not 闸门命中:
            cursor.get('已核验', {}).pop(key, None)         # 内容变 → 降级
    return rec, 闸门命中, 是否追加


print('\n=== V 系列：T20 转正闸门（%d 断言）===' % 11)

# V-G0 源码锁：闸门关键行 + PRESCREEN_BLIND_EXT 仍在 collect.py（防复刻与实现脱节）
with open(os.path.join(HERE, '..', 'collect.py'), encoding='utf-8') as _f:
    _src = _f.read()
chk('V-G0 闸门源码锁：转正闸门 + pop 降级 + PRESCREEN_BLIND_EXT 均在 collect.py',
    "if rec.get('状态') == STATUS_PENDING:" in _src
    and "cursor.get('已核验', {}).get(key) == h0" in _src
    and "cursor.get('已核验', {}).pop(key, None)" in _src
    and 'PRESCREEN_BLIND_EXT' in _src)

# V-G1 授权命中转正：cursor 已核验[key]==待核验态哈希 → 升已核验 + 追加新行 + 授权保留
_k1 = C.dedup_key('项目经历', '某项目', ['/r/p/v5.md'])
_r1 = _mkrec()
_c1 = _cursor()
_c1['已核验'][_k1] = C.record_hash(_r1)                    # 授权 = 待核验态哈希
_r1, _hit1, _add1 = _gate(_r1, _k1, _c1)
chk('V-G1 授权命中 → 转正已核验 + 追加新行 + 授权保留不 pop',
    _r1['状态'] == C.STATUS_VERIFIED and _hit1 and _add1
    and _c1['已核验'].get(_k1) is not None,
    '状态=%r 命中=%r 追加=%r' % (_r1['状态'], _hit1, _add1))

# V-G2 内容变自动降级：授权哈希对不上 → 不转正 + 追加新行 + pop 授权
_k2 = C.dedup_key('项目经历', '某项目', ['/r/p/v5.md'])
_r2 = _mkrec(成果='完成模块（改版）')                       # 内容变了
_c2 = _cursor()
_c2['已核验'][_k2] = 'stale_hash_000'                      # 旧授权，与当前内容不符
_r2, _hit2, _add2 = _gate(_r2, _k2, _c2)
chk('V-G2 内容变 → 不转正(仍待核验) + 追加新行 + pop 授权降级',
    _r2['状态'] == C.STATUS_PENDING and not _hit2 and _add2
    and _k2 not in _c2['已核验'],
    '状态=%r 命中=%r 授权残留=%r' % (_r2['状态'], _hit2, _k2 in _c2['已核验']))

# V-G3 盲区扩展名不转正：集合定义正确 + 无授权 .xlsx 条目闸门不转正
chk('V-G3a 盲区扩展名集合 == {.xlsx,.docx}',
    C.PRESCREEN_BLIND_EXT == {'.xlsx', '.docx'}, repr(C.PRESCREEN_BLIND_EXT))
_r3 = _mkrec(证据路径=['/r/data/table.xlsx'], 名称='数据表')
_k3 = C.dedup_key('项目经历', '数据表', ['/r/data/table.xlsx'])
_c3 = _cursor()                                            # 盲区格式：prescreen 不授权
_r3, _hit3, _ = _gate(_r3, _k3, _c3)
chk('V-G3b .xlsx 无授权 → 闸门不转正(仍待核验)',
    _r3['状态'] == C.STATUS_PENDING and not _hit3)

# V-G4 含数字不转正：量化值非空无授权 → 闸门不转正
_r4 = _mkrec(量化值=[{'值': '30%', '已溯源': True}])
_k4 = C.dedup_key('项目经历', '某项目', ['/r/p/v5.md'])
_c4 = _cursor()
_r4, _hit4, _ = _gate(_r4, _k4, _c4)
chk('V-G4 含数字(量化值非空)无授权 → 闸门不转正(仍待核验)',
    _r4['状态'] == C.STATUS_PENDING and not _hit4)

# V-G5 需OCR不转正：闸门只处理待核验；需OCR 即便被误授权也不转正
_r5 = _mkrec(状态=C.STATUS_NEEDOCR)
_k5 = C.dedup_key('项目经历', '某项目', ['/r/p/v5.md'])
_c5 = _cursor()
_c5['已核验'][_k5] = C.record_hash(_r5)                    # 即便误授权
_r5, _hit5, _ = _gate(_r5, _k5, _c5)
chk('V-G5 需OCR状态 → 闸门只认待核验，即便被授权也不转正',
    _r5['状态'] == C.STATUS_NEEDOCR and not _hit5)

# V-G6 教育背景/获奖荣誉零转正：两类无授权 → 均不转正
_ok6 = True
for _cat in ('教育背景', '获奖荣誉'):
    _r6 = _mkrec(分类=_cat, 名称='%s条目' % _cat)
    _k6 = C.dedup_key(_cat, '%s条目' % _cat, ['/r/p/v5.md'])
    _r6, _hit6, _ = _gate(_r6, _k6, _cursor())
    if _r6['状态'] != C.STATUS_PENDING or _hit6:
        _ok6 = False
chk('V-G6 教育背景/获奖荣誉无授权 → 零转正', _ok6)

# V-G7 次轮幂等：首轮转正追加新行；次轮同内容授权仍命中但记录哈希已存 → 不再追加
_k7 = C.dedup_key('项目经历', '某项目', ['/r/p/v5.md'])
_c7 = _cursor()
_c7['已核验'][_k7] = C.record_hash(_mkrec())               # 授权
_, _, _add7a = _gate(_mkrec(), _k7, _c7)                   # 首轮：转正 + 追加
_, _hit7b, _add7b = _gate(_mkrec(), _k7, _c7)              # 次轮：同内容
chk('V-G7 幂等：首轮追加新行、次轮授权仍命中但不再追加(记录哈希已存)',
    _add7a and _hit7b and not _add7b,
    '首轮追加=%r 次轮命中=%r 次轮追加=%r' % (_add7a, _hit7b, _add7b))

# V-G8 write_queue 已核验分区存在 + 人工裁决区逐字节保留
import tempfile as _tf
_qd = _tf.mkdtemp()
_old_q = os.path.join(_qd, 'queue.md')
_judge = (C.JUDGE_MARK + '（只追加，生成器不覆盖本区）\n\n'
          '- [2026-11-12] id=PI-0001 裁决：属实 → 状态改为 已核验\n')
with open(_old_q, 'w', encoding='utf-8') as f:
    f.write('# 待核验队列\n\n旧内容\n\n' + _judge)
_saved_qp = C.QUEUE_PATH
C.QUEUE_PATH = _old_q
try:
    C.write_queue([_mkrec(id='PI-8001', 状态=C.STATUS_VERIFIED, 名称='已核验项'),
                   _mkrec(id='PI-8002', 状态=C.STATUS_PENDING, 名称='待核验项')])
    with open(_old_q, encoding='utf-8') as f:
        _q_out = f.read()
finally:
    C.QUEUE_PATH = _saved_qp
chk('V-G8a write_queue 生成「已核验」第五节 + 语义边界文案 + 已核验条目在列',
    '## 五、已核验' in _q_out and '不等于内容属实' in _q_out and 'PI-8001' in _q_out)
chk('V-G8b write_queue 逐字节保留既有人工裁决区',
    _judge.strip() in _q_out and '裁决：属实' in _q_out)

print('\n===== %d 项不符 =====' % len(FAIL))
for f in FAIL:
    print(' - ' + f)
sys.exit(1 if FAIL else 0)
