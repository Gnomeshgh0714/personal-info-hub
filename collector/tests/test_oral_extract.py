#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""口述线索抽取测试（hook_stop.py 的 extract_oral_clues / _extract_clues_from_text）。

用法：python3 collector/tests/test_oral_extract.py
退出码 0 = 全部通过；1 = 有断言不符。
纯标准库，不写任何盘上产物（用 tempfile 造临时 transcript，进程退出回收）。

隔离断言三条（七条红线的测试级锁定）：
  1. oral_clues.jsonl 条目不得出现在 personal_info.jsonl
  2. extract_session_facts / extract_oral_clues 不得调用任何写 personal_info.jsonl 的函数
     （静态检查 hook_stop.py 源码）
  3. 线索对象 status 恒等于 '待核验/口述线索'

本测试独立于 regression_guard.py，避免阶段2 编辑冲突。
"""
import importlib.util
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# 动态加载 hook_stop.py
spec = importlib.util.spec_from_file_location(
    'H', os.path.join(HERE, '..', 'hook_stop.py'))
H = importlib.util.module_from_spec(spec)
spec.loader.exec_module(H)

FAIL = []


def chk(name, ok, extra=''):
    """对齐 regression_guard 风格的断言输出。"""
    print(('PASS  ' if ok else 'FAIL  ') + name + (('  | ' + extra) if extra and not ok else ''))
    if not ok:
        FAIL.append(name)


# ==========================================================================
# ORAL_CASES：不应抽取 vs 应抽取
# ==========================================================================
# 格式：(用户文本, 期望命中模式列表, 期望 has_number)
#   命中模式列表为空 [] 表示不应抽取
#   命中模式列表非空表示应抽取，每项为模式名
#   has_number=None 表示不检查该字段

ORAL_CASES = [
    # ---- 不应抽取（噪声/指令/问句/否定/纯英文/短文本） ----
    ('做', [], None),
    ('继续', [], None),
    ('好的', [], None),
    ('继续完成任务', [], None),
    ('帮我写一个函数', [], None),
    ('请帮我改一下这个文件', [], None),
    ('帮我生成一份报告', [], None),
    ('帮我搜索相关论文', [], None),
    ('这个项目用了什么技术栈？', [], None),
    ('我的GPA是多少来着？', [], None),
    ('不要改这个文件', [], None),
    ('别动那个配置', [], None),
    ('没有别的意思', [], None),
    ('git add .', [], None),
    ('python3 collect.py --full', [], None),
    ('npm install && npm run dev', [], None),
    ('这才像话', [], None),
    ('把html放到桌面', [], None),
    ('看看输出对不对', [], None),
    ('检查一下格式', [], None),

    # ---- 应抽取（口述事实，命中 Layer 3 模式） ----
    ('我大三暑假在某公司做了量化研究', ['我在X做了Y'], False),
    ('我曾在某公司参与人工智能平台开发', ['我在X做了Y'], False),
    ('我拿了国家奖学金', ['我拿了X奖'], False),
    ('我获得了校级优秀毕业生荣誉', ['我拿了X奖'], False),
    ('我获得了特别奖学金', ['我拿了X奖'], False),   # M-2: '别' 后向约束，「特别」不再误杀
    ('我的GPA是3.8', ['我的GPA是X'], True),
    ('我的绩点为3.9', ['我的GPA是X'], True),
    ('这个项目上线了三个月', ['项目X了Y'], True),
    ('我的论文完成了初稿', ['项目X了Y'], False),
    ('我2023年从某大学毕业', ['我X年从Y毕业'], True),
    ('我2025年在某大学入学', ['我X年从Y毕业'], True),

    # ---- 边界：含数字必标 has_number=True ----
    ('我实习时带了5个人的团队', [], None),
    ('我在示例咨询公司实习了3个月负责数据清洗', ['我在X做了Y'], True),
    ('我的排名是第7', ['我的GPA是X'], True),
]


# ==========================================================================
# 测试执行
# ==========================================================================

print('=== 口述线索抽取用例（%d 条） ===' % len(ORAL_CASES))

bad = 0
for text, expect_patterns, expect_has_number in ORAL_CASES:
    clues = H._extract_clues_from_text(
        text, 'test-session', '/tmp/test.jsonl', 1, '2026-01-01T00:00:00')

    got_patterns = sorted(set(c['matched_pattern'] for c in clues))
    expected_sorted = sorted(set(expect_patterns))

    if got_patterns != expected_sorted:
        bad += 1
        print('FAIL  %r' % text[:50])
        print('      期望模式 %s' % expected_sorted)
        print('      实得模式 %s' % got_patterns)
        FAIL.append('抽取用例 %r' % text[:30])
    elif expect_has_number is not None and clues:
        got_number = any(c['has_number'] for c in clues)
        if got_number != expect_has_number:
            bad += 1
            print('FAIL  %r  has_number' % text[:50])
            print('      期望 has_number=%s' % expect_has_number)
            print('      实得 has_number=%s' % got_number)
            FAIL.append('has_number 用例 %r' % text[:30])

print('抽取用例：%d/%d 通过' % (len(ORAL_CASES) - bad, len(ORAL_CASES)))

# ==========================================================================
# 隔离断言 1：线索对象 status 恒等于 '待核验/口述线索'
# ==========================================================================
print('\n=== 隔离断言：status 恒定 ===')

_status_clues = H._extract_clues_from_text(
    '我在某公司做了量化研究', 'test', '/t.jsonl', 1, '')
for c in _status_clues:
    chk('status 恒等「待核验/口述线索」',
        c['status'] == '待核验/口述线索',
        '实得 %r' % c.get('status'))

_multi_clues = H._extract_clues_from_text(
    '我2023年从某大学毕业，我的GPA是3.8', 'test', '/t.jsonl', 1, '')
for c in _multi_clues:
    chk('多模式命中 status 恒等',
        c['status'] == '待核验/口述线索',
        '实得 %r' % c.get('status'))

# ==========================================================================
# 隔离断言 2：extract 函数不得调用写 personal_info.jsonl 的函数
# ==========================================================================
print('\n=== 隔离断言：不写 personal_info.jsonl ===')

_hook_source_path = os.path.join(HERE, '..', 'hook_stop.py')
with open(_hook_source_path, 'r', encoding='utf-8') as f:
    _hook_source = f.read()

# 检查源码中不存在写 personal_info.jsonl 的代码路径
# （允许在注释/docstring 中提及说明）
_code_lines = []
_in_docstring = False
for line in _hook_source.split('\n'):
    stripped = line.strip()
    if stripped.startswith('#'):
        continue
    if '"""' in stripped:
        _in_docstring = not _in_docstring
        continue
    if _in_docstring:
        continue
    _code_lines.append(line)

