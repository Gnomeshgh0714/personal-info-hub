#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""个人信息中台采集器 v1。

按 collector/sources.json 白名单定点遍历各源根，把实习、项目、获奖、
技能、论文、教育背景六类事实抽成结构化条目，追加写入 中台数据/personal_info.jsonl，
再重生成六个分类视图与 待核验队列.md，最后在 采集日志.md 追加一条带五连计数的编号记录。

设计口径：
- 只读源、只写中台。所有源文件一律不修改、不移动、不删除。
- 严禁整盘遍历：只在白名单源根内用 os.scandir 定点下钻，受最大深度约束。
- jsonl 是追加式事件日志：同一 id 的更新以新行落盘、版本递增，旧行不删不改；
  下游视图按「同 id 取最后一行」聚合。
- 幂等：记录内容哈希不变则不追加新行，--full 重复跑不产生重复条目。
- 全程 fail-open：单文件异常一律 catch、计数、继续，不中断整轮。
- 每个数字都要能回到源文本行号；定位不到的把状态置「无源待确认」，
  采集器永不自动置「已核验」。

只用标准库；xlsx 走 openpyxl、docx 走 python-docx，缺库时退化为只登记路径并记日志。

用法：
    python3 collector/collect.py --full          # 忽略 mtime 游标，重扫全部源根
    python3 collector/collect.py --incremental   # 默认，走游标只处理新增/变更文件
    python3 collector/collect.py --dry-run       # 只扫描与计数，不落盘
