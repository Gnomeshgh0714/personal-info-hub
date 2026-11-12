"""个人信息中台 SessionStart hook。

把本脚本注册到 Qoder 项目级 settings（.qoder/settings.local.json 或
.qoder/settings.json）的 SessionStart 事件即可接线。脚本通过 stdin
接收事件 JSON，恒定 exit 0——hook 绝不能阻断 Agent 主流程。

职责两件：
1. 把触发事实追加写入 collector/state/hook_events.jsonl（state/ 已被 .gitignore
   排除，不入库），用于确认「hook 到底有没有被 IDE 调起」；
2. 调 collect.py --incremental，把上一次会话之后新增/变更的源文件补进中台。
   串行、带超时（INCREMENTAL_TIMEOUT）、失败只记日志不抛出。

接线开关 ENABLE_INCREMENTAL 默认 True；要临时停用自动采集（例如源盘在大量
重写时），把它改回 False 即可，无需动 settings（仍会写事件日志、仍 exit 0）。

写盘边界：日志目录由本文件自身位置推导（collector/state/），不使用 cwd，
因此无论 IDE 从哪个工作目录调起，都只会写进本仓库内。

fail-open 三道防线（缺一不可，共同保证「任何异常都 exit 0」）：
1. json.load 外层 except Exception——不止 ValueError/OSError；
2. isinstance(data, dict) 守卫——stdin 是合法 JSON 但为数组/字符串/数字时，
   data.get(...) 会抛 AttributeError，必须当空输入处理；
3. __main__ 兜底 try/except——任何未预料异常也只 exit 0。
"""
import datetime
import json
import os
import sys

# --------------------------------------------------------------------------
# 路径：一律由本文件位置推导，不依赖 cwd
# --------------------------------------------------------------------------
COLLECTOR_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(COLLECTOR_DIR)                 # 仓库根
STATE_DIR = os.path.join(COLLECTOR_DIR, 'state')
EVENTS_PATH = os.path.join(STATE_DIR, 'hook_events.jsonl')
COLLECT_PY = os.path.join(COLLECTOR_DIR, 'collect.py')

# 接线开关：True = 会话开始即跑一次 --incremental。
# 改回 False 可临时停用自动采集，脚本仍会写事件日志、仍 exit 0。
ENABLE_INCREMENTAL = True
# 增量采集超时（秒）。必须小于 settings 里 SessionStart 的 hook timeout（如 120），
# 否则外层会先杀进程，本脚本就没机会把超时写进事件日志供排查。
# 增量只处理变更文件，100s 对慢盘冷启动也有足够余量。
INCREMENTAL_TIMEOUT = 100

MSG_LEN = 200                    # 事件字段截断长度


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def s(obj):
    return obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)


def truncate(text, n):
    text = s(text)
    return text if len(text) <= n else text[:n] + '…[截断]'


def run_incremental():
    """调 collect.py --incremental。

    硬约束：
    - 显式用 python3（sys.executable 兜底 'python3'），脚本路径由 __file__ 推导，不靠 cwd；
    - 带超时（INCREMENTAL_TIMEOUT），避免会话开始被慢盘卡住；
    - 只捕获异常记进返回值，绝不向 Agent 抛出、绝不影响 exit code。
    """
    if not ENABLE_INCREMENTAL:
        return {'skipped': '接线开关已关（ENABLE_INCREMENTAL=False，只写事件日志）'}
    try:
        import subprocess
        p = subprocess.run([sys.executable or 'python3', COLLECT_PY, '--incremental'],
                           cwd=BASE_DIR, capture_output=True, text=True,
                           timeout=INCREMENTAL_TIMEOUT)
        return {'returncode': p.returncode,
                'stdout_tail': truncate(p.stdout or '', MSG_LEN),
                'stderr_tail': truncate(p.stderr or '', MSG_LEN)}
    except Exception as e:                       # 任何失败都只记录，不阻断
        return {'error': '%s: %s' % (type(e).__name__, e)}


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:                              # noqa: BLE001  fail-open
        return 0                                 # 输入不可解析：静默放行
    if not isinstance(data, dict):
        return 0                                 # 合法 JSON 但非对象（[1,2,3]、"x"、7）：
                                                 # 当空输入处理，否则 data.get 抛 AttributeError

    event = data.get('hook_event_name') or 'unknown'
    record = {
        'ts': now_iso(),
        'session_id': data.get('session_id') or 'unknown',
        'event': event,
        'hook': 'session_start',
        'detail': {
            'cwd': s(data.get('cwd') or os.getcwd()),
            'source': s(data.get('source') or ''),          # startup / resume / clear
            'raw_keys': sorted(data.keys()),
            'collect_py_exists': os.path.exists(COLLECT_PY),
            'incremental': run_incremental(),
        },
    }

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