_code_text = '\n'.join(_code_lines)
_pi_write_patterns = [
    r'open\(.*personal_info.*[\'\"]w',
    r'open\(.*personal_info.*[\'\"]a',
    r'PERSONAL_INFO.*PATH',
    r'personal_info\.jsonl.*write',
]
for pat in _pi_write_patterns:
    found = re.search(pat, _code_text)
    chk('源码不含写 personal_info 模式: %s' % pat[:30],
        found is None,
        '命中 %r' % (found.group() if found else ''))

chk('ORAL_CLUES_PATH 指向 oral_clues.jsonl',
    'oral_clues.jsonl' in H.ORAL_CLUES_PATH,
    H.ORAL_CLUES_PATH)
chk('ORAL_CLUES_PATH 不含 personal_info',
    'personal_info' not in H.ORAL_CLUES_PATH,
    H.ORAL_CLUES_PATH)

# ==========================================================================
# 隔离断言 3：端到端测试（用临时 transcript 跑 extract_oral_clues）
# ==========================================================================
print('\n=== 端到端抽取测试（临时 transcript） ===')

_tmpdir = tempfile.mkdtemp(prefix='oral_test_')
_tmp_transcript = os.path.join(_tmpdir, 'transcript.jsonl')

# 构造测试 transcript
_test_lines = [
    # user raw-string：应被抽取
    {'type': 'user', 'message': {'role': 'user', 'content': '我大三暑假在某公司做了量化研究'},
     'timestamp': '2026-11-13T10:00:00', 'sessionId': 'test-e2e'},
    # assistant：绝不抽取
    {'type': 'assistant', 'message': {'role': 'assistant', 'content': '我在某公司做了客户经理'},
     'timestamp': '2026-11-13T10:00:01'},
    # user tool_result（content 是 list）：绝不抽取
    {'type': 'user', 'message': {'role': 'user', 'content': [
        {'type': 'tool_result', 'content': '我拿了国家奖学金'}]},
     'timestamp': '2026-11-13T10:00:02'},
    # user raw-string 噪声：不抽取
    {'type': 'user', 'message': {'role': 'user', 'content': '帮我写一个函数'},
     'timestamp': '2026-11-13T10:00:03'},
    # user raw-string：应被抽取（含数字）
    {'type': 'user', 'message': {'role': 'user', 'content': '我的GPA是3.8'},
     'timestamp': '2026-11-13T10:00:04', 'sessionId': 'test-e2e'},
    # progress 类型：忽略
    {'type': 'progress', 'message': {'role': 'system', 'content': '我在某公司实习'},
     'timestamp': '2026-11-13T10:00:05'},
]

