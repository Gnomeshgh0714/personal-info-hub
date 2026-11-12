"""个人信息中台 Stop hook。

把本脚本注册到 Qoder 项目级 settings 的 Stop 事件即可接线（与同工作区其他
Stop hook 并存时各占一个数组元素，追加不覆盖）。脚本通过 stdin 接收事件
JSON，恒定 exit 0——hook 绝不能阻断 Agent 主流程。

职责两件：
1. 把触发事实与 transcript 线索追加写入 collector/state/hook_events.jsonl
   （state/ 已被 .gitignore 排除，不入库）；
2. 读 transcript_path，统计行数与口述事实线索命中行数，为后续的会话抽取存证。

接线开关 ENABLE_SESSION_EXTRACT 默认 True：完整读 transcript 并登记线索计数。
但「口述事实 → 候选条目」的抽取逻辑尚未实现，所以开关打开后仍不会产生任何
条目，也不会写 personal_info.jsonl。实现时必需满足：
    会话结束时扫 transcript，把用户口述的个人信息（「我大三暑假在 XX 做了 XX」
    「这个项目拿了 XX 奖」）抽成候选条目，采集方式标「会话提取」，证据路径指向
    transcript 文件本身、证据定位指向行号，状态一律「待核验」——口述内容没有
    第二来源印证，绝不自动置「已核验」。候选只追加进 待核验队列.md 供人工确认，
    确认后才进 personal_info.jsonl。

transcript 处理口径：格式未官方文档化，因此只做字节级/行级的轻量统计与子串
计数，不假设 JSON 结构；读失败不阻断，把错误写进 transcript_error 字段供排查。

写盘边界：日志目录由本文件自身位置推导（collector/state/），不使用 cwd，
因此无论 IDE 从哪个工作目录调起，都只会写进本仓库内。transcript 只读不写。

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

# --------------------------------------------------------------------------
# 路径：一律由本文件位置推导，不依赖 cwd
# --------------------------------------------------------------------------
COLLECTOR_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(COLLECTOR_DIR)                 # 仓库根
STATE_DIR = os.path.join(COLLECTOR_DIR, 'state')
EVENTS_PATH = os.path.join(STATE_DIR, 'hook_events.jsonl')

# 接线开关：True = 完整读 transcript 并登记线索计数。
# 注意：抽取逻辑本身尚未实现，所以开关打开也不会产生条目、不会写 jsonl。
ENABLE_SESSION_EXTRACT = True

TRANSCRIPT_READ_LIMIT = 2_000_000   # transcript 最多读入字符数，防大文件拖慢会话结束
MSG_LEN = 200                       # 事件字段截断长度

# 口述事实线索词：按这些词定位候选句。当前只用于计数，不做抽取。
FACT_HINT_RE = re.compile(
    r'我(?:在|曾|于|是|做过|参与|负责|拿过|得过|得过奖)|'
    r'(?:实习|项目|论文|竞赛|获奖|奖学金|排名|绩点|GPA|保研|综测|志愿|社团)')


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def s(obj):
    return obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)


def truncate(text, n):
    text = s(text)
    return text if len(text) <= n else text[:n] + '…[截断]'


def transcript_summary(transcript_path):
    """只读扫 transcript，做轻量统计。

    不假设 JSON 结构：只数字节数、行数与口述线索词命中行数，作为将来抽取
    会话口述事实的可行性证据。读失败不阻断（写 transcript_error 供排查）。
    """
    summary = {'transcript_path': transcript_path}
    try:
        with open(transcript_path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read(TRANSCRIPT_READ_LIMIT)
        lines = text.split('\n')
        summary['transcript_bytes'] = len(text.encode('utf-8'))
        summary['transcript_lines'] = len(lines)
        summary['transcript_truncated'] = len(text) >= TRANSCRIPT_READ_LIMIT
        # 线索命中只计数，不抄原文：会话里可能含敏感内容，落盘留给人工确认后再说
        summary['fact_hint_lines'] = sum(1 for ln in lines if FACT_HINT_RE.search(ln))
    except OSError as e:
        summary['transcript_error'] = str(e)
    return summary


def extract_session_facts(summary):
    """从 transcript 抽会话口述事实为候选条目（开关已开，抽取未实现）。

    开关关闭时只回一个占位说明；开关打开后仍然不会产生任何条目，因为
    「口述事实 → 候选条目」的抽取逻辑还没实现，只把线索计数带回事件日志存证。
    抽取实现时必须满足（勿删）：
    - 采集方式标「会话提取」，证据路径指向 transcript、证据定位指向行号；
    - 状态一律「待核验」，口述事实绝不自动置「已核验」；
    - 只写 待核验队列.md，人工确认后才进 personal_info.jsonl；
    - 全程 fail-open，任何异常只记日志。
    """
    if not ENABLE_SESSION_EXTRACT:
        return {'skipped': '接线开关已关（ENABLE_SESSION_EXTRACT=False，只登记 transcript 线索计数）',
                'fact_hint_lines': summary.get('fact_hint_lines')}
    return {'todo': '会话口述事实抽取待实现（线索已登记，本轮未生成候选条目）',
            'fact_hint_lines': summary.get('fact_hint_lines'),
            'transcript_lines': summary.get('transcript_lines'),
            'transcript_bytes': summary.get('transcript_bytes')}


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
    tp = data.get('transcript_path')
    if tp:
        summary = transcript_summary(tp)
        detail.update(summary)
        detail['session_facts'] = extract_session_facts(summary)
    else:
        detail['session_facts'] = {'skipped': '事件未带 transcript_path'}

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
