#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""个人信息中台 Stop hook（口述线索抽取 v1）。

把本脚本注册到 Qoder 项目级 settings 的 Stop 事件即可接线（与同工作区其他
Stop hook 并存时各占一个数组元素，追加不覆盖）。脚本通过 stdin 接收事件
JSON，恒定 exit 0——hook 绝不能阻断 Agent 主流程。

职责三件：
1. 把触发事实与 transcript 线索追加写入 collector/state/hook_events.jsonl
   （state/ 已被 .gitignore 排除，不入库）；
2. 读 transcript_path，解析 JSONL 统计用户真文本中的口述事实线索命中数；
3. 口述线索抽取 v1：从用户键入消息中按保守句式模式抽取事实线索，
   写入 collector/state/oral_clues.jsonl（gitignored、append-only）。
   线索状态恒为「待核验/口述线索」，绝不自动转正，绝不写 personal_info.jsonl。

R1-4（2026-11-12）起带去抖：同一 session 在 DEBOUNCE_SECONDS（60s）内重复 Stop
不重扫 transcript，状态存 collector/state/debounce.json（gitignored）。本脚本
并不跑 --incremental（采集在 SessionStart 那边），所以去抖省的只是重复 IO；
去抖逻辑读写异常一律按放行处理，恒 exit 0 的 fail-open 语义不变。

口述线索抽取七条红线（不可违反）：
    1. 只从 type=='user' 且 message.content 为原始字符串（不是 list）的消息抽
    2. 绝不从 assistant 输出抽
    3. 绝不从 tool_result（content 是 list）抽
    4. 线索状态恒「待核验/口述线索」，绝不自动转正
    5. 含数字必标 has_number=True
    6. 只 append 写 collector/state/oral_clues.jsonl（gitignored），绝不写 personal_info.jsonl
    7. 整个抽取链路包 try/except，异常只写 hook_events.jsonl 的 detail 字段、恒 exit 0

transcript 处理口径：JSONL 格式，每行一个 JSON 对象。
    字段：type（user/assistant/progress/session_meta）、message（{role, content}）、
    sessionId、timestamp、uuid、cwd。
    用户真实输入识别：type=='user' 且 message.content 是原始字符串（不是 list）。
    message.content 是 list 时为 tool_result，不是人类键入。

写盘边界：日志目录由本文件自身位置推导（collector/state/），不使用 cwd，
因此无论 IDE 从哪个工作目录调起，都只会写进本仓库内。
transcript 只读不写。

fail-open 三道防线（缺一不可，共同保证「任何异常都 exit 0」）：
1. json.load 外层 except Exception——不止 ValueError/OSError；
2. isinstance(data, dict) 守卫——stdin 是合法 JSON 但为数组/字符串/数字时，
   data.get(...) 会抛 AttributeError，必须当空输入处理；