with open(_tmp_transcript, 'w', encoding='utf-8') as f:
    for obj in _test_lines:
        f.write(json.dumps(obj, ensure_ascii=False) + '\n')

# 临时替换 ORAL_CLUES_PATH 避免污染真实状态
_orig_clues_path = H.ORAL_CLUES_PATH
_tmp_clues = os.path.join(_tmpdir, 'oral_clues.jsonl')
H.ORAL_CLUES_PATH = _tmp_clues

clues_written, error = H.extract_oral_clues(_tmp_transcript, 'test-e2e')
chk('端到端无报错', error is None, str(error))
chk('端到端抽取 2 条线索（第1行+第5行）', clues_written == 2,
    '实得 %d' % clues_written)

# 验证写入的线索内容
if os.path.isfile(_tmp_clues):
    with open(_tmp_clues, 'r', encoding='utf-8') as f:
        written = [json.loads(ln) for ln in f if ln.strip()]
    chk('写入条数 == 2', len(written) == 2, '实得 %d' % len(written))
    if written:
        chk('第1条 line_no == 1', written[0]['line_no'] == 1,
            str(written[0].get('line_no')))
        chk('第1条 source == session_oral', written[0]['source'] == 'session_oral')
        chk('第1条 status 恒等', written[0]['status'] == '待核验/口述线索')
        chk('第1条 has_number == False', written[0]['has_number'] is False)
        last = written[-1]
        chk('末条 has_number == True', last['has_number'] is True,
            str(last.get('has_number')))
        chk('末条 status 恒等', last['status'] == '待核验/口述线索')
        chk('末条 raw_text 含 GPA', 'GPA' in last['raw_text'])
else:
    chk('oral_clues.jsonl 已创建', False, '文件不存在')

# 幂等测试：再跑一次，不应重复写入
clues_written_2, error_2 = H.extract_oral_clues(_tmp_transcript, 'test-e2e')
chk('幂等：第二次抽取 0 条', clues_written_2 == 0,
    '实得 %d' % clues_written_2)

# 还原路径
H.ORAL_CLUES_PATH = _orig_clues_path

# ==========================================================================
# 隔离断言 4：oral_clues 条目不得出现在 personal_info.jsonl
# ==========================================================================
print('\n=== 隔离断言：oral_clues 不入 personal_info.jsonl ===')

_pi_path = os.path.join(os.path.dirname(HERE), '..', '中台数据', 'personal_info.jsonl')
if os.path.isfile(_pi_path) and os.path.isfile(_tmp_clues):
    with open(_tmp_clues, 'r', encoding='utf-8') as f:
        clue_texts = set()
        for ln in f:
            if ln.strip():
                obj = json.loads(ln)
                clue_texts.add(obj.get('raw_text', ''))
    with open(_pi_path, 'r', encoding='utf-8') as f:
        pi_content = f.read()
    overlap = [t for t in clue_texts if t and t in pi_content]
    chk('oral_clues 文本不出现在 personal_info.jsonl',
        len(overlap) == 0,
        '重叠 %d 条: %s' % (len(overlap), overlap[:2]))
else:
    chk('oral_clues 文本不出现在 personal_info.jsonl（jsonl 不存在，天然隔离）', True)

# ==========================================================================
# transcript_summary 修正验证
# ==========================================================================
print('\n=== transcript_summary 修正验证 ===')

summary = H.transcript_summary(_tmp_transcript)
chk('summary 有 user_raw_string_lines 字段',
    'user_raw_string_lines' in summary, str(summary.keys()))
# 6 行 JSONL，其中 type=user 且 content 是 str 的有 3 行（第1/4/5行）
chk('user_raw_string_lines == 3',
    summary.get('user_raw_string_lines') == 3,
    '实得 %s' % summary.get('user_raw_string_lines'))
# FACT_HINT_RE 在这 3 行中命中的：第1行（我...做了）和第5行（GPA）= 2
chk('fact_hint_lines == 2',
    summary.get('fact_hint_lines') == 2,
    '实得 %s' % summary.get('fact_hint_lines'))
# fact_hint_lines 不应等于 transcript_lines（修正前的 bug 特征）
chk('fact_hint_lines != transcript_lines（bug 已修）',
    summary.get('fact_hint_lines') != summary.get('transcript_lines'),
    'fact_hint=%s transcript=%s' % (summary.get('fact_hint_lines'),
                                     summary.get('transcript_lines')))

# ==========================================================================
# fail-open 验证
# ==========================================================================
print('\n=== fail-open 验证 ===')

# 喂不存在的 transcript
_bad_summary = H.transcript_summary('/nonexistent/path/transcript.jsonl')
chk('不存在的 transcript 不抛异常', 'transcript_error' in _bad_summary)