"""

from __future__ import annotations

import argparse
import calendar
import datetime
import hashlib
import json
import os
import re
import sys
from html.parser import HTMLParser

# --------------------------------------------------------------------------
# 路径常量
# --------------------------------------------------------------------------
COLLECTOR_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(COLLECTOR_DIR)                 # 仓库根
DATA_DIR = os.path.join(BASE_DIR, '中台数据')
JSONL_PATH = os.path.join(DATA_DIR, 'personal_info.jsonl')
QUEUE_PATH = os.path.join(BASE_DIR, '待核验队列.md')
LOG_PATH = os.path.join(BASE_DIR, '采集日志.md')
SOURCES_PATH = os.path.join(COLLECTOR_DIR, 'sources.json')
STATE_DIR = os.path.join(COLLECTOR_DIR, 'state')
CURSOR_PATH = os.path.join(STATE_DIR, 'cursor.json')

CATEGORIES = ['实习经历', '项目经历', '获奖荣誉', '技能资产', '论文作品', '教育背景']

STATUS_PENDING = '待核验'
STATUS_VERIFIED = '已核验'
STATUS_CONFLICT = '主源冲突'
STATUS_NOSOURCE = '无源待确认'
STATUS_NEEDOCR = '需OCR人工录入'

# 场景警示（配置驱动）：源配置里的「场景警示规则」数组逐条匹配——条目的
# 简历场景含「场景含」子串、且名称/成果文本命中「文本匹配」正则时，把「警示」
# 文案写进条目的 场景警示 字段。典型用法：保研场景派生的排名/成绩类条目禁止
# 流入求职简历表述。不配规则则不打任何警示；正则一律忽略大小写，坏正则跳过。
def scene_warning(场景: str, 文本: str, 规则: list | None) -> str:
    for rule in 规则 or []:
        含 = rule.get('场景含') or ''
        if not 含 or 含 not in (场景 or ''):
            continue
        try:
            if re.search(rule.get('文本匹配') or '', 文本 or '', re.I):
                return rule.get('警示') or ''
        except re.error:
            continue
    return ''

# 落盘字段序：基础字段 + 场景字段 + 记录哈希。场景字段只在非空时写入，
# 所以 ordered 的拼装不能一律用 rec.get(k)（那会把 None 写进 jsonl）。
BASE_FIELDS = ('id', '版本', '分类', '名称', '时间', '角色', '成果', '量化值',
               '无源量化值', '来源盘', '来源工作区', '源清单', '证据路径', '证据定位',
               '采集时间', '采集方式', '扫描模式', '状态', '主源冲突组')
SCENE_FIELDS = ('简历场景', '场景警示', '主源裁决')

# 简历 tex 节名 → 六个分类
SECTION_CATEGORY = {
    '教育背景': '教育背景',
    '实习经历': '实习经历',
    '工作经历': '实习经历',
    '项目经历': '项目经历',
    '科研经历': '项目经历',
    '研究经历': '项目经历',
    '学术论文': '论文作品',
    '论文发表': '论文作品',
    'ai 应用经验': '项目经历',
    'ai应用经验': '项目经历',
    '技能': '技能资产',
    '技能与研究工具': '技能资产',
    '专业技能': '技能资产',
    '荣誉奖项': '获奖荣誉',
    '获奖情况': '获奖荣誉',
    '奖项': '获奖荣誉',
    '校园经历': '项目经历',
    '学生工作': '项目经历',
    '志愿活动': '获奖荣誉',
    '证书': '获奖荣誉',
    '头部': '',
}

# 简历里不属于六个分类的节（联系方式等），整类跳过不入库
SKIP_SECTIONS = {'', '头部', 'header', '联系方式', '基本信息', '个人信息'}

MAX_QUANT = 6          # 单条最多登记的量化值个数
SUMMARY_LEN = 800      # 成果字段落盘上限（视图里另行截短）
VIEW_LEN = 120         # 分类视图表格里的成果摘要长度
TEXT_READ_LIMIT = 4_000_000   # 单文本文件最多读入字符数

# 二进制类扩展名：凡遇到这类文件而未按文本解析，均计入「跳过二进制」
BINARY_EXTS = {
    '.pdf', '.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff', '.heic',
    '.doc', '.ppt', '.pptx', '.xls', '.xlsm', '.key', '.pages', '.numbers',
    '.zip', '.rar', '.7z', '.gz', '.tar', '.bz2', '.dmg', '.pkg',
    '.mp4', '.mov', '.avi', '.mp3', '.wav', '.m4a',
    '.db', '.sqlite', '.so', '.dylib', '.bin', '.exe', '.woff', '.woff2', '.ttf',
}


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def today() -> str:
    return datetime.datetime.now().astimezone().strftime('%Y-%m-%d')


# --------------------------------------------------------------------------
# 计数器
# --------------------------------------------------------------------------
class Stats:
    """一轮采集的全部计数与留痕。"""

    def __init__(self) -> None:
        self.新增 = 0
        self.更新 = 0
        self.待核验 = 0
        self.无源待确认 = 0
        self.跳过二进制 = 0
        # 辅助计数
        self.未变条目 = 0
        self.未变文件 = 0
        self.跳过压缩包 = 0
        self.跳过依赖目录 = 0
        self.跳过超大文件 = 0
        self.跳过重复副本 = 0
        self.跳过归档目录 = 0
        self.跳过其他类型 = 0
        self.跳过模板文件 = 0
        self.只登记路径 = 0
        self.无源标记被占用 = 0   # 含无源数字但状态已被主源冲突/需OCR 占用的条目
        self.命中文件 = 0
        self.异常 = []          # [(源名, 路径, 错误)]
        self.源明细 = []        # [{源, 状态, 命中, 条目, 说明}]
        self.缺库 = []          # 未能导入的第三方库

    def 记异常(self, 源名: str, 路径: str, err: Exception) -> None:
        self.异常.append((源名, 路径, '%s: %s' % (type(err).__name__, err)))


# --------------------------------------------------------------------------
# 文本清洗
# --------------------------------------------------------------------------
_ESCAPES = [(r'\%', '%'), (r'\&', '&'), (r'\$', '$'), (r'\_', '_'),
            (r'\#', '#'), (r'\{', '('), (r'\}', ')'), (r'\,', ','),
            (r'\;', ';'), (r'\!', '!'), ('--', '--'), ('~', ' ')]

_UNWRAP_RE = re.compile(
    r'\\(?:textbf|bf|emph|textit|it|underline|mbox|textsc|text|textrm|textsf|texttt|'
    r'mathbf|bm|uline|hl|colorbox)\{([^{}]*)\}')
_HREF_RE = re.compile(r'\\href\{[^{}]*\}\{([^{}]*)\}')
_URL_RE = re.compile(r'\\url\{([^{}]*)\}')
_CMD_RE = re.compile(r'\\[a-zA-Z@]+\*?(?:\[[^\[\]]*\])?(?:\{[^{}]*\})*')
_MATH_RE = re.compile(r'\$([^$]*)\$')
_SPACE_RE = re.compile(r'[ \t]{2,}')
# LaTeX 强制换行与其可选间距参数：\\[0.15em] / \\[6pt] / 裸 \\。
# _CMD_RE 只吃「反斜杠 + 字母」的命令，吃不掉这个纯符号形式，
# 不单独处理就会把 [0.15em] 的 0.15、[6pt] 的 6 当成正文数字漏进成果与量化值。
_BRK_RE = re.compile(r'\\\\\s*\[[^\[\]]*\]|\\\\')


_GREEK = [('\\beta', 'β'), ('\\alpha', 'α'), ('\\gamma', 'γ'), ('\\delta', 'δ'),
          ('\\Delta', 'Δ'), ('\\lambda', 'λ'), ('\\mu', 'μ'), ('\\sigma', 'σ'),
          ('\\theta', 'θ'), ('\\pi', 'π'), ('\\rho', 'ρ'), ('\\tau', 'τ'),
          ('\\times', '×'), ('\\cdot', '·'), ('\\leq', '≤'), ('\\geq', '≥'),
          ('\\approx', '≈'), ('\\neq', '≠'), ('\\pm', '±'), ('\\infty', '∞'),
          ('\\rightarrow', '→'), ('\\to', '→'), ('\\sum', 'Σ')]


def latex_inline_to_text(s: str) -> str:
    """把一行 LaTeX 正文清成可读纯文本（不处理结构命令，结构由解析器单独吃）。"""
    for a, b in _ESCAPES:
        s = s.replace(a, b)
    for a, b in _GREEK:
        s = s.replace(a, b)
    for _ in range(6):
        prev = s
        s = _HREF_RE.sub(r'\1', s)
        s = _URL_RE.sub(r'\1', s)
        s = _UNWRAP_RE.sub(r'\1', s)
        if s == prev:
            break
    s = _MATH_RE.sub(r'\1', s)
    s = _BRK_RE.sub(' ', s)
    s = _CMD_RE.sub('', s)
    s = s.replace('$', '').replace('{', '').replace('}', '')
    s = _SPACE_RE.sub(' ', s)
    return s.strip()


class _HTMLText(HTMLParser):
    """抽 HTML 可见文本、title 与标题层级。"""

    SKIP = {'script', 'style', 'svg', 'noscript', 'template', 'head'}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ''
        self.headings = []      # [(level, text, 累计字符偏移)]
        self.blocks = []        # 非标题文本块
        self._skip_depth = 0
        self._in_title = False
        self._cur_heading = None
        self._buf = []
        self._chars = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
            return
        if tag == 'title':
            self._in_title = True
            self._buf = []
        m = re.fullmatch(r'h([1-6])', tag)
        if m:
            self._flush()
            self._cur_heading = int(m.group(1))
            self._buf = []

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == 'title':
            self._in_title = False
            self.title = ''.join(self._buf).strip()
            self._buf = []
        m = re.fullmatch(r'h([1-6])', tag)
        if m and self._cur_heading is not None:
            text = _SPACE_RE.sub(' ', ''.join(self._buf)).strip()
            if text:
                self.headings.append((self._cur_heading, text, self._chars))
            self._cur_heading = None
            self._buf = []
        if tag in ('p', 'div', 'li', 'section', 'br', 'tr'):
            self._flush()

    def handle_data(self, data):
        if self._skip_depth:
            return
        self._buf.append(data)
        self._chars += len(data)

    def _flush(self):
        if self._cur_heading is not None or self._in_title:
            return
        text = _SPACE_RE.sub(' ', ''.join(self._buf)).strip()
        self._buf = []
        if len(text) >= 8:
            self.blocks.append((text, self._chars))

    def close(self):
        super().close()
        self._flush()


def html_to_parts(text: str) -> tuple[str, list, list]:
    p = _HTMLText()
    try:
        p.feed(text)
        p.close()
    except Exception:
        pass
    return p.title, p.headings, p.blocks


# --------------------------------------------------------------------------
# 量化值与溯源
# --------------------------------------------------------------------------
_UNIT = (r'%|％|万分之|个百分点|个点|个月|年半|人次|万字|千字|余字|多余字|'
         r'pp|bp|GB|MB|TB|KB|ms|小时|分钟|万元|亿元|美元|'
         r'字|个|份|项|篇|页|条|人|家|款|次|期|届|天|年|月|周|秒|倍|元|'
         r'行|轮|版|张|所|门|分|套|步|名|位|种|组|批|场|章|节|题|问|册|本|卷|级|阶段')
# 单位表的顶序必须把多字单位排在它的单字前缀之前（个月 先于 个、人次 先于 人、
# 分钟 先于 分、个百分点 先于 个），否则正则会在同一位置先匹上短的那个，
# 把「6 个月」切成「6个」+ 游走的「月」、「5 人次」切成「5人」+「次」。
# 数字部分：先试千位分隔形式（60,000 / 1,000），再试普通整数与小数。
# 两个「+」均要求与数字或单位**紧贴**：500+款 / 97%+ 是约数后缀，
# 而「映射 1 + 2 + 6」里带空格的 + 是算术加号，不能当成后缀拍成「1+」「2+」。
QUANT_RE = re.compile(
    r'(\d{1,3}(?:[,\uff0c]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(\+)?\s*(' + _UNIT + r')?(\+)?')
YEAR_RE = re.compile(r'^(19|20)\d{2}$')
# 文件名里的版本/序号数字（resume_v3.tex、analysis2.do）：数字后紧跟扩展名点
_FILE_EXT_RE = re.compile(r'\.(?:tex|md|pdf|docx?|xlsx?|html?|pptx?|py|do|R|txt|json)\b', re.I)
# 算术表达式：「映射 1 + 2 + 6 构成最小可行集成」里的加号是运算符号，
# 两边的数字是编号而非量化值，前后两个方向都要认出来才能全部挡掉。
_ARITH_POST_RE = re.compile(r'^\s*\+\s*\d')
_ARITH_PRE_RE = re.compile(r'\d\s*\+\s*$')
# 排名形式（5/30、前 5%）：分子分母都是有意义的事实数字，不拆开也不丢弃


def locate_number(token: str, lines: list[str], norm: list[str] | None = None) -> str:
    """在源文本行里定位数字 token，返回 'L12' 或 ''。

    先比规范化后的同行文本（去掉 LaTeX / HTML 标记，行号与原文一致），
    再比原始行；两者都匹不上才算真的无源。
    """
    for i, line in enumerate(lines, start=1):
        if token in line:
            return 'L%d' % i
    if norm:
        for i, line in enumerate(norm, start=1):
            if token in line:
                return 'L%d' % i
    return ''


_TAG_RE = re.compile(r'<[^>]+>')


def norm_lines(raw: list[str], kind: str) -> list[str]:
    """逐行规范化，长度与下标与 raw 一致，保证行号仍然是真的。"""
    out = []
    for line in raw:
        if kind == 'html':
            s = html_unescape(_TAG_RE.sub(' ', line))
            s = _SPACE_RE.sub(' ', s).strip()
        elif kind == 'tex':
            s = latex_inline_to_text(line)
        else:
            s = line.strip()
        out.append(s)
    return out


def html_unescape(s: str) -> str:
    import html as _html
    return _html.unescape(s)


# 日期上下文：带 4 位年份的日期串，或「N月N日」形式。
# 命中则该数字是日期碎片（2026-01-15 里的 01 / 15），不是成果量化值。
_DATE_CTX_RE = re.compile(
    r'(?:19|20)\d{2}\s*[.\-/年]\s*\d{1,2}|\d{1,2}\s*[.\-/月]\s*\d{1,2}\s*[日号]')
# 标识符前缀：数字紧贴这些符号时是型号/参数/锥号，不是量化值（Recall@5、#1、parsep=0pt）
_SYM_PRE = '@#=_~^|\\'
# 文档内部结构枚举：「幻灯片 1 / 幻灯片 2 / 幻灯片 3」这类导航序号不是事实数字
_ENUM_PRE_RE = re.compile(r'(?:幻灯片|幻灯|Slide|slide|封面页|目录页|备注页)\s*$')
# 年月日期：数字紧跟在「2005 年」这类年份之后时是月份/日期，不是时长
_PRE_YEAR_RE = re.compile(r'(?:19|20)\d{2}\s*[年.\-/]\s*$')
MAX_BARE_DIGITS = 6    # 无单位纯数字超过这个位数即电话/学号/邮编类标识，不是量化值
# 考试与绩点分数上下文：这类数字紧跟在考试名/分数词后，是真事实，
# 不能被「英文名 + 空格 + 版本号」规则误杀（IELTS 7.5、TOEFL 105、CET-6：600）。
# 中英文考试名都要收全：源文本里「英语（CET-6：600，IELTS：7.5）」与「雅思 7.5」
# 两种写法都存在，只覆盖 CET-6 会让 IELTS：7.5 落到下游的列表编号/时刻记号规则里
# 被当成噪声丢掉。
# 英文分数词 Score/Rating/Average 一并收进来：版本号规则放宽到小数之后，
# 「Score 4.5」这一类真分数必须靠这里豁免，否则会被当版本号清掉。
# 两个细节：
# 1. 英文词前后加 \b。不加就会匹上长词的尾巴：「impact 15」的 act、「benchmark: 5」
#    的 mark 都会误当考试名，把本该当版本号清掉的数字反过来保住。
# 2. 考试名与冒号之间容一个型号尾巴（CET-6、HSK6、TOEFL iBT）。
#    没有它，「CET-6:600」用半角冒号写时 6:60 会先命中时刻记号规则，
#    而 600 的前导窗口是「CET-6:」——考试名与冒号被「-6」隔开，豁免不上，600 会丢。
# 本表在三处规则里都只用作「豁免」，扩大它只会多留数字、不会多删数字。
_SCORE_EXAM_EN = (r'IELTS|TOEFL|TOEIC|GRE|GMAT|LSAT|MCAT|CET|PTE|SAT|ACT|BEC|CATTI|'
                  r'Duolingo|DET|GPA|Score|Rating|Average')
_SCORE_EXAM_ZH = (r'雅思|托福|托业|多邻国|大学英语|四六级|六级|四级|专四|专八|'
                  r'绩点|分数|得分|成绩|评分|均分|平均分|排名|名次')
_SCORE_PRE_RE = re.compile(
    r'(?:(?:\b(?:' + _SCORE_EXAM_EN + r')\b'
    r'(?:\s*[-–—/·]?\s*(?:\d{1,2}|iBT|[ivxIVX]{1,4}))?)'
    r'|(?:' + _SCORE_EXAM_ZH + r'))\s*[:：]?\s*$', re.I)
# ISO 时间戳分量：2026-11-11T10:56:24+0800、15:21:07+08:00 里的时分秒是机器
# 生成的时刻记号，不是业绩数字。判据是「时:分」（分必须两位），因此全角冒号的
# 真分数（CET-6：600）不命中；限定无单位，「历时 2小时」一类真时长也不受影响。
_TIME_CTX_RE = re.compile(r'\d{1,2}:\d{2}(?::\d{2})?')
# 时区偏移尾巴：数字后的加号若引出 0800 / 08:00，那是时区，
# 不是「500+款」「97%+」那类约数后缀。
_TZ_TAIL_RE = re.compile(r'(?:\d{4}(?!\d)|\d{1,2}:\d{2})')
# 多级章节号：1.1 / 2.3.1。成果里「小节：一、结论先给 / 1.1 选型建议 / 1.2 各档位」
# 这类标题编号是文档结构，不是成果量化值。单级整数不纳入，避免误杀真数字。
_SECTION_NO_RE = re.compile(r'^\d+(?:\.\d+){1,3}$')
# 章节号的前导字符集：数字紧邻前面（2 字符内）全是空白或这些分隔符时，
# 它是被列举出来的编号；夹在中文词后面（「从 1.1 提升到 2.4」）则照常保留。
# § 必须在集里：「定性锚点来自内部报告 §8.2」「验证器分歧审计（§3.4 · 图 5）」
# 里的 §N.N 是论文章节引用，不是业绩数字。
_SECTION_PRE_CHARS = ' \t\n/|>#*·-—、，,；;：:§'
# 章节引导词：「小节：1.1 工作概述 / 1.2 DCF」「目录：3.2 数据清洗」里，
# 编号紧跟在引导词后（可带全/半角冒号与空白），它是文档结构编号。
# 单列一条而不能靠把冒号加进 _SECTION_PRE_CHARS 解决：那里只看紧邻 2 字符，
# 「小节：1.1」的 2 字符窗口是「节：」，中文字「节」不在分隔符集里。
_SECTION_LEAD_RE = re.compile(
    r'(?:小节|小标题|章节|分节|目录|提纲|大纲|附录)\s*[:：]?\s*$')
# 比值前导豁免：斜杠前紧邻（跳空白）是数字时，斜杠后面是比值的分母——量表上限
# 或总分，是真事实（GPA & 3.8 / 4.0、IELTS 7.5 / 9.0）。而「1.1 选型建议 / 1.2 各档位」
# 里斜杠前是文字，后面才是被列举出来的章节号。缺了这条豁免，带空格的
# 「3.8 / 4.0」会因前导 2 字符是「/ 」而把 4.0 误判成章节号丢掉。
_RATIO_PRE_RE = re.compile(r'\d(?:\.\d+)?\s*/\s*$')
# 电话号码片段（R1-3）：区号（可带括号与 +）后接至少两组 3-4 位、以空格/点/横线
# 分隔的数字；或不含区号的三组以上同形态数字。落在命中区间里的数字是号码碎片而非
# 量化值——「(+86) 138 0000 0000」四段即此形态。
# 不做「联系方式整段豁免」的理由：成果字段落盘前已把换行折成空格、压成单行，号码
# 碎片与已复验真值 5（Availability: 5 days/week）、3.8/4.0（GPA）同处一段，整段豁免
# 会连带杀掉真值。
# 不误伤的形态：小数（12.3）、千位分隔（1,000）、日期（2026-11-11 的月日只 2 位）、
# 区间（16--60）、比值（38/38）都构不成「≥3 段 3-4 位数字」；5 days/week 与
# GPA: 3.8/4.0 后面紧跟英文词或斜杠，也不落在任何号码区间内。
_PHONE_GROUP_RE = re.compile(
    r'(?:\(\s*)?\+?\d{1,4}(?:\s*\))?[\s.\-]?\d{3,4}(?:[\s.\-]\d{3,4}){1,}'
    r'|\d{3,4}(?:[\s.\-]\d{3,4}){2,}')
# 号码片段只在候选数字附近的窗口里找（成果字段上限 800 字符），前 16 后 32 足以
# 覆盖「区号 + 四组」的最长形态（约 25 字符），避免对每个候选数字全文扫一遍。
_PHONE_WIN = (16, 32)
# 遮罩占位符（R1-3）：数字后紧跟 X / ＊ 一类打码占位符时，它是被遮掉的联系方式或
# 年份碎片（1XX-XXXX-XXXX 的 1、20XX 学年的 20），不是事实数字。
_MASK_TAIL_RE = re.compile(r'[XxＸｘ＊*]')
# 文件名主干序号（R1-3）：数字前紧贴文档名词、且本身是无单位裸整数时，它是文件名
# 派生的序号（「示例简历3 原文」的 3、「详见附件报告2」的 2），不是量化值。
# 既有 _FILE_EXT_RE 只认「数字 + 扩展名点」，无扩展名的写法会漏网。
# 三重收窄保住真值：只认裸整数（3份报告、5套模型 带单位不受影响）、只认纯整数
# （GPA 3.8 一类小数不受影响）、排除纯年份（2023级 由既有 YEAR_RE 规则处理）。
_DOC_STEM_RE = re.compile(
    r'(?:简历|报告|论文|文书|模板|稿件?|版本|附件|总结|计划|方案|记录|笔记|清单|'
    r'说明|材料|文件|文档|答卷|作业|试卷|提纲|大纲|表格|截图|照片|证书|证明|'
    r'推荐信|陈述)$')


def _in_phone_group(文本: str, start: int, end: int) -> bool:
    """候选数字是否落在电话号码片段区间内（R1-3 联系方式去噪）。

    只在 [start-16, end+32] 的局部窗口里找号码片段，且要求某个命中区间**完整覆盖**
    该数字：窗口被截断时宁可漏判（少杀）也不误判（错杀真值）。
    """
    lo = max(0, start - _PHONE_WIN[0])
    seg = 文本[lo:min(len(文本), end + _PHONE_WIN[1])]
    a, b = start - lo, end - lo
    return any(pm.start() <= a and b <= pm.end() for pm in _PHONE_GROUP_RE.finditer(seg))


def _quant_match(文本: str, m, seen: set, 时间数字=frozenset()):
    """校验一个 QUANT_RE 匹配是否为有效量化值，返回 (值, 数字) 或 None。

    挡掉下列非成果量化值（基于首次全量采集的量化值噪音归类而定）：
    1. 列表编号「（1）」与有序列表「0. 目标 / 1. 项目资产地图」；
    2. 型号与参数标识：数字紧贴 ASCII 字母、连字符或 @#=_ 等符号
       （CET-6、N4、GPT-4、HTML5、v4、Recall@5、parsep=0pt）；
    3. 英文技术名后的版本号：PostgreSQL 15、Material Design 3；
    4. 日期碎片：2026-01-15 / 2026.06 / 2026年3月 里的各段（含纯年份）；
    5. 前导零碎片：0、01、02；
    6. ISO 时间戳与时区偏移：10:56:24+0800 / 15:21:07+08:00 里的各段；
    7. 多级章节号：1.1 选型建议 / 2.3.1 实现细节 / 4.1 题目（带单位也认）；
       引导词后的编号（小节：1.1 工作概述）；
       § 紧邻数字的一律当章节引用：§8.2、以及括号里的（§3.4 · 图 5）；
    8. 点分串的各段：内网 IP 192.0.2.1 里的 192.0 与 2.1；
    9. 英文技术名 + 空格 + 版本号，整数与小数都认：PostgreSQL 15、Django 5.2、
       Terminal-Bench 2.0 / 4.0。紧跟 ASCII 字母的（实测 6.3s）与考试分数
       （GPA 3.8、IELTS：7.5）靠 _SCORE_PRE_RE 与后置字母守卫豁免。
    10. 电话号码片段：(+86) 138 0000 0000、138 1234 5678 里落在号码区间内的各段；
    11. 遮罩占位符：1XX-XXXX-XXXX 的 1、20XX 学年的 20；
    12. 文件名主干序号：无扩展名的写法也要挡——「示例简历3 原文」的 3、「附件报告2」
        的 2（既有 _FILE_EXT_RE 只认「数字 + 扩展名点」）。
    明确保留的有价值形式：带单位（21家、97%、14套）、带加号（500+款、20+个）、
    小数（GPA 3.8、IELTS 7.5、实测 6.3s）、排名与分数比（专业排名 5/30、38/38 测试全绿）、
    考试分数（CET-6：600、IELTS：7.5）、量表分母（GPA & 3.8 / 4.0 的 4.0）、
    句中数值（从 1.1 提升到 2.4）、英文冒号后的真事实（Availability: 5 days/week）。
    加号按它在源文本里的真实位置拼接。
    """
    num = m.group(1)
    unit = m.group(3) or ''
    pre_plus = m.group(2) or ''
    post_plus = m.group(4) or ''
    pre = 文本[m.start() - 1] if m.start() > 0 else ''
    post = 文本[m.end()] if m.end() < len(文本) else ''
    # 时区偏移：数字后的加号引出 0800 / 08:00 时，整个数字是 ISO 时间戳的秒段，
    # 不是「500+款」那类约数后缀（24+0800、07+08:00 即此噪声）。
    if post_plus and _TZ_TAIL_RE.match(文本[m.end():m.end() + 6]):
        return None                     # 24+0800 / 07+08:00：加号后是时区偏移
    # QUANT_RE 允许「数字 + 空格 + 单位」，但也因此让尾部 (\+)? 能跳过空格
    # 抓到算术加号（「1 + 2」→ post_plus='+'）。要求 + 与数字或单位紧贴，
    # 否则当作算术符号丢弃：500+款、97%+ 保留，映射 1 + 2 不拍成「1+」「2+」。
    if post_plus and not unit and not (m.end() >= 2 and 文本[m.end() - 2].isdigit()):
        post_plus = ''
        if re.match(r'^\s*\d', 文本[m.end():m.end() + 4]):
            return None                     # 「1 + 2」：隔空的加号后面又接数字，是算式
    裸 = not unit and not pre_plus and not post_plus

    # 前导零碎片必须在「裸」判断之外：07+08:00 的 07 因为带了时区加号而不算裸，
    # 留在裸分支里就会漏过。带单位的 0.5倍、0% 一类不受影响。
    if not unit and re.fullmatch(r'0\d*', num):
        return None

    if 裸 and (_ARITH_POST_RE.match(文本[m.end():m.end() + 6])
               or _ARITH_PRE_RE.search(文本[max(0, m.start() - 6):m.start()])):
        return None                         # 算术表达式里的编号

    # 标识符前缀：数字紧贴 ASCII 字母、连字符或 @#=_ 等符号时，是型号/等级/参数
    # 而非量化值（CET-6、N4、GPT-4、HTML5、v4、Recall@5、parsep=0pt、#1#2%）。
    # 中文前缀不受影响：「提升60%」「共5人」「第3章」照常保留。
    if pre and (pre in _SYM_PRE or pre == '-' or (pre.isascii() and pre.isalpha())):
        return None
    if post == '_':                       # 目录名编号前缀：01_主简历
        return None
    # 遮罩占位符（R1-3）：1XX-XXXX-XXXX 的 1、20XX 学年的 20 都是被打码的联系方式/
    # 年份碎片。只看紧邻的后一个字符且要求无单位：带单位的量化值后面跟的是单位汉字，
    # 不会是 X；「3XL」一类尺码写法同样按噪声处理（它本来就不是业绩量化值）。
    if not unit and post and _MASK_TAIL_RE.match(post):
        return None
    # 多级编号的尾段：2.3.1 会被 QUANT_RE 切成 2.3 与 1，剩下的 1 前面紧贴
    # 「小数点 + 数字」，说明它只是编号的一段，不是独立数值。
    if pre == '.' and m.start() >= 2 and 文本[m.start() - 2].isdigit():
        return None
    # 点分串的前中段：192.0.2.1（内网 IP）会被切成 192.0 与 2.1，
    # 后者被上面的尾段规则拦下，前者靠这条：数字后紧贴「点 + 数字」时，
    # 它只是点分串（IP、版本号、多级编号）的一段。真小数不受影响：
    # 「GPA 3.8 / 4.0」的 3.8 后是空格、「实测 6.3s」后是单位。
    if post == '.' and m.end() + 1 < len(文本) and 文本[m.end() + 1].isdigit():
        return None                     # 192.0.2.1 / 2.3.1 的前中段
    # §N / §N.N 章节引用符：§ 紧邻数字时它一定是论文或报告的章节号，不是业绩
    # 数字（「验证器分歧审计（§3.4 · 图 5）」「.git history 抄答案（§7.5）」）。
    # 单列一条而不只靠 _SECTION_PRE_CHARS：那里查的是「紧邻前面 2 字符全是
    # 分隔符」，而 § 前面常带全角括号（「（§3.4」），窗口会被括号挡住；反过来
    # 把括号也加进分隔符集，行首的「（12.3 天）」就会被误判成章节号。
    if pre == '§':
        return None                     # §3.4 / §7.5 / §8.2 / §7
    if _FILE_EXT_RE.match(文本[m.end():m.end() + 8]):
        return None                         # 文件名里的序号：resume_v3.tex
    # 文件名主干序号的无扩展名写法（R1-3）：「全部例句逐字取自示例简历3 原文」的 3
    # 后面是空格而非扩展名点，_FILE_EXT_RE 挡不住。
    # 三重收窄（裸 + 纯整数 + 非年份）保住 3份报告、GPA 3.8、2023级、第7 等真值。
    if (裸 and num.isdigit() and not YEAR_RE.match(num)
            and _DOC_STEM_RE.search(文本[max(0, m.start() - 6):m.start()])):
        return None                         # 示例简历3 / 附件报告2 / 论文3
    # 电话号码片段（R1-3）：「(+86) 138 0000 0000」四段。只豁免落在号码区间内的
    # 数字，同段真值 5（Availability）与 3.8/4.0（GPA）照常保留。
    # 用 m.end(1)（数字本身的末尾）而非 m.end()：后者会把 \s* 吃进的空格算进来，
    # 让区间右界越过数字本身，可能把紧邻其后的真值一并罩住。
    if _in_phone_group(文本, m.start(), m.end(1)):
        return None                         # 86 / 138 / 0000 / 0000 一类号码碎片
    if pre in ('（', '(') and post in ('）', ')') and 裸:   # 列表编号「（1）」
        return None
    if num in seen:
        return None

    win_pre = 文本[max(0, m.start() - 8):m.start()]
    if _ENUM_PRE_RE.search(win_pre):        # 幻灯片 1 / 幻灯片 2 结构枚举
        return None
    if _PRE_YEAR_RE.search(win_pre) and unit in ('', '月', '日', '号'):
        return None                     # 「2005 年 8 月」的 8 是月份，不是时长
                                        # 限定单位，避免误杀「2024 年 3 份报告」
    # ISO 时间戳分量：不限「裸」，因为 10:56:24+0800 里的 24 会带上时区加号。
    # 豁免考试分数：「CET-6:600，IELTS:7.5」用半角冒号写时，6:60 会让本规则误命中，
    # 把紧跟在考试名后的真分数当成时刻记号丢掉，所以考试名/分数词后的一律放行。
    if (not unit and _TIME_CTX_RE.search(文本[max(0, m.start() - 12):m.end() + 12])
            and not _SCORE_PRE_RE.search(文本[max(0, m.start() - 12):m.start()])):
        return None                     # 10:56:24 / 15:21:07 一类时刻记号
    # 章节引导词后紧跟的编号：小节：1.1 工作概述 / 目录：3.2 数据清洗
    if _SECTION_NO_RE.match(num) and _SECTION_LEAD_RE.search(
            文本[max(0, m.start() - 12):m.start()]):
        return None                     # 小节：1.1 / 目录：3.2 / 附录：2.1
    # 多级章节号：紧邻前面只有空白与分隔符时，是被列举出来的标题编号。
    # 同样不限「裸」：_UNIT 白名单含「题/项/节/章/问」一类词，「4.1 题目与三层
    # 递进问题」「2.2 项目密钥与交接包」会被切成带单位的 4.1题 / 2.2项，
    # 留在裸分支里就会漏过。豁免两类真事实：考试与绩点分数（GPA 3.8）、
    # 比值分母（GPA & 3.8 / 4.0 的 4.0）；夹在中文词后的一律保留
    # （约 12.3 天、估值17.80亿元、下降 0.8%、实测 6.3s、从 1.1 提升到 2.4）。
    if (_SECTION_NO_RE.match(num)
            and not _SCORE_PRE_RE.search(文本[max(0, m.start() - 12):m.start()])
            and not _RATIO_PRE_RE.search(文本[max(0, m.start() - 12):m.start()])
            and all(c in _SECTION_PRE_CHARS for c in 文本[max(0, m.start() - 2):m.start()])):
        return None                     # 1.1 选型建议 / §8.2 / 4.1 题目 / 2.3.1 实现细节

    if 裸:
        if len(num.replace(',', '').replace('，', '')) > MAX_BARE_DIGITS:
            return None                     # 电话号码、学号、邮编类长数字串
        if YEAR_RE.match(num):            # 纯年份：时间是时间，不是成果量化
            return None
        win = 文本[max(0, m.start() - 10):m.end() + 10]
        if _DATE_CTX_RE.search(win):      # 日期碎片
            return None
        j = m.start() - 1                 # 英文技术名 + 空格 + 版本号
        # 小数版本号同样要挡：Django 5.2、Terminal-Bench 2.0 / 4.0 都是产品版本号，
        # 不是业绩数字（旧版这里带 '.' not in num 条件，正好把小数版本号全放过了）。
        # 放宽之后靠三重豁免保住真值：考试与绩点分数（GPA 3.8、IELTS：7.5、Score 4.5）、
        # 比值分母（3.8 / 4.0）、以及数字后紧跟 ASCII 字母的度量写法（实测 6.3s、默认 5s
        # ——s 不在 _UNIT 白名单里，没有这道守卫就会被当成「英文名 + 版本号」清掉）。
        # 字母守卫必须看 m.end(1)（数字本身的末尾）而不是 m.end()：QUANT_RE 的
        # \s* 会把数字后的空格一起吃进匹配，用 m.end() 则「for 6 months」的 post 变成 m，
        # 反而把该当版本号清掉的 6 保住；用 m.end(1) 才能分开「6.3s」（紧贴字母，真度量）
        # 与「6 months」（隔空格，英文名 + 数字）。
        # 「Availability: 5 days/week」的 5 靠 k 位守卫保住：它前面是「: 」，
        # 冒号不是 ASCII 字母，不构成「英文技术名 + 空格 + 数字」形态。
        尾字母 = 文本[m.end(1)] if m.end(1) < len(文本) else ''
        if (j >= 0 and 文本[j] == ' '
                and not (尾字母.isascii() and 尾字母.isalpha())
                and not _SCORE_PRE_RE.search(文本[max(0, m.start() - 12):m.start()])
                and not _RATIO_PRE_RE.search(文本[max(0, m.start() - 12):m.start()])):
            k = j - 1
            if k >= 0 and 文本[k].isascii() and 文本[k].isalpha():
                return None
        # 下面两条必须用元组判成员：post 在文本末尾是空串，而 '' in '.、)）' 恒为 True，
        # 用字符串包含会把握在末尾的「CET-6：600」「共完成 12」误当有序列表编号丢掉。
        # 豁免考试分数：「IELTS：7.5）」的 7.5 前是全角冒号、后是右括号，形态与
        # 列表编号「1.」「（1）」完全重合，只靠位置判会把它当编号丢掉。
        if (post in ('.', '、', ')', '）') and (not pre or pre.isspace() or pre in (':', '：'))
                and not _SCORE_PRE_RE.search(文本[max(0, m.start() - 12):m.start()])):
            nxt = 文本[m.end() + 1] if m.end() + 1 < len(文本) else ' '
            if post != '.' or nxt.isspace():   # 有序列表编号（「3.8」的小数点不算）
                return None
    elif YEAR_RE.match(num) and unit in ('年', '月'):
        return None                         # 2026年 是时间点；3年 / 6月 才是时长

    值 = (num + pre_plus + unit) if pre_plus else (num + unit + post_plus)
    return 值.strip(), num


def extract_quants(成果: str, lines: list[str], 时间: str,
                  norm: list[str] | None = None) -> list[dict]:
    """从成果文本里抽数字并回到源文本定位；定位不到的标 已溯源=false。"""
    out = []
    seen = set()
    时间数字 = frozenset(re.findall(r'\d+(?:\.\d+)?', 时间 or ''))
    for m in QUANT_RE.finditer(成果 or ''):
        got = _quant_match(成果 or '', m, seen, 时间数字)
        if got is None:
            continue
        值, num = got
        seen.add(num)
        定位 = locate_number(num, lines, norm)
        if not 定位 and 值 != num:
            定位 = locate_number(值, lines, norm)
        start = max(0, m.start() - 14)
        上下文 = (成果[start:m.end() + 14]).replace('\n', ' ').strip()
        out.append({'值': 值, '定位': 定位 or '未定位',
                    '已溯源': bool(定位), '上下文': 上下文})
        if len(out) >= MAX_QUANT:
            break
    return out


def quants_from_points(要点: list, lines: list[str], tex_norm: list[str],
                       时间: str = '') -> list[dict]:
    """从简历要点 (文本, 行号) 抽量化值。

    优先用要点自带的行号做同行校验（数字就在这一行），匹不上再全文定位。
    与 extract_quants 共用 QUANT_RE，排除列表编号「（1）」这类非量化值。
    """
    out: list[dict] = []
    seen = set()
    时间数字 = frozenset(re.findall(r'\d+(?:\.\d+)?', 时间 or ''))
    for t, ln in 要点:
        for m in QUANT_RE.finditer(t):
            got = _quant_match(t, m, seen, 时间数字)
            if got is None:
                continue
            值, num = got
            seen.add(num)
            同行 = tex_norm[ln - 1] if 0 < ln <= len(tex_norm) else ''
            qloc = ('L%d' % ln) if (num in 同行) else locate_number(num, lines, tex_norm)
            out.append({'值': 值, '定位': qloc or '未定位',
                        '已溯源': bool(qloc),
                        '上下文': t[max(0, m.start() - 14):m.end() + 14].strip()})
            if len(out) >= MAX_QUANT:
                return out
    return out


def truncate(s: str, n: int = SUMMARY_LEN) -> str:
    s = (s or '').strip()
    return s if len(s) <= n else s[:n].rstrip() + '…'


# 视图渲染层要折成空格的字符：除 \n \t 外的 C0 控制字符（U+0000-U+001F）。
# \n 由 cell() 末尾单独折成空格；\t 保留（渲染为空白、不折断表格）。
# DEL（U+007F）未纳入：实测 jsonl 全库 0 命中，且它不属于 C0。
VIEW_CTRL_RE = re.compile('[\x00-\x08\x0b-\x1f]')


def cell(s) -> str:
    """md 表格单元格转义。

    归一化口径（R1-2）：除 \n \t 外的 C0 控制字符一律折成空格。
    U+000B（垂直制表）与 U+000D（回车）会被 markdown 渲染器当换行处理、把表格行
    折断——源文件原样带入的垂直制表曾导致分类视图的表格行断成两行。

    只作用于视图渲染层：jsonl 是 append-only 事实源，源字符原样保留、绝不改写
    （写盘走 json.dumps，不经过本函数），证据映射仍可回溯到源文件的原始字节。
    """
    if s is None:
        return ''
    s = VIEW_CTRL_RE.sub(' ', str(s))
    return s.replace('|', '\\|').replace('\n', ' ').strip()


def short_path(p: str) -> str:
    """把绝对路径压成可读短路径（保留盘符与末三级）。"""
    parts = p.split(os.sep)
    parts = [x for x in parts if x]
    if len(parts) <= 4:
        return '/' + '/'.join(parts)
    return '/…/' + '/'.join(parts[-3:])


# --------------------------------------------------------------------------
# 游标
# --------------------------------------------------------------------------
def load_cursor() -> dict:
    try:
        with open(CURSOR_PATH, 'r', encoding='utf-8') as f:
            c = json.load(f)
    except (OSError, ValueError):
        c = {}
    c.setdefault('文件', {})
    c.setdefault('id映射', {})
    c.setdefault('记录哈希', {})
    c.setdefault('序号', 0)
    return c


def save_cursor(cursor: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    cursor['更新时间'] = now_iso()
    tmp = CURSOR_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cursor, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, CURSOR_PATH)


def next_id(cursor: dict, key: str) -> str:
    """按去重键取稳定 id；首次出现则分配新序号。"""
    if key in cursor['id映射']:
        return cursor['id映射'][key]
    cursor['序号'] = int(cursor.get('序号', 0)) + 1
    new = 'PI-%04d' % cursor['序号']
    cursor['id映射'][key] = new
    return new


def dedup_key(分类: str, 名称: str, 证据路径: list[str]) -> str:
    路径 = 证据路径[0] if 证据路径 else ''
    if 路径.endswith('.tex'):
        return '%s|%s|%s' % (分类, 名称, 路径)   # 简历分节：名称仍是节内唯一判别位
    return '%s|%s|单条' % (分类, 路径)            # 一文件一条：路径即身份


def record_hash(rec: dict) -> str:
    """内容哈希：排除 id/版本/采集时间/扫描模式/记录哈希，保证重跑不产生新行。"""
    payload = {k: v for k, v in rec.items()
               if k not in ('id', '版本', '采集时间', '扫描模式', '记录哈希')}
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode('utf-8')).hexdigest()[:16]


# --------------------------------------------------------------------------
# 读取源文件
# --------------------------------------------------------------------------
def read_text_file(path: str) -> str:
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        return f.read(TEXT_READ_LIMIT)


def parse_frontmatter(text: str) -> tuple[dict, str, int]:
    """解析 YAML 风格 frontmatter，返回 (字段, 剩余正文, 正文起始行号)。"""
    lines = text.split('\n')
    if not lines or lines[0].strip() != '---':
        return {}, text, 1
    fm = {}
    for i, line in enumerate(lines[1:], start=2):
        if line.strip() in ('---', '...'):
            return fm, '\n'.join(lines[i:]), i + 1
        m = re.match(r'^([A-Za-z_\u4e00-\u9fff][\w\u4e00-\u9fff]*)\s*:\s*(.*)$', line)
        if m:
            fm[m.group(1).strip()] = m.group(2).strip().strip('"\'')
    return {}, text, 1


def md_title_and_summary(text: str, fallback: str) -> tuple[str, str, str, int]:
    """返回 (标题, 一句话摘要, 证据定位, 标题行号)。"""
    fm, body, offset = parse_frontmatter(text)
    lines = body.split('\n')
    title, title_ln = '', 0
    for i, line in enumerate(lines, start=1):
        m = re.match(r'^#\s+(.+?)\s*#*$', line)
        if m:
            title = latex_inline_to_text(m.group(1)) or m.group(1).strip()
            title_ln = i
            break
    if not title:
        title = fm.get('title') or fallback
    # 摘要：优先标题后的实义 blockquote，其次首个实义段落
    summary, sum_ln = '', 0
    for i, line in enumerate(lines[title_ln:], start=title_ln + 1):
        st = line.strip()
        if not st or st.startswith('#'):
            continue
        if st.startswith('>'):
            cand = st.lstrip('>').strip()
            # 知识库一类文档的 blockquote 是元信息栏，不是内容摘要，跳过
            if re.match(r'^\*\*(文档类型|生成时间|归档库路径|文件总数|数据来源|条目数|'
                        r'创建|更新|负责人|版本)\*\*', cand):
                continue
        elif st.startswith(('|', '---', '```', '![', '<')):
            continue
        else:
            cand = re.sub(r'^[-*+]\s+', '', st)
        cand = latex_inline_to_text(cand)
        if len(cand) >= 6:
            summary, sum_ln = cand, i
            break
    定位 = 'L%d' % (title_ln + offset - 1) if title_ln else 'L%d' % offset
    if sum_ln:
        定位 += '-L%d' % (sum_ln + offset - 1)
    return title, summary, 定位, title_ln


# --------------------------------------------------------------------------
# 简历 tex 解析
# --------------------------------------------------------------------------
_BANNER_RE = re.compile(r'^%\s*[=]{3,}\s*(.+?)\s*[=]{3,}\s*$')
_SECTION_RE = re.compile(r'\\(?:resumesection|section\*?|chapter\*?)\{([^{}]*)\}')
_ENTRY_RE = re.compile(r'\\resumeentry\{((?:[^{}]|\{[^{}]*\})*)\}\s*\{((?:[^{}]|\{[^{}]*\})*)\}')
_SUBENTRY_RE = re.compile(r'\\resumesubentry\{((?:[^{}]|\{[^{}]*\})*)\}\s*\{((?:[^{}]|\{[^{}]*\})*)\}')
_ENTRY_FB_RE = re.compile(r'\\textbf\{((?:[^{}]|\{[^{}]*\})*)\}\s*\\hfill\s*(.*)$')
_ITEM_RE = re.compile(r'^\s*\\item\s*(.*)$')
_END_RE = re.compile(r'^\s*\\end\{')


def parse_resume_tex(text: str) -> list[dict]:
    """把简历 tex 拆成 [{节, 名称, 时间, 角色, 地点, 要点[], 起行, 止行}]。

    只解析 \\begin{document} 之后的正文，避免把 \\newcommand 定义里的
    \\textbf{#1}\\hfill{#2} 误当成一条真实经历。
    """
    lines = text.split('\n')
    起 = 0
    for i, line in enumerate(lines):
        if '\\begin{document}' in line:
            起 = i + 1
            break
    section = ''
    section_disp = ''          # 节名原文（保留大小写），只用于定位显示
    entries: list[dict] = []
    cur: dict | None = None
    item_buf: list[str] = []
    item_ln = 0

    def flush_item():
        nonlocal item_buf, item_ln
        if cur is not None and item_buf:
            raw = ' '.join(item_buf)
            cleaned = latex_inline_to_text(raw)
            if cleaned:
                cur['要点'].append((cleaned, item_ln))
        item_buf, item_ln = [], 0

    def flush_entry(end_ln: int):
        nonlocal cur
        flush_item()
        if cur is not None:
            cur['止行'] = max(cur['起行'], end_ln - 1)
            entries.append(cur)
            cur = None

    for idx, raw in enumerate(lines[起:], start=起 + 1):
        line = raw.rstrip()
        if '\\newcommand' in line or '\\newenvironment' in line or '#1' in line or '#2' in line:
            continue
        mb = _BANNER_RE.match(line)
        if mb and not _SECTION_RE.search(line):
            flush_entry(idx)
            section_disp = mb.group(1).strip()
            section = section_disp.lower()
            if section in SKIP_SECTIONS:
                cur = None
            continue
        ms = _SECTION_RE.search(line)
        if ms:
            flush_entry(idx)
            section_disp = latex_inline_to_text(ms.group(1)).strip()
            section = section_disp.lower()
            if section in SKIP_SECTIONS:
                cur = None
            continue
        me = _ENTRY_RE.search(line)
        if not me:
            mfb = _ENTRY_FB_RE.search(line)
            if mfb and '\\newcommand' not in line and '\\item' not in line:
                me = mfb
        if me:
            flush_entry(idx)
            name = latex_inline_to_text(me.group(1))
            when = latex_inline_to_text(me.group(2)) if me.lastindex and me.lastindex >= 2 else ''
            cur = {'节': section, '节原文': section_disp, '名称': name, '时间': when,
                   '角色': '', '地点': '', '要点': [], '起行': idx, '止行': idx}
            continue
        msub = _SUBENTRY_RE.search(line)
        if msub and cur is not None:
            flush_item()
            cur['角色'] = latex_inline_to_text(msub.group(1))
            cur['地点'] = latex_inline_to_text(msub.group(2))
            continue
        mit = _ITEM_RE.match(line)
        if mit:
            flush_item()
            # 没有 \resumeentry 的整块（技能、教育背景常见）：起一个隐式条目兜住 \item
            if cur is None and section not in SKIP_SECTIONS:
                cur = {'节': section, '节原文': section_disp, '名称': '', '时间': '',
                       '角色': '', '地点': '', '要点': [], '起行': idx, '止行': idx,
                       '散段': True}
            item_buf = [mit.group(1)]
            item_ln = idx
            continue
        if _END_RE.match(line):
            flush_item()
            continue
        if cur is not None and item_buf:
            item_buf.append(line.strip())
            continue
        # 无 \item 的散段正文：同样归到隐式条目
        if cur is None and section and section not in SKIP_SECTIONS:
            stripped = latex_inline_to_text(line)
            if len(stripped) >= 6 and not line.lstrip().startswith('%'):
                cur = {'节': section, '节原文': section_disp, '名称': '', '时间': '',
                       '角色': '', '地点': '', '要点': [], '起行': idx, '止行': idx,
                       '散段': True}
                item_buf = [line.strip()]
                item_ln = idx
    flush_entry(len(lines) + 1)
    return entries


# --------------------------------------------------------------------------
# 第三方库（可选）
# --------------------------------------------------------------------------
def try_openpyxl(stats: Stats):
    try:
        import openpyxl                      # noqa
        return openpyxl
    except ImportError:
        if 'openpyxl' not in stats.缺库:
            stats.缺库.append('openpyxl')
        return None


def try_docx(stats: Stats):
    try:
        import docx                          # noqa
        return docx
    except ImportError:
        if 'python-docx' not in stats.缺库:
            stats.缺库.append('python-docx')
        return None


def parse_xlsx(path: str, 仅元信息: bool, stats: Stats) -> tuple[str, list[str], str] | None:
    mod = try_openpyxl(stats)
    if mod is None:
        return None
    wb = mod.load_workbook(path, read_only=True, data_only=True)
    try:
        sheets = list(wb.sheetnames)
        lines: list[str] = []
        for name in sheets:
            ws = wb[name]
            if 仅元信息:
                lines.append('工作表「%s」（%s 行 × %s 列，仅登记结构）' % (name, ws.max_row, ws.max_column))
                continue
            lines.append('工作表「%s」（%s 行 × %s 列）' % (name, ws.max_row, ws.max_column))
            got = 0
            for row in ws.iter_rows(max_row=25, max_col=10, values_only=True):
                vals = ['' if v is None else str(v).strip() for v in row]
                if any(vals):
                    lines.append(' | '.join(v for v in vals if v))
                    got += 1
                if got >= 20:
                    break
        summary = '；'.join(lines[:6])
        return summary, lines, '工作表 ' + '/'.join('「%s」' % s for s in sheets[:6])
    finally:
        wb.close()


def parse_docx(path: str, 仅元信息: bool, stats: Stats) -> tuple[str, list[str], str] | None:
    mod = try_docx(stats)
    if mod is None:
        return None
    doc = mod.Document(path)
    paras = [p.text.strip() for p in doc.paragraphs]
    lines = [p for p in paras if p]
    if 仅元信息:
        return '共 %d 段（仅登记结构，未抄正文）' % len(lines), [], '段落 1-%d' % len(lines)
    summary = ' '.join(lines[:8])
    return summary, lines, '段落 1-%d' % min(len(lines), 8)


# --------------------------------------------------------------------------
# 条目构造
# --------------------------------------------------------------------------
def make_record(src: dict, path: str, 分类: str, 名称: str, 时间: str, 角色: str,
                成果: str, 证据定位: str, lines: list[str], 采集方式: str,
                扫描模式: str, 状态: str | None = None, 量化值: list | None = None,
                norm: list[str] | None = None, 冲突组: str = '',
                场景: str = '', 场景警示: str = '', 主源裁决: str = '') -> dict:
    盘 = src.get('盘', '')
    rec = {
        '分类': 分类 if 分类 in CATEGORIES else src.get('默认分类', '项目经历'),
        '名称': (名称 or '').strip()[:180],
        '时间': (时间 or '').strip()[:80],
        '角色': (角色 or '').strip()[:120],
        '成果': truncate(成果),
        '来源盘': 盘,
        '来源工作区': src.get('工作区', ''),
        '源清单': src.get('名称', ''),
        '证据路径': [path],
        '证据定位': 证据定位 or '',
        '采集方式': 采集方式,
        '扫描模式': 扫描模式,
        # 冲突组：同一事实有多个主源时的分组名（如「简历多源」）。
        # 采集器只标组、不选边；裁决由人在 待核验队列.md 底部记录。
        # 按场景归类后不再以「状态 == 主源冲突」为条件：多源按场景并存、状态回到待核验
        # 组名仍要留着当场景分组身份（也是回归时比对“哪些条属同组”的钩子）。
        '主源冲突组': (冲突组 or ''),
    }
    # 场景字段只在非空时写入 rec：无条件写会让全库每条的 record_hash 都变，
    # 重跑时平白追加几百行「版本 2」，把 append-only 的历史撑肿。
    if 场景:
        rec['简历场景'] = 场景
    if 场景警示:
        rec['场景警示'] = 场景警示
    if 主源裁决:
        rec['主源裁决'] = 主源裁决
    if 量化值 is None:
        量化值 = extract_quants(rec['成果'], lines, rec['时间'], norm)
    rec['量化值'] = 量化值
    # 无源标记独立于状态：状态可以被显式传入的「主源冲突」「需OCR人工录入」占用，
    # 但一条记录里若有定位不到的数字，这个事实不能因此被盖掉。所以 无源量化值
    # 一律写全（不看状态），状态只在未显式指定时才按有无无源数字来定。
    rec['无源量化值'] = [q.get('值', '') for q in 量化值 if not q.get('已溯源')]
    if 状态 is None:
        状态 = STATUS_NOSOURCE if rec['无源量化值'] else STATUS_PENDING
    rec['状态'] = 状态
    return rec


# --------------------------------------------------------------------------
# 各提取器：一个文件 → 0..N 条记录
# --------------------------------------------------------------------------
def extract_resume_tex(src: dict, path: str, scan_mode: str, stats: Stats) -> list[dict]:
    text = read_text_file(path)
    lines = text.split('\n')
    fname = os.path.basename(path)
    conflict_files = set(src.get('主源冲突文件') or [])
    is_conflict = fname in conflict_files
    冲突组 = src.get('主源冲突组', '') if is_conflict else ''
    # 多简历源不选唯一权威源，按场景并存（人工裁决）。
    # 场景映射写在 sources.json（人工决策落人工配置文件），采集器只读取、不推断。
    场景 = ((src.get('简历场景映射') or {}).get(fname, '')) if is_conflict else ''
    主源裁决 = (src.get('主源裁决', '') if 场景 else '')
    # 安全阀：文件在 主源冲突文件 里、但 简历场景映射 漏配了这个文件名时，
    # 仍标「主源冲突」（未裁决），避免映射漏配导致冲突标记被静默丢掉。
    冲突状态 = (STATUS_CONFLICT if (is_conflict and not 场景) else None)

    def 场景警示(文本: str) -> str:
        """按源配置的场景警示规则给本场景条目打警示标签，无规则一律不打。"""
        return scene_warning(场景, 文本, src.get('场景警示规则'))

    采集方式 = '全量扫描'
    out: list[dict] = []

    if path.endswith('.tex'):
        parsed = parse_resume_tex(text)
        tex_norm = norm_lines(lines, 'tex')
        散段合并: dict[str, list] = {}
        for e in parsed:
            节 = (e.get('节') or '').strip()
            节显示 = (e.get('节原文') or 节).strip()
            分类 = SECTION_CATEGORY.get(节, src.get('默认分类', '实习经历'))
            要点文本 = [t for t, _ in e['要点']]
            if e.get('散段') or not e['名称']:
                # 无 \resumeentry 的整块（技能、教育背景常见）：按节合并成一条
                if 节 in SKIP_SECTIONS:
                    continue
                bucket = 散段合并.setdefault(
                    节, {'要点': [], '起行': e['起行'], '分类': 分类, '节显示': 节显示})
                bucket['要点'].extend([(t, ln) for t, ln in e['要点']])
                bucket['起行'] = min(bucket['起行'], e['起行'])
                continue
            成果 = '；'.join(要点文本) if 要点文本 else ''
            名称 = e['名称']
            if not 成果 and not 名称:
                continue
            # 条目级定位：指向整条经历在 tex 里的行区间。
            # 与量化值的数字级行号是两回事，不得共用变量名互相覆盖。
            定位 = '节「%s」L%d-L%d' % (节显示 or '未命名', e['起行'], e['止行'])
            角色 = e['角色']
            if e['地点']:
                角色 = (角色 + ' · ' + e['地点']).strip(' ·')
            时间 = e['时间']
            量化 = quants_from_points(e['要点'], lines, tex_norm, 时间)
            out.append(make_record(src, path, 分类, 名称, 时间, 角色, 成果, 定位,
                                   lines, 采集方式, scan_mode,
                                   状态=冲突状态,
                                   量化值=量化, 冲突组=冲突组, 场景=场景,
                                   场景警示=场景警示('%s %s' % (名称, 成果)),
                                   主源裁决=主源裁决))
        for 节, bucket in 散段合并.items():
            要点文本 = [t for t, _ in bucket['要点']]
            if not 要点文本:
                continue
            止行 = max([ln for _, ln in bucket['要点']] or [bucket['起行']])
            名称 = '%s（%s）' % (SECTION_CATEGORY.get(节, '简历条目'), os.path.splitext(fname)[0])
            定位 = '节「%s」L%d-L%d' % (bucket.get('节显示') or 节 or '未命名',
                                     bucket['起行'], 止行)
            量化 = quants_from_points(bucket['要点'], lines, tex_norm)
            out.append(make_record(src, path, bucket['分类'], 名称, '', '',
                                   '；'.join(要点文本), 定位, lines, 采集方式, scan_mode,
                                   状态=冲突状态,
                                   量化值=量化, 冲突组=冲突组, 场景=场景,
                                   场景警示=场景警示('%s %s' % (名称, '；'.join(要点文本))),
                                   主源裁决=主源裁决))
        return out

    # 非 tex（如经历补充 md）：走通用 md 抽取
    return extract_doc_auto(src, path, scan_mode, stats)


def extract_skill_registry(src: dict, path: str, scan_mode: str, stats: Stats) -> list[dict]:
    """一个 SKILL.md → 一条技能资产条目（只登记名称与一句话定位）。"""
    text = read_text_file(path)
    fm, body, offset = parse_frontmatter(text)
    name = fm.get('name') or os.path.basename(os.path.dirname(path))
    desc = fm.get('description') or ''
    if not desc:
        m = re.search(r'^description\s*:\s*(.+)$', text, re.M)
        desc = m.group(1).strip() if m else ''
    # 一句话定位：description 首句
    首句 = re.split(r'(?<=[。.])\s+', desc.strip())[0] if desc.strip() else ''
    定位 = 'SKILL.md frontmatter L%d-L%d' % (offset - 1, min(offset + 3, len(text.split('\n'))))
    lines = text.split('\n')
    rec = make_record(src, path, '技能资产', name, '', 'Agent Skill 技能包',
                      truncate(首句 or desc, 200) or '（SKILL.md 无 description）',
                      定位, lines, '全量扫描', scan_mode, 量化值=[])
    return [rec]


def extract_readme_oneliner(src: dict, path: str, scan_mode: str, stats: Stats) -> list[dict]:
    """代码项目 README → 一条项目经历条目，只取一句话定位。"""
    text = read_text_file(path)
    lines = text.split('\n')
    项目名 = os.path.basename(os.path.dirname(path))
    fallback = 项目名
    title, summary, 定位, _ = md_title_and_summary(text, fallback)
    if title and title.lower() not in ('readme', 'readme.md'):
        名称 = '%s（%s）' % (项目名, title)
    else:
        名称 = 项目名
    rec = make_record(src, path, src.get('默认分类', '项目经历'), 名称, '',
                      '代码项目定位', truncate(summary, 200), 定位, lines,
                      '全量扫描', scan_mode)
    return [rec]


def extract_doc_auto(src: dict, path: str, scan_mode: str, stats: Stats) -> list[dict]:
    ext = os.path.splitext(path)[1].lower()
    默认分类 = src.get('默认分类', '项目经历')
    仅元信息 = bool(src.get('仅元信息'))
    stem = os.path.splitext(os.path.basename(path))[0]
    lines: list[str] = []

    # --- 二进制与扫描件：只登记路径与元信息 ---
    if ext in ('.pdf', '.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff',
               '.doc', '.ppt', '.pptx', '.xls', '.xlsm', '.key', '.pages', '.numbers'):
        stats.只登记路径 += 1
        stats.跳过二进制 += 1
        size = os.path.getsize(path)
        成果 = ('%s 文件，%.1f KB，v1 不解析内容，只登记路径与元信息，待 OCR 或人工录入。'
                % (ext.lstrip('.').upper(), size / 1024))
        rec = make_record(src, path, 默认分类, stem, '', '', 成果,
                          '（二进制，无文本定位）', lines, '全量扫描', scan_mode,
                          状态=STATUS_NEEDOCR, 量化值=[])
        return [rec]

    # --- xlsx / docx：二进制容器，给不出源文本行号，故不抽量化值（数字核验留给人工）---
    if ext in ('.xlsx', '.docx'):
        got = (parse_xlsx if ext == '.xlsx' else parse_docx)(path, 仅元信息, stats)
        if got is None:
            stats.跳过二进制 += 1
            return []
        summary, _lines, 定位 = got
        rec = make_record(src, path, 默认分类, stem, '', '',
                          truncate(summary, 400), 定位, [], '全量扫描', scan_mode,
                          量化值=[])
        return [rec]

    # --- html ---
    if ext in ('.html', '.htm'):
        text = read_text_file(path)
        lines = text.split('\n')          # 用原始文件行做数字定位，行号才是真的
        title, headings, blocks = html_to_parts(text)
        名称 = title or (headings[0][1] if headings else stem)
        子标题 = [h[1] for h in headings[:6]]
        首段 = blocks[0][0] if blocks else ''
        成果 = 首段
        if 子标题:
            成果 = (首段 + ' 章节：' + ' / '.join(子标题)).strip()
        # HTML 不做行级定位，用标题名定位
        定位 = 'HTML <title>「%s」' % 名称 if title else 'HTML 首个 <h1>「%s」' % 名称
        时间 = date_from_name(stem)
        rec = make_record(src, path, 默认分类, truncate(名称, 120), 时间, '',
                          truncate(成果, 400), 定位, lines, '全量扫描', scan_mode,
                          norm=norm_lines(lines, 'html'))
        return [rec]

    # --- tex（非简历） ---
    if ext == '.tex':
        text = read_text_file(path)
        raw_lines = text.split('\n')
        mt = re.search(r'\\title\{((?:[^{}]|\{[^{}]*\})*)\}', text)
        名称 = latex_inline_to_text(mt.group(1)) if mt else stem
        lines = raw_lines
        # 只取 \begin{document} 之后的正文（与 parse_resume_tex 一致）。
        # preamble 里的 \usepackage[AutoFakeBold=2.5]、\hypersetup{colorlinks=true}、
        # \newcommand{...}{#1#2%}、\begin{itemize}[parsep=0pt] 都不是事实内容，
        # 混进成果只会产生 LaTeX 参数垃圾与假量化值。
        起_idx = 0
        for i, line in enumerate(raw_lines):
            if '\\begin{document}' in line:
                起_idx = i + 1
                break
        body = []
        for i, line in enumerate(raw_lines[起_idx:], start=起_idx + 1):
            if re.match(r'^\s*%', line):
                continue
            if '\\newcommand' in line or '\\newenvironment' in line or '#1' in line or '#2' in line:
                continue
            if re.match(r'^\s*\\(usepackage|geometry|setlength|definecolor|setmain|pagestyle|'
                        r'hypersetup|fontspec|setCJK|linespread|begin\{document\}|end\{document\}|'
                        r'maketitle|vspace|hrule|noindent)', line):
                continue
            cleaned = latex_inline_to_text(re.sub(r'^\s*\\item\s*', '', line))
            # 环境可选参数残留（如 [left=1.2em, parsep=0pt]）不是正文
            if not cleaned or re.fullmatch(r'\[[^\]]*\]', cleaned.strip()):
                continue
            if len(cleaned) >= 4:
                body.append((cleaned, i))
        正文 = [b for b, _ in body]
        成果 = ' '.join(正文[:6])
        起 = body[0][1] if body else 1
        止 = body[min(5, len(body) - 1)][1] if body else 1
        定位 = '正文 L%d-L%d' % (起, 止)
        rec = make_record(src, path, 默认分类, truncate(名称, 120), date_from_name(stem),
                          '', truncate(成果, 400), 定位, lines, '全量扫描', scan_mode,
                          norm=norm_lines(lines, 'tex'))
        return [rec]

    # --- md / txt（默认） ---
    text = read_text_file(path)
    lines = text.split('\n')
    fm, _body, _off = parse_frontmatter(text)
    title, summary, 定位, _ = md_title_and_summary(text, stem)
    分类 = 默认分类
    fm_cat = (fm.get('分类') or '').strip()
    if fm_cat in CATEGORIES:
        分类 = fm_cat
    时间 = date_from_name(stem) or (fm.get('年份', '') and '%s 年' % fm.get('年份') or '')
    角色 = ''
    领域 = fm.get('核心领域') or fm.get('文档类型')
    if 领域:
        角色 = str(领域)
    成果 = summary
    小节 = [re.sub(r'^#{2,3}\s+', '', l).strip() for l in lines
            if re.match(r'^#{2,3}\s+', l)][:5]
    if 小节:
        成果 = (summary + ' 小节：' + ' / '.join(小节)).strip()
    rec = make_record(src, path, 分类, truncate(title, 120), 时间, 角色,
                      truncate(成果, 400), 定位, lines, '全量扫描', scan_mode)
    return [rec]


# 文件名里的日期。MinerU 一类导出文件的名字带雪花 ID
# （MinerU_markdown_2069230004568154112），旧写法只按
# 「(20\d{2}) 可选分隔 (\d{1,2}) 可选分隔 (\d{1,2})」硬切，会从 ID 中段截出
# 2069-23-00 这类不可能的假日期；2026-1-009_... 也会被切成 2026-01-00。
# 三重守卫：
#   ① 候选段前后不允许紧邻数字——否则它只是长数字串（雪花 ID、编号）的中段；
#   ② 年份后无分隔符时只认 YYYYMM / YYYYMMDD 紧凑日期，其余形态不认；
#   ③ 月 1–12、日 1–31 且不超过该月真实天数（calendar 校验，拒 2026-02-30）。
# 逐候选校验，全不合法则该条时间字段留空，绝不硬塞一个不可能的日期。
_DATE_RE = re.compile(r'(20\d{2})([-_年./]?)(\d{1,2})?([-_月./]?)(\d{1,2})?')
_COMPACT_DATE_LENS = (6, 8)          # 无分隔符时只认 YYYYMM 与 YYYYMMDD


def _valid_ymd(y: int, mo: int | None, d: int | None) -> bool:
    """月 1–12、日 1–31 且不超过该月真实天数；不可能的日期一律判假。"""
    if mo is not None and not 1 <= mo <= 12:
        return False
    if d is not None:
        if not 1 <= d <= 31:
            return False
        if mo is not None and d > calendar.monthrange(y, mo)[1]:
            return False
    return True


def date_from_name(stem: str) -> str:
    for m in _DATE_RE.finditer(stem):
        y_text, sep1, mo_text, sep2, d_text = m.groups()
        # 守卫①：前后紧邻数字 → 长数字串的尾巴，不是日期
        if m.start() > 0 and stem[m.start() - 1].isdigit():
            continue
        if m.end() < len(stem) and stem[m.end()].isdigit():
            continue
        # 守卫②：年份紧贴月份（无分隔符）时，只认 6 位或 8 位紧凑日期
        if not sep1 and mo_text is not None:
            if sep2 and d_text:
                continue                     # 年月相连、月日又断开（20261_2）：形态不一致
            n = len(y_text) + len(mo_text) + len(d_text or '')
            if n not in _COMPACT_DATE_LENS or (n == 6 and d_text):
                continue
        # 守卫③：范围与真实天数校验
        mo = int(mo_text) if mo_text else None
        d = int(d_text) if d_text else None
        if not _valid_ymd(int(y_text), mo, d):
            continue
        if mo and d:
            return '%s-%02d-%02d' % (y_text, mo, d)
        if mo:
            return '%s-%02d' % (y_text, mo)
        return '%s 年' % y_text
    return ''                                # 无合法候选：时间留空，不硬塞


# 提取器注册表。dedup_key 的分治口径依赖此处的产条数不变量：
#   resume_tex —— 处理 .tex 简历、可产多条（分节），key 保留「名称」作节内判别位；
#   其余（skill_registry / readme_oneliner / doc_auto）—— 非 tex、恒产 0 或 1 条，
#     故 dedup_key 走「一文件一条·路径即身份」（名称不入 key，改名走版本+1）。
# 纪律：若将来新增让「非 tex」文件产多条的提取器，必须同步改 dedup_key 与
#   regression_guard 的 G4，否则多条会挤进同一 key、互相覆盖版本。
EXTRACTORS = {
    'resume_tex': extract_resume_tex,
    'skill_registry': extract_skill_registry,
    'readme_oneliner': extract_readme_oneliner,
    'doc_auto': extract_doc_auto,
}


# --------------------------------------------------------------------------
# 遍历
# --------------------------------------------------------------------------
# 模板与占位文件识别。文件名带「模板/demo/template/占位」只是初筛，不能单凭它
# 就整类排除——真成果也会带这些词：「…研究汇报_v2_模板.html」是用模板做的真汇报，
# 「模板选型执行说明」里模板是主题词，「demo进度记录」里 demo 是阶段名，
# 「prompt-templates」模板集合本身就是产出。所以分两路判：
#   能读到正文的文本文件——还要求正文里的排版占位符（X.X、20XX、XXXX、{{}}、
#   Lorem ipsum、example@）同时达到命中数与密度双阈值，才判为「等着被填空的模板」；
#   只登记路径的二进制/扫描件——读不到正文，只认强特征文件名（模板词收尾或紧接 demo）。
TEMPLATE_NAME_RE = re.compile(r'模板|模版|占位|placeholder|template|demo', re.I)
TEMPLATE_STRONG_RE = re.compile(
    r'(?:模板|模版|占位|placeholder|template)(?:[-_ ]?demo)?$|demo$', re.I)
PLACEHOLDER_RE = re.compile(
    r'X{2,}|X\.X|\{\{[^{}]*\}\}|Lorem\s+ipsum|example@|<占位|placeholder|'
    r'【[^】]{0,12}(?:填写|补充|替换)[^】]{0,12}】|此处填|待填写|your[_-]?(?:name|email)',
    re.I)
PLACEHOLDER_MIN_HITS = 12        # 占位符命中数下限
PLACEHOLDER_MIN_RATIO = 0.005    # 命中数 / 正文字符数 下限（真模板约 2%，规范文档约 0.15%）
TEMPLATE_PEEK_BYTES = 200_000    # 判模板时最多读入的正文字节数


def _template_stem(name: str) -> str:
    """取文件名主干：剥掉扩展名、尾部版本号与同步副本后缀。"""
    stem = os.path.splitext(name)[0]
    stem = re.sub(r'[-_ ]v?\d+(?:\.\d+)*$', '', stem)     # _v2 / -v3.1
    stem = re.sub(r'\(\d+\)$', '', stem)                   # (1) 重复副本
    return stem.strip(' -_')


def is_template_file(path: str, name: str, 是二进制: bool, stats: Stats) -> bool:
    """判定是否为「等着被填空的排版模板」。判不准时一律返回 False（fail-open）。"""
    stem = _template_stem(name)
    if not TEMPLATE_NAME_RE.search(stem):
        return False
    if 是二进制:
        return bool(TEMPLATE_STRONG_RE.search(stem))   # 读不到正文，只认强特征
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read(TEMPLATE_PEEK_BYTES)
    except OSError as e:
        stats.记异常('模板判定', path, e)
        return False                                   # 读不了就交回正常解析
    hits = len(PLACEHOLDER_RE.findall(text))
    return (hits >= PLACEHOLDER_MIN_HITS
            and hits >= len(text) * PLACEHOLDER_MIN_RATIO)


def is_dup_copy(name: str, pattern: str) -> bool:
    stem = os.path.splitext(name)[0]
    try:
        return bool(re.search(pattern, stem))
    except re.error:
        return False


def iter_source_files(src: dict, cfg: dict, stats: Stats, seen: set):
    """在白名单源根内定点下钻，产出待处理文件路径。"""
    gskip = cfg.get('全局跳过', {})
    skip_dirs = set(gskip.get('目录名') or [])
    skip_exts = set(x.lower() for x in (gskip.get('扩展名') or []))
    skip_names = set(gskip.get('文件名') or [])
    dup_re = gskip.get('重复副本正则', '')
    dup_on = bool(gskip.get('跳过重复副本'))
    max_mb_global = float(gskip.get('单文件上限MB', 20))

    root = src.get('根', '')
    if not root:
        return
    if not os.path.isabs(root):
        root = os.path.join(BASE_DIR, root)   # 相对路径按仓库根解析，demo 配置开箱即用
    最大深度 = int(src.get('最大深度', 1))
    排除目录 = set(src.get('排除目录') or [])
    允许扩展 = set(x.lower() for x in (src.get('扩展名') or []))
    登记扩展 = set(x.lower() for x in (src.get('登记扩展名') or []))
    仅文件名 = set(src.get('仅文件名') or [])
    max_mb = float(src.get('单文件上限MB') or max_mb_global)

    if os.path.isfile(root):
        yield root
        return
    if not os.path.isdir(root):
        return

    子路径 = src.get('子路径') or ['']
    for sp in 子路径:
        base = os.path.join(root, sp) if sp else root
        if not os.path.isdir(base):
            continue
        stack = [(base, 0)]
        while stack:
            cur, depth = stack.pop()
            try:
                entries = list(os.scandir(cur))
            except OSError as e:
                stats.记异常(src.get('名称', ''), cur, e)
                continue
            for ent in sorted(entries, key=lambda e: e.name):
                name = ent.name
                if ent.is_dir(follow_symlinks=False):
                    if name in skip_dirs or name in 排除目录:
                        if name in ('node_modules', '.venv', 'venv', '__pycache__',
                                    '.pytest_cache', '.git'):
                            stats.跳过依赖目录 += 1
                        elif name == '99_归档':
                            stats.跳过归档目录 += 1
                        continue
                    if name.startswith('.') and name not in ('.github',):
                        continue
                    if depth + 1 < 最大深度:
                        stack.append((ent.path, depth + 1))
                    continue
                if not ent.is_file(follow_symlinks=False):
                    continue
                if ent.path in seen:
                    continue
                seen.add(ent.path)
                ext = os.path.splitext(name)[1].lower()
                是二进制 = ext in BINARY_EXTS
                if name in skip_names or name.startswith('._') or ext in skip_exts:
                    if ext in ('.zip', '.rar', '.7z', '.gz', '.tar', '.bz2'):
                        stats.跳过压缩包 += 1
                    if 是二进制:
                        stats.跳过二进制 += 1
                    continue
                if dup_on and dup_re and is_dup_copy(name, dup_re):
                    stats.跳过重复副本 += 1
                    if 是二进制:
                        stats.跳过二进制 += 1
                    continue
                if 仅文件名 and name not in 仅文件名:
                    continue
                if ext not in 允许扩展 and ext not in 登记扩展:
                    if 是二进制:
                        stats.跳过其他类型 += 1
                        stats.跳过二进制 += 1
                    continue
                try:
                    size = ent.stat().st_size
                except OSError as e:
                    stats.记异常(src.get('名称', ''), ent.path, e)
                    continue
                if size > max_mb * 1024 * 1024:
                    stats.跳过超大文件 += 1
                    if 是二进制:
                        stats.跳过二进制 += 1
                    continue
                if size == 0 and ext not in 登记扩展:
                    continue
                # 模板与占位文件整类跳过：模板里的 (86)1XX-XXXX-XXXX、20XX 学年、
                # GPA: X.X/X.X 是排版占位符，不是本人事实，纳入只会污染主库。
                if is_template_file(ent.path, name, 是二进制, stats):
                    stats.跳过模板文件 += 1
                    if 是二进制:
                        stats.跳过二进制 += 1
                    continue
                yield ent.path


# --------------------------------------------------------------------------
# 视图 / 队列 / 日志 生成
# --------------------------------------------------------------------------
def load_latest(jsonl_path: str) -> list[dict]:
    latest: dict[str, dict] = {}
    order: list[str] = []
    try:
        with open(jsonl_path, 'r', encoding='utf-8') as f:
            for ln, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                rid = rec.get('id') or 'L%d' % ln
                if rid not in latest:
                    order.append(rid)
                latest[rid] = rec
    except OSError:
        return []
    return [latest[r] for r in order]


def quant_cell(rec: dict) -> str:
    qs = rec.get('量化值') or []
    if not qs:
        return '—'
    parts = []
    for q in qs[:4]:
        mark = '' if q.get('已溯源') else '⚠无源'
        parts.append('%s%s' % (q.get('值', ''), mark))
    if len(qs) > 4:
        parts.append('等 %d 项' % len(qs))
    return '；'.join(parts)


def scene_cell(rec: dict) -> str:
    """场景列：`简历场景`，带警示时接 `⚠警示`；未参与场景归类的条目显示 —。"""
    场景 = rec.get('简历场景') or ''
    if not 场景:
        return '—'
    警示 = rec.get('场景警示') or ''
    return cell(场景 + ('　⚠' + 警示 if 警示 else ''))


def write_category_views(records: list[dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    by_cat = {c: [] for c in CATEGORIES}
    for r in records:
        by_cat.setdefault(r.get('分类', '项目经历'), []).append(r)
    for cat in CATEGORIES:
        rows = by_cat.get(cat, [])
        状态计数: dict[str, int] = {}
        for r in rows:
            状态计数[r.get('状态', STATUS_PENDING)] = 状态计数.get(r.get('状态', STATUS_PENDING), 0) + 1
        out = ['# %s' % cat, '',
               '> **生成时间**: %s' % now_iso(),
               '> **数据来源**: `中台数据/personal_info.jsonl`（由 `collector/collect.py` 生成，请勿手改）',
               '> **条目数**: %d' % len(rows), '',
               '---', '', '## 状态统计', '',
               '| 状态 | 条目数 |', '|------|--------|']
        if 状态计数:
            for k in [STATUS_PENDING, STATUS_CONFLICT, STATUS_NOSOURCE, STATUS_NEEDOCR, STATUS_VERIFIED]:
                if k in 状态计数:
                    out.append('| %s | %d |' % (k, 状态计数[k]))
            for k, v in sorted(状态计数.items()):
                if k not in (STATUS_PENDING, STATUS_CONFLICT, STATUS_NOSOURCE,
                             STATUS_NEEDOCR, STATUS_VERIFIED):
                    out.append('| %s | %d |' % (k, v))
        else:
            out.append('| （暂无） | 0 |')
        out += ['', '---', '', '## 条目明细', '',
                '| # | id | 名称 | 时间 | 角色 | 成果摘要 | 量化值 | 来源盘 | 证据路径 | 证据定位 | 状态 | 场景/警示 |',
                '|---|----|------|------|------|----------|--------|--------|----------|----------|------|-----------|']
        if rows:
            for i, r in enumerate(rows, start=1):
                out.append('| %d | %s | %s | %s | %s | %s | %s | %s | `%s` | %s | %s | %s |' % (
                    i, cell(r.get('id')), cell(r.get('名称')), cell(r.get('时间') or '—'),
                    cell(r.get('角色') or '—'), cell(truncate(r.get('成果'), VIEW_LEN)), quant_cell(r),
                    cell(r.get('来源盘')), cell(short_path((r.get('证据路径') or [''])[0])),
                    cell(r.get('证据定位')), cell(r.get('状态')), scene_cell(r)))
        else:
            out.append('| — | — | 本分类暂无条目 | — | — | — | — | — | — | — | — | — |')
        out += ['', '---', '',
                '> 规则说明：本视图由 jsonl 生成，不手改；改数据请改 `personal_info.jsonl` 后重跑采集器。',
                '> 「⚠无源」标记表示该数字在源文本定位不到，它同时被记进条目的 `无源量化值` 字段'
                '（状态被「主源冲突」「需OCR人工录入」占用时该标记仍在），不得直接写进简历。',
                '> 「场景/警示」列来自 sources.json 的人工场景映射：同一事实的多个主源按场景并存，'
                '不选唯一权威源；带 ⚠ 的条目命中了源配置的「场景警示规则」，仅限对应场景使用。'
                '场景归类只定主源归属，不裁决内容真伪，所以这些条目状态仍为「待核验」。', '']
        with open(os.path.join(DATA_DIR, '%s.md' % cat), 'w', encoding='utf-8') as f:
            f.write('\n'.join(out))


JUDGE_MARK = '## 人工裁决记录'


def write_queue(records: list[dict]) -> None:
    # 保留既有人工裁决区
    裁决区 = ''
    try:
        with open(QUEUE_PATH, 'r', encoding='utf-8') as f:
            old = f.read()
        i = old.find(JUDGE_MARK)
        if i >= 0:
            裁决区 = old[i:].rstrip() + '\n'
    except OSError:
        pass
    if not 裁决区:
        裁决区 = (JUDGE_MARK + '（只追加，生成器不覆盖本区）\n\n'
                  '> 格式：`- [日期] id=PI-XXXX 裁决：{结论} → 状态改为 {新状态}`。\n'
                  '> 裁决后把对应 `id` 的新版本行追加进 `personal_info.jsonl`，旧行不动。\n\n（暂无）\n')

    冲突 = [r for r in records if r.get('状态') == STATUS_CONFLICT]
    # 无源区按「无源量化值」标记取，不再只看状态：状态被主源冲突或需OCR 占用的条目
    # 若同时含无源数字，也必须在这里露出来（两个区可能重叠，头部总数按 id 去重）。
    无源 = [r for r in records if r.get('状态') == STATUS_NOSOURCE or r.get('无源量化值')]
    ocr = [r for r in records if r.get('状态') == STATUS_NEEDOCR]
    待核 = [r for r in records if r.get('状态') == STATUS_PENDING]
    待处理 = {r.get('id') for r in 冲突 + 无源 + ocr + 待核}
    # 场景归类条目：状态已回到待核验（仍在第四节），这里单独列一张表，
    # 让人工核验时能直接看到「这条归哪个场景、能不能往求职简历里写」。
    场景条目 = [r for r in records if r.get('简历场景')]
    警示条目 = [r for r in 场景条目 if r.get('场景警示')]

    out = ['# 待核验队列', '',
           '> **生成时间**: %s' % now_iso(),
           '> **数据来源**: `中台数据/personal_info.jsonl`（上半部由 `collector/collect.py` 生成，请勿手改）',
           '> **待处理条目数**: %d（主源冲突 %d + 无源待确认 %d + 需OCR %d + 待核验 %d；按 id 去重，'
           '含无源数字的条目会同时出现在本区与它的状态区）'
           % (len(待处理), len(冲突), len(无源), len(ocr), len(待核)),
           '> **已按场景归类（状态仍为待核验）**: %d（其中带场景警示 %d 条）'
           % (len(场景条目), len(警示条目)),
           '',
           '本队列收四类候选：主源冲突项（同一事实在多个主源表述不一致）、无源待确认项（数字在源文本定位不到）、'
           '需 OCR 人工录入项（PDF / 扫描件只登记了路径）、待核验项（文本源已解析，等人过目）。',
           '核验结论写进文件底部的「人工裁决记录」区，该区只追加、生成器不覆盖。',
           '', '---', '',
           '## 一、主源冲突（需裁决，采集器不自动选边）', '',
           '按冲突组排序，同组条目互为备选一；裁决结果写底部「人工裁决记录」区。', '',
           '| # | id | 冲突组 | 分类 | 名称 | 冲突源 | 证据定位 |',
           '|---|----|--------|------|------|--------|----------|']
    if 冲突:
        排序 = sorted(冲突, key=lambda r: (r.get('主源冲突组') or '', r.get('分类') or '', r.get('id') or ''))
        for i, r in enumerate(排序, start=1):
            out.append('| %d | %s | %s | %s | %s | `%s` | %s |' % (
                i, cell(r.get('id')), cell(r.get('主源冲突组') or '—'),
                cell(r.get('分类')), cell(r.get('名称')),
                cell(short_path((r.get('证据路径') or [''])[0])),
                cell(r.get('证据定位'))))
    else:
        out.append('| — | — | — | — | 暂无主源冲突项 | — | — |')

    if 场景条目:
        out += ['', '### 已裁决：多主源按场景并存', '',
                '> 人工裁决多源并存、按场景归类、不选唯一权威源。本裁决只定「主源归属哪个场景」，',
                '> **不裁决内容真伪** —— 下面这些条目状态仍是「待核验」，仍会出现在第四节等人过目。',
                '> 带警示标签的条目命中了源配置的「场景警示规则」（如保研场景的排名/成绩类），',
                '> 这类表述仅限对应场景使用。场景映射与警示规则存于 `collector/sources.json`，改口径先改那里。', '',
                '| # | id | 简历场景 | 分类 | 名称 | 来源文件 | 场景警示 |',
                '|---|----|----------|------|------|----------|----------|']
        场景排序 = sorted(场景条目, key=lambda r: (r.get('简历场景') or '',
                                       r.get('分类') or '', r.get('id') or ''))
        for i, r in enumerate(场景排序, start=1):
            out.append('| %d | %s | %s | %s | %s | `%s` | %s |' % (
                i, cell(r.get('id')), cell(r.get('简历场景')), cell(r.get('分类')),
                cell(r.get('名称')),
                cell(short_path((r.get('证据路径') or [''])[0])),
                cell(r.get('场景警示') or '—')))

    out += ['', '---', '', '## 二、无源待确认（数字或事实缺证据）', '',
            '| # | id | 分类 | 名称 | 缺证内容 | 证据路径 |',
            '|---|----|------|------|----------|----------|']
    if 无源:
        for i, r in enumerate(无源, start=1):
            miss = '；'.join(r.get('无源量化值') or []) or \
                '；'.join(q.get('值', '') for q in (r.get('量化值') or []) if not q.get('已溯源'))
            out.append('| %d | %s | %s | %s | %s | `%s` |' % (
                i, cell(r.get('id')), cell(r.get('分类')), cell(r.get('名称')),
                cell(miss or '整条无文件证据'),
                cell(short_path((r.get('证据路径') or [''])[0]))))
    else:
        out.append('| — | — | — | 暂无无源项 | — | — |')

    out += ['', '---', '', '## 三、需 OCR / 人工录入（二进制只登记路径）', '',
            '| # | id | 分类 | 名称 | 文件类型 | 证据路径 |',
            '|---|----|------|------|----------|----------|']
    if ocr:
        for i, r in enumerate(ocr, start=1):
            p = (r.get('证据路径') or [''])[0]
            out.append('| %d | %s | %s | %s | %s | `%s` |' % (
                i, cell(r.get('id')), cell(r.get('分类')), cell(r.get('名称')),
                cell(os.path.splitext(p)[1].lstrip('.').upper() or '—'), cell(short_path(p))))
    else:
        out.append('| — | — | — | 暂无二进制登记项 | — | — |')

    out += ['', '---', '', '## 四、待核验（文本源已解析，等人过目）', '',
            '| # | id | 分类 | 名称 | 时间 | 证据路径 |',
            '|---|----|------|------|------|----------|']
    if 待核:
        for i, r in enumerate(待核, start=1):
            out.append('| %d | %s | %s | %s | %s | `%s` |' % (
                i, cell(r.get('id')), cell(r.get('分类')), cell(r.get('名称')),
                cell(r.get('时间') or '—'),
                cell(short_path((r.get('证据路径') or [''])[0]))))
    else:
        out.append('| — | — | — | 暂无待核验项 | — | — |')

    out += ['', '---', '']
    text = '\n'.join(out) + 裁决区
    with open(QUEUE_PATH, 'w', encoding='utf-8') as f:
        f.write(text)


_LOG_HEAD_RE = re.compile(r'^##\s*采集\s*#(\d+)', re.M)


def next_log_no() -> int:
    try:
        with open(LOG_PATH, 'r', encoding='utf-8') as f:
            old = f.read()
    except OSError:
        return 1
    nums = [int(m) for m in _LOG_HEAD_RE.findall(old)]
    return (max(nums) + 1) if nums else 1


def write_log(no: int, cfg: dict, scan_mode: str, stats: Stats,
              records: list[dict], 源清单条数: int, dry: bool) -> str:
    库存: dict[str, int] = {}
    for r in records:
        库存[r.get('状态', STATUS_PENDING)] = 库存.get(r.get('状态', STATUS_PENDING), 0) + 1
    分类库存: dict[str, int] = {}
    for r in records:
        分类库存[r.get('分类', '?')] = 分类库存.get(r.get('分类', '?'), 0) + 1
    # 场景库存：多源并存后按场景统计，并单独数带场景警示的条目。
    场景库存: dict[str, int] = {}
    警示数 = 0
    for r in records:
        if r.get('简历场景'):
            场景库存[r['简历场景']] = 场景库存.get(r['简历场景'], 0) + 1
        if r.get('场景警示'):
            警示数 += 1

    标题 = '首次全量采集' if (no == 1 and scan_mode == 'full') else (
        '全量重扫' if scan_mode == 'full' else '增量采集')
    out = ['## 采集 #%03d：%s' % (no, 标题), '',
           '- **采集时间**：%s' % now_iso(),
           '- **模式**：%s' % ('`--full`（忽略 mtime 游标重扫全部源根；id 映射与记录哈希保留）'
                              if scan_mode == 'full' else
                              '`--incremental`（走 collector/state/cursor.json，只处理新增/变更文件）'),
           '- **源清单版本**：%s（%d 条源）' % (cfg.get('版本', '?'), 源清单条数),
           '- **采集方式**：全量扫描',
           '- **落盘**：%s' % ('否（--dry-run，只计数不写盘）' if dry else '是'),
           '', '### 五连计数', '',
           '| 计数项 | 数量 |', '|---|---|',
           '| 新增 | %d |' % stats.新增,
           '| 更新 | %d |' % stats.更新,
           '| 待核验 | %d |' % stats.待核验,
           '| 无源待确认 | %d |' % stats.无源待确认,
           '| 跳过二进制 | %d |' % stats.跳过二进制,
           '', '### 本轮其他计数', '',
           '| 计数项 | 数量 |', '|---|---|',
           '| 命中文件 | %d |' % stats.命中文件,
           '| 内容未变（不追加新行） | %d |' % stats.未变条目,
           '| 游标未变文件（增量跳过） | %d |' % stats.未变文件,
           '| 只登记路径的二进制/扫描件 | %d |' % stats.只登记路径,
           '| 跳过压缩包 | %d |' % stats.跳过压缩包,
           '| 跳过依赖目录 | %d |' % stats.跳过依赖目录,
           '| 跳过 99_归档 | %d |' % stats.跳过归档目录,
           '| 跳过超大文件 | %d |' % stats.跳过超大文件,
           '| 跳过 (N) 重复副本 | %d |' % stats.跳过重复副本,
           '| 跳过非白名单类型 | %d |' % stats.跳过其他类型,
           '| 跳过模板/占位文件 | %d |' % stats.跳过模板文件,
           '| 含无源数字但状态被占用（已写入无源量化值字段） | %d |' % stats.无源标记被占用,
           '| 单文件异常（已 catch 继续） | %d |' % len(stats.异常),
           '', '### 库存快照（按同 id 取最后一行聚合）', '',
           '| 维度 | 明细 |', '|---|---|',
           '| 条目总数 | %d |' % len(records),
           '| 按状态 | %s |' % ('、'.join('%s %d' % (k, v) for k, v in
                                      sorted(库存.items(), key=lambda x: -x[1])) or '空'),
           '| 按分类 | %s |' % ('、'.join('%s %d' % (k, v) for k, v in
                                      sorted(分类库存.items(), key=lambda x: -x[1])) or '空'),
           '| 按简历场景 | %s |' % (
               ('、'.join('%s %d' % (k, v) for k, v in
                         sorted(场景库存.items(), key=lambda x: -x[1]))
                + '（其中带场景警示 %d 条）' % 警示数) if 场景库存 else '空'),
           '', '### 源根命中与产出', '',
           '| 源 | 状态 | 命中文件 | 产出条目 | 说明 |', '|---|---|---|---|---|']
    for s in stats.源明细:
        out.append('| %s | %s | %d | %d | %s |' % (
            cell(s['源']), cell(s['状态']), s['命中'], s['条目'], cell(s['说明'])))
    out += ['', '### 跳过与退化说明', '']
    if stats.缺库:
        out.append('- 第三方库缺失：%s。相关文件退化为只登记路径并计入「跳过二进制」。'
                   % '、'.join(stats.缺库))
    else:
        out.append('- openpyxl 与 python-docx 均可用，xlsx / docx 走正常解析。')
    out.append('- PDF、JPG、PNG、DOC、PPT、XLSM 一类二进制与扫描件 v1 不解析内容，'
               '只登记路径与元信息，状态置「需OCR人工录入」，计入「跳过二进制」。')
    out.append('- 压缩包（zip/rar/7z/gz）、LaTeX 编译中间产物（aux/log/out/synctex）、'
               '.env、.DS_Store 按全局规则整类跳过。')
    out.append('- 同步工具遗留的「xxx(1).ext」重复副本整类跳过并单独计数：内容与正本一致，'
               '纳入只会虚增条目与新增计数。')
    out.append('- 涉密源可配「仅元信息」：xlsx 只登记工作表名与行列规模，不抄单元格值，'
               '避免把敏感底稿内容写进中台。')
    out.append('- 模板/占位文件整类跳过并单独计数（本轮 %d 个）：文件名带模板/demo/template/占位 只是初筛，'
               '还要求正文里的排版占位符（X.X、20XX、XXXX、example@）同时达到命中数与密度双阈值才跳，'
               '因此「…研究汇报_v2_模板.html」（用模板做的真汇报）、「模板选型执行说明」（模板是主题词）、'
               '「demo进度记录」（demo 是阶段名）、「prompt-templates」（模板集合本身就是产出）'
               '一类真成果不会被误杀；读不到正文的二进制只认强特征文件名（模板词收尾或紧接 demo）。'
               % stats.跳过模板文件)
    out.append('- 无源数字的判定不被状态屏蔽：每条记录都带 `无源量化值` 字段，'
               '即使状态已被「主源冲突」或「需OCR人工录入」占用，定位不到的数字仍会记在该字段里，'
               '并同时列进待核验队列的「无源待确认」区（本轮此类 %d 条）。' % stats.无源标记被占用)
    out += ['', '### 异常与处理', '']
    if stats.异常:
        for 源名, 路径, err in stats.异常[:40]:
            out.append('- [%s] `%s` → %s（已 catch，继续本轮）' % (cell(源名), cell(short_path(路径)), cell(err)))
        if len(stats.异常) > 40:
            out.append('- 另有 %d 条同类异常，未逐条列出。' % (len(stats.异常) - 40))
    else:
        out.append('- 本轮无单文件异常。')
    out += ['', '---', '']
    text = '\n'.join(out)
    if not dry:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(text)
    return text


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def run(full: bool, dry: bool, only: str | None) -> int:
    scan_mode = 'full' if full else 'incremental'
    stats = Stats()

    try:
        with open(SOURCES_PATH, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        print('[FATAL] 源清单读取失败：%s' % e, file=sys.stderr)
        return 2
    sources = cfg.get('源清单') or []
    if only:
        sources = [s for s in sources if only in (s.get('名称') or '')]

    cursor = load_cursor()
    if full:
        cursor['文件'] = {}        # --full 只清 mtime 游标，id 映射与记录哈希保留

    待写: list[dict] = []
    for src in sources:
        源名 = src.get('名称', '?')
        root = src.get('根', '')
        if root and not os.path.isabs(root):
            root = os.path.join(BASE_DIR, root)   # 与 iter_source_files 同一口径
        命中 = 0
        条目 = 0
        if not root or (not os.path.isdir(root) and not os.path.isfile(root)):
            stats.源明细.append({'源': 源名, '状态': '跳过', '命中': 0, '条目': 0,
                                 '说明': '源根不存在或不可访问：%s' % root})
            continue
        extractor = EXTRACTORS.get(src.get('提取器', 'doc_auto'), extract_doc_auto)
        seen: set = set()
        try:
            files = list(iter_source_files(src, cfg, stats, seen))
        except Exception as e:                       # noqa: BLE001
            stats.记异常(源名, root, e)
            files = []
        for path in files:
            命中 += 1
            stats.命中文件 += 1
            try:
                st = os.stat(path)
                sig = '%d:%d' % (int(st.st_mtime), st.st_size)
            except OSError as e:
                stats.记异常(源名, path, e)
                continue
            prev = cursor['文件'].get(path)
            if not full and prev and prev.get('签名') == sig:
                stats.未变文件 += 1
                continue
            try:
                recs = extractor(src, path, scan_mode, stats)
            except Exception as e:                   # noqa: BLE001  fail-open
                stats.记异常(源名, path, e)
                continue
            cursor['文件'][path] = {'签名': sig, 'mtime': int(st.st_mtime),
                                    'size': st.st_size, '源': 源名}
            for rec in recs:
                key = dedup_key(rec['分类'], rec['名称'], rec['证据路径'])
                rid = next_id(cursor, key)
                rec['id'] = rid
                h = record_hash(rec)
                rec['记录哈希'] = h
                if cursor['记录哈希'].get(key) == h:
                    stats.未变条目 += 1
                    continue
                是更新 = key in cursor['记录哈希']
                rec['版本'] = int(cursor.get('版本计数', {}).get(key, 0)) + 1
                cursor.setdefault('版本计数', {})[key] = rec['版本']
                cursor['记录哈希'][key] = h
                rec['采集时间'] = now_iso()
                ordered = {}
                for k in BASE_FIELDS:
                    ordered[k] = rec.get(k)
                for k in SCENE_FIELDS:            # 场景字段：空则不落盘
                    if rec.get(k):
                        ordered[k] = rec[k]
                ordered['记录哈希'] = rec['记录哈希']
                待写.append(ordered)
                条目 += 1
                if 是更新:
                    stats.更新 += 1
                else:
                    stats.新增 += 1
                if ordered['状态'] == STATUS_PENDING:
                    stats.待核验 += 1
                elif ordered['状态'] == STATUS_NOSOURCE:
                    stats.无源待确认 += 1
                elif ordered.get('无源量化值'):
                    stats.无源标记被占用 += 1   # 无源信息已存进字段，不被状态盖掉
        stats.源明细.append({'源': 源名, '状态': '命中', '命中': 命中, '条目': 条目,
                             '说明': src.get('说明', '')[:80]})

    if dry:
        print('[dry-run] 五连计数：新增 %d / 更新 %d / 待核验 %d / 无源待确认 %d / 跳过二进制 %d'
              % (stats.新增, stats.更新, stats.待核验, stats.无源待确认, stats.跳过二进制))
        print('[dry-run] 本轮将追加 %d 行；异常 %d 条' % (len(待写), len(stats.异常)))
        print('[dry-run] 跳过：压缩包 %d / 依赖目录 %d / 99_归档 %d / 超大文件 %d / '
              '重复副本 %d / 非白名单类型 %d / 模板占位文件 %d / 只登记路径 %d'
              % (stats.跳过压缩包, stats.跳过依赖目录, stats.跳过归档目录,
                 stats.跳过超大文件, stats.跳过重复副本, stats.跳过其他类型,
                 stats.跳过模板文件, stats.只登记路径))
        print('[dry-run] 源根明细：')
        for s in stats.源明细:
            print('  %-28s %-4s 命中 %4d  条目 %4d  %s'
                  % (s['源'], s['状态'], s['命中'], s['条目'],
                     '' if s['状态'] == '命中' else s['说明']))
        for 源名, 路径, err in stats.异常[:20]:
            print('  [异常] %s | %s | %s' % (源名, short_path(路径), err))
        return 0

    # --- 追加 jsonl ---
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(JSONL_PATH, 'a', encoding='utf-8') as f:
        for rec in 待写:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')

    # --- 重生成视图与队列 ---
    records = load_latest(JSONL_PATH)
    write_category_views(records)
    write_queue(records)

    # --- 采集日志 ---
    no = next_log_no()
    write_log(no, cfg, scan_mode, stats, records, len(sources), dry)

    # --- 保存游标 ---
    save_cursor(cursor)

    print('[collect] 采集 #%03d 完成（%s）' % (no, scan_mode))
    print('[collect] 五连计数：新增 %d / 更新 %d / 待核验 %d / 无源待确认 %d / 跳过二进制 %d'
          % (stats.新增, stats.更新, stats.待核验, stats.无源待确认, stats.跳过二进制))
    print('[collect] 本轮追加 %d 行；库存条目 %d 条；异常 %d 条'
          % (len(待写), len(records), len(stats.异常)))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description='个人信息中台采集器 v1')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--full', action='store_true', help='忽略 mtime 游标，重扫全部源根')
    g.add_argument('--incremental', action='store_true', help='默认；走游标只处理新增/变更文件')
    ap.add_argument('--dry-run', action='store_true', help='只扫描与计数，不落盘')
    ap.add_argument('--only', default=None, help='只跑名称含该子串的源（调试用）')
    a = ap.parse_args()
    try:
        return run(full=bool(a.full), dry=bool(a.dry_run), only=a.only)
    except Exception as e:                           # noqa: BLE001
        print('[FATAL] 采集器未预期异常：%s: %s' % (type(e).__name__, e), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