3. __main__ 兜底 try/except——任何未预料异常（包括 transcript 读取链路）也只 exit 0。
"""
import datetime
import json
import os
import re
import sys
import time

# --------------------------------------------------------------------------
# 路径：一律由本文件位置推导，不依赖 cwd
# --------------------------------------------------------------------------
COLLECTOR_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(COLLECTOR_DIR)                 # 仓库根
STATE_DIR = os.path.join(COLLECTOR_DIR, 'state')
EVENTS_PATH = os.path.join(STATE_DIR, 'hook_events.jsonl')
ORAL_CLUES_PATH = os.path.join(STATE_DIR, 'oral_clues.jsonl')
FROZEN_PATH = os.path.join(STATE_DIR, 'FROZEN')       # H-3 外部冻结总闸（gitignored 空文件）

# 接线开关：True = 完整读 transcript 并执行口述线索抽取。
# 改回 False 可临时停用抽取，脚本仍会写事件日志、仍 exit 0。
ENABLE_SESSION_EXTRACT = True

TRANSCRIPT_READ_LIMIT = 2_000_000   # transcript 最多读入字符数，防大文件拖慢会话结束
MSG_LEN = 200                       # 事件字段截断长度

# --------------------------------------------------------------------------
# 去抖闸门（R1-4，2026-11-12）
# --------------------------------------------------------------------------
DEBOUNCE_PATH = os.path.join(STATE_DIR, 'debounce.json')
DEBOUNCE_SECONDS = 60       # 同一 session 在此窗口内重复触发时，拦下重复动作
DEBOUNCE_KEEP = 200         # 状态文件最多保留的键数，防 session 累积把它撑大


def _load_debounce() -> dict:
    try:
        with open(DEBOUNCE_PATH, 'r', encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:                              # noqa: BLE001  fail-open
        return {}                                  # 读不到/损坏就当没有记录：
                                                   # 宁可多跑一次，也不能漏采


def _save_debounce(d: dict) -> None:
    try:
        if len(d) > DEBOUNCE_KEEP:                 # 只留最近的一批键
            d = dict(sorted(d.items(), key=lambda kv: kv[1])[-DEBOUNCE_KEEP:])
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = DEBOUNCE_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, DEBOUNCE_PATH)             # 原子替换，避免半截文件
    except Exception:                              # noqa: BLE001  写失败不影响主流程
        pass


def debounce_gate(hook: str, session_id) -> tuple:
    """返回 (放行?, 说明字典)。放行时顺手把本次时间戳写回状态文件。

    时钟回拨（gap < 0）按放行处理：那种情况下拦不住任何真实重复，
    而误拦一次采集的代价远大于多跑一次。
    """
    key = '%s:%s' % (hook, session_id or 'unknown')
    now = time.time()
    d = _load_debounce()
    try:
        gap = now - float(d.get(key))
    except (TypeError, ValueError):
        gap = None                                 # 没有记录或记录损坏
    if gap is not None and 0 <= gap < DEBOUNCE_SECONDS:
        return False, {'debounced': True,
                       'debounce_window': DEBOUNCE_SECONDS,
                       'since_last_seconds': round(gap, 1)}
    d[key] = now
    _save_debounce(d)
    return True, {'debounced': False,
                  'debounce_window': DEBOUNCE_SECONDS,
                  'since_last_seconds': (round(gap, 1) if gap is not None else None)}


# --------------------------------------------------------------------------
# 口述事实线索抽取（保守 v1）
# --------------------------------------------------------------------------

# 口述事实线索词：Layer 2 粗筛正则，只在用户真文本上跑
FACT_HINT_RE = re.compile(
    r'我.{0,10}(?:在|曾|于|做|参与|负责|拿|获|得|实习|工作|担任|开发|研究|毕业|入学)|'
    r'(?:实习|项目|论文|竞赛|获奖|奖学金|排名|绩点|GPA|保研|综测|志愿|社团|荣誉|毕业|入学|录取)')

# Layer 3 五个保守句式模式（宁漏不误）
FACT_PATTERNS = [
    ('我在X做了Y',
     re.compile(r'我.{0,10}?(?:在|曾|于)\s*(.{2,30}?)\s*(?:做|实习|工作|参与|负责|担任|开发|研究)')),
    ('我拿了X奖',
     re.compile(r'我(?:拿到|获得|拿|得|获)\s*(?:了|过)?\s*(.{2,40}?)(?:奖|奖学金|荣誉|名次|认证)')),
    ('项目X了Y',
     re.compile(r'(?:这个|那个|我的)(?:项目|论文|作品|报告|系统)\s*(.{0,50}?)(?:上线|发表|通过|完成|交付|部署)')),
    ('我的GPA是X',
     re.compile(r'我的?\s*(?:GPA|绩点|排名|成绩|均分)\s*(?:是|为|：|:)\s*(.{1,20})')),
    ('我X年从Y毕业',
     re.compile(r'我\s*(\d{4})\s*年?\s*(?:从|在)?\s*(.{2,20}?)\s*(?:毕业|入学|录取|保研|考研)')),
]

# Layer 1 噪声过滤：已知指令模式（任务指令不是事实陈述）
_INSTRUCTION_RE = re.compile(r'^请?帮我?(?:写|改|做|生成|搜索|查|看看|检查|分析|整理)')

# 否定前缀（无转折时过滤）
# M-2: '别' 加后向约束，只在「别+动词」（别说/别动/别改…）时算否定前缀；
#      裸 '别' 会误杀「特别奖学金」「级别评定」等含 '别' 的正常事实词。
_NEGATION_RE = re.compile(r'(?:不是|不要|别(?=[做说写改动手去要])|没有|不能|不用)')
_TRANSITION_RE = re.compile(r'(?:但是|但|不过|然而|可是|却)')

# 数字检测（含阿拉伯数字或中文数字量词）
_NUMBER_RE = re.compile(r'\d|[一二三四五六七八九十百千万亿]+[个条项份人年月天次篇]')


def _has_cjk(text):
    """判断文本是否含 CJK 字符。"""
    return any('\u4e00' <= ch <= '\u9fff' for ch in text)


def _split_sentences(text):
    """M-2: 按中英文句末标点与换行切分为句子列表（去空白项）。"""
    parts = re.split('[。！？；，、' + chr(10) + '!?;,]+', text)
    return [p.strip() for p in parts if p.strip()]


def _sentence_is_noise(sent):
    """M-2: 单句级否定/指令过滤（否定无转折、或匹配指令模式即噪声）。"""
    if _NEGATION_RE.search(sent) and not _TRANSITION_RE.search(sent):
        return True
    if _INSTRUCTION_RE.search(sent):
        return True
    return False


def _is_noise(text):
    """Layer 1 噪声过滤。返回 True 表示应跳过。"""
    # 长度 <= 5 字符
    if len(text) <= 5:
        return True
    # 以问号结尾（中英文）
    stripped = text.rstrip()
    if stripped and stripped[-1] in ('?', '\uff1f'):
        return True
    # 全 ASCII（无 CJK 字符）
    if not _has_cjk(text):
        return True
    # M-2: 否定/指令按句切分逐句过滤——只有所有句子都是噪声才判整段噪声，
    #      避免长任务指令里一个否定词/指令废掉其余句子里的真实事实。
    sents = _split_sentences(text)
    if sents and all(_sentence_is_noise(x) for x in sents):
        return True
    return False


def _extract_clues_from_text(text, session_id, transcript_path, line_no, timestamp):
    """对单条用户文本执行 Layer 1-4 抽取，返回线索对象列表。"""
    clues = []

    # Layer 1: 噪声过滤
    if _is_noise(text):
        return clues

    # Layer 2: FACT_HINT_RE 粗筛
    if not FACT_HINT_RE.search(text):
        return clues

    # Layer 3: 五个保守句式模式
    matched_patterns = []
    for name, pattern in FACT_PATTERNS:
        if pattern.search(text):
            matched_patterns.append(name)

    if not matched_patterns:
        return clues

    # Layer 4: 结构化线索对象
    for pname in matched_patterns:
        clue = {
            'source': 'session_oral',
            'session_id': session_id or 'unknown',
            'transcript_path': transcript_path,
            'line_no': line_no,
            'timestamp': timestamp or '',
            'raw_text': text[:200],
            'matched_pattern': pname,
            'has_number': bool(_NUMBER_RE.search(text)),
            'status': '待核验/口述线索',
        }
        clues.append(clue)

    return clues


def _load_existing_clue_keys():
    """读取已有 oral_clues.jsonl，返回 (session_id, line_no, matched_pattern) 集合用于幂等判重。"""
    keys = set()
    try:
        if os.path.isfile(ORAL_CLUES_PATH):
            with open(ORAL_CLUES_PATH, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        keys.add((obj.get('session_id'), obj.get('line_no'),
                                  obj.get('matched_pattern')))
                    except (json.JSONDecodeError, AttributeError):
                        pass
    except Exception:
        pass
    return keys


def extract_oral_clues(transcript_path, session_id):
    """从 transcript 抽取口述事实线索（Layer 0-4 完整链路）。

    红线：只从 type=='user' 且 message.content 为原始字符串的行抽取。
    返回 (clues_written, error_msg)。error_msg 为 None 表示正常。
    """
    try:
        with open(transcript_path, 'r', encoding='utf-8', errors='replace') as f:
            raw_text = f.read(TRANSCRIPT_READ_LIMIT)
    except OSError as e:
        return 0, str(e)

    lines = raw_text.split('\n')
    existing_keys = _load_existing_clue_keys()
    new_clues = []

    for line_no_0, line in enumerate(lines):
        if not line.strip():
            continue
        # Layer 0: 解析 JSONL，只保留 type=='user' 且 message.content 是 str
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get('type') != 'user':
            continue
        msg = obj.get('message')
        if not isinstance(msg, dict):
            continue
        content = msg.get('content')
        if not isinstance(content, str):
            continue  # content 是 list 时为 tool_result，跳过

        # 提取时间戳
        ts = obj.get('timestamp', '')

        # Layer 1-4 抽取
        clues = _extract_clues_from_text(content, session_id, transcript_path,
                                         line_no_0 + 1, ts)
        for clue in clues:
            # M-1: 幂等键含 matched_pattern——同一行的多模式线索各自落盘；
            #      旧键 (session_id, line_no) 会吞掉同行其余模式（丢 GPA/毕业院校）。
            key = (clue['session_id'], clue['line_no'], clue['matched_pattern'])
            if key not in existing_keys:
                new_clues.append(clue)
                existing_keys.add(key)

    # 写入 oral_clues.jsonl（append-only）
    if new_clues:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(ORAL_CLUES_PATH, 'a', encoding='utf-8') as f:
                for clue in new_clues:
                    f.write(json.dumps(clue, ensure_ascii=False) + '\n')
        except OSError as e:
            return 0, 'write_error: %s' % str(e)

    return len(new_clues), None


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def s(obj):
    return obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)


def truncate(text, n):
    text = s(text)
    return text if len(text) <= n else text[:n] + '…[截断]'


def transcript_summary(transcript_path):
    """只读扫 transcript，做轻量统计（修正版：正确解析 JSONL）。

    修正（2026-11-13）：原实现把 JSONL 当纯文本按行 split 跑正则，
    导致 assistant 输出关键词也被计数（实测 fact_hint_lines==transcript_lines）。
    现改为先 json.loads 每行，只取 type=='user' 且 message.content 是 str 的行，
    再跑 FACT_HINT_RE 正则计数。
    """
    summary = {'transcript_path': transcript_path}
    try:
        with open(transcript_path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read(TRANSCRIPT_READ_LIMIT)
        lines = text.split('\n')
        summary['transcript_bytes'] = len(text.encode('utf-8'))
        summary['transcript_lines'] = len(lines)
        summary['transcript_truncated'] = len(text) >= TRANSCRIPT_READ_LIMIT

        # 修正：只统计 user raw-string 行中的线索命中
        user_text_lines = 0
        fact_hint_count = 0
        for line in lines:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get('type') != 'user':
                continue
            msg = obj.get('message')
            if not isinstance(msg, dict):
                continue
            content = msg.get('content')
            if not isinstance(content, str):
                continue
            user_text_lines += 1
            if FACT_HINT_RE.search(content):
                fact_hint_count += 1

        summary['user_raw_string_lines'] = user_text_lines
        summary['fact_hint_lines'] = fact_hint_count
    except OSError as e:
        summary['transcript_error'] = str(e)
    return summary


def extract_session_facts(summary, transcript_path=None, session_id=None):
    """从 transcript 抽取会话口述事实线索（v1 保守实现）。

    开关关闭时只回一个占位说明；开关打开后执行 Layer 0-4 抽取，
    线索 append 写入 collector/state/oral_clues.jsonl（gitignored）。
    绝不写 personal_info.jsonl。全程 fail-open，异常只记日志。
    """
    if not ENABLE_SESSION_EXTRACT:
        return {'skipped': '接线开关已关（ENABLE_SESSION_EXTRACT=False，只登记 transcript 线索计数）',
                'fact_hint_lines': summary.get('fact_hint_lines')}

    if _is_frozen():
        return {'skipped': 'FROZEN 冻结标志存在，跳过口述线索抽取（仅登记 transcript 线索计数）',
                'fact_hint_lines': summary.get('fact_hint_lines')}

    if not transcript_path:
        return {'skipped': '无 transcript_path',
                'fact_hint_lines': summary.get('fact_hint_lines')}

    try:
        clues_written, error = extract_oral_clues(transcript_path, session_id)
        result = {
            'fact_hint_lines': summary.get('fact_hint_lines'),
            'user_raw_string_lines': summary.get('user_raw_string_lines'),
            'transcript_lines': summary.get('transcript_lines'),
            'transcript_bytes': summary.get('transcript_bytes'),
            'clues_written': clues_written,
        }
        if error:
            result['error'] = error
        return result
    except Exception as e:
        return {'error': str(e),
                'fact_hint_lines': summary.get('fact_hint_lines')}


def _is_frozen():
    """H-3: collector/state/FROZEN 存在即冻结——hook 跳过采集/抽取动作，仍写事件日志。
    """
    try:
        return os.path.exists(FROZEN_PATH)
    except OSError:
        return False


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:                              # noqa: BLE001  fail-open
        return 0                                 # 输入不可解析：静默放行
    if not isinstance(data, dict):
        return 0                                 # 合法 JSON 但非对象（[1,2,3]、"x"、7）：
                                                 # 当空输入处理，否则 data.get 抛 AttributeError

    event = data.get('hook_event_name') or 'unknown'
    detail = {
        'cwd': s(data.get('cwd') or os.getcwd()),
        'stop_hook_active': data.get('stop_hook_active'),
        'last_assistant_message': truncate(data.get('last_assistant_message') or '', MSG_LEN),
    }
    # L-5: 中段（FROZEN 判定 / 去抖 / transcript 统计 / 口述抽取）整体包 try，
    #      任何非 OSError 异常也只记进 detail['error']，保证 record 一定被构造并写入
    #      （否则连「本次 Stop 触发过」都不登记，可观测性有缺口）。
    # H-3: FROZEN 总闸——见 collector/state/FROZEN 即跳过动作，但仍写事件日志。
    try:
        tp = data.get('transcript_path')
        if _is_frozen():
            detail['frozen'] = True
            detail['session_facts'] = {
                'skipped': 'FROZEN 冻结标志存在（collector/state/FROZEN），'
                           '跳过 transcript 扫描与口述线索抽取，仅登记事件'}
        elif tp:
            # 去抖（R1-4）：同一会话常在短时间内多次 Stop，短窗口内重复触发只登记「被拦下」。
            ok, gate = debounce_gate('stop', data.get('session_id'))
            detail['debounce'] = gate
            if ok:
                summary = transcript_summary(tp)
                detail.update(summary)
                detail['session_facts'] = extract_session_facts(
                    summary, transcript_path=tp, session_id=data.get('session_id'))
            else:
                detail['session_facts'] = {
                    'skipped': '去抖拦下（同一 session 短窗口内已扫过 transcript）'}
        else:
            detail['session_facts'] = {'skipped': '事件未带 transcript_path'}
    except Exception as e:                         # noqa: BLE001  L-5 可观测性兜底
        detail['error'] = repr(e)

    record = {'ts': now_iso(),
              'session_id': data.get('session_id') or 'unknown',
              'event': event,
              'hook': 'stop',
              'detail': detail}

    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(EVENTS_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
    except OSError:
        pass                                     # 写盘失败不阻断 Agent
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:                              # noqa: BLE001  最后一道 fail-open 兜底
        sys.exit(0)                                # hook 绝不能以非 0 退出阻断 Agent