# extract_oral_clues 对不存在的文件返回错误而非异常
_n, _e = H.extract_oral_clues('/nonexistent/path.jsonl', 'test')
chk('extract_oral_clues 坏路径不抛异常', _e is not None and _n == 0,
    'n=%d e=%s' % (_n, _e))

# ==========================================================================
# M-2 回归：'别' 后向约束 + _is_noise 逐句过滤
# ==========================================================================
print('\n=== M-2：否定词收窄 + 逐句过滤 ===')

chk('M-2 _NEGATION_RE 不误杀「特别奖学金」',
    H._NEGATION_RE.search('我获得了特别奖学金') is None)
chk('M-2 _NEGATION_RE 不误杀「级别评定」',
    H._NEGATION_RE.search('级别评定通过') is None)
chk('M-2 _NEGATION_RE 仍拦「别说」',
    H._NEGATION_RE.search('别说这个') is not None)
chk('M-2 _NEGATION_RE 仍拦「别动」',
    H._NEGATION_RE.search('别动那个配置') is not None)
chk('M-2 「特别奖学金」应抽出线索',
    len(H._extract_clues_from_text('我获得了特别奖学金', 't', '/t.jsonl', 1, '')) > 0)
chk('M-2 逐句：否定分句不废掉同段事实',
    H._is_noise('别改这个文件，我2023年从某大学毕业') is False)
chk('M-2 逐句：整段皆否定仍判噪声',
    H._is_noise('别动那个，不要改这个') is True)

# ==========================================================================
# M-1 回归：落盘幂等键含 matched_pattern（多模式同行各自落盘）
# ==========================================================================
print('\n=== M-1：多模式同行落盘幂等 ===')

_m1_dir = tempfile.mkdtemp(prefix='m1_test_')
_m1_tp = os.path.join(_m1_dir, 'transcript.jsonl')
_m1_line = '我2023年从某大学毕业，我的GPA是3.8，我在某公司实习'
_m1_obj = {'type': 'user',
           'message': {'role': 'user', 'content': _m1_line},
           'timestamp': '2026-11-16T00:00:00', 'sessionId': 'm1-sess'}
with open(_m1_tp, 'w', encoding='utf-8') as f:
    f.write(json.dumps(_m1_obj, ensure_ascii=False) + '\n')

# Layer4 产出条数（绕过落盘层直接抽取，作为期望值）
_m1_expected = H._extract_clues_from_text(_m1_line, 'm1-sess', _m1_tp, 1,
                                          '2026-11-16T00:00:00')
chk('M-1 多模式行 Layer4 产出 >= 2 条', len(_m1_expected) >= 2,
    '实得 %d' % len(_m1_expected))

# 端到端：落盘条数应 == Layer4 产出条数（旧幂等键会吞成 1 条）
_orig_clues = H.ORAL_CLUES_PATH
_m1_clues = os.path.join(_m1_dir, 'oral_clues.jsonl')
H.ORAL_CLUES_PATH = _m1_clues
_n1, _e1 = H.extract_oral_clues(_m1_tp, 'm1-sess')
chk('M-1 端到端无报错', _e1 is None, str(_e1))
chk('M-1 落盘条数 == Layer4 产出条数', _n1 == len(_m1_expected),
    '落盘 %d vs 产出 %d' % (_n1, len(_m1_expected)))
if os.path.isfile(_m1_clues):
    with open(_m1_clues, 'r', encoding='utf-8') as f:
        _w = [json.loads(x) for x in f if x.strip()]
    _got_pats = sorted(set(c['matched_pattern'] for c in _w))
    _exp_pats = sorted(set(c['matched_pattern'] for c in _m1_expected))
    chk('M-1 落盘模式集 == 产出模式集', _got_pats == _exp_pats,
        '落盘 %s vs 产出 %s' % (_got_pats, _exp_pats))
# 重跑一次不重复追加
_n2, _e2 = H.extract_oral_clues(_m1_tp, 'm1-sess')
chk('M-1 幂等：重跑 0 条（键含 matched_pattern）', _n2 == 0, '实得 %d' % _n2)
if os.path.isfile(_m1_clues):
    with open(_m1_clues, 'r', encoding='utf-8') as f:
        _w2 = [json.loads(x) for x in f if x.strip()]
    chk('M-1 重跑后总条数不变', len(_w2) == len(_m1_expected),
        '实得 %d' % len(_w2))
H.ORAL_CLUES_PATH = _orig_clues

# ==========================================================================
# 汇总
# ==========================================================================
print('')
print('===== %d 项不符 =====' % len(FAIL))
for f in FAIL:
    print(' - ' + f)
sys.exit(1 if FAIL else 0)
