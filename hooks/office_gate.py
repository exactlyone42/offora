import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE_URL = os.environ.get('OFFORA_URL', 'http://127.0.0.1:18780').rstrip('/')
STATE_DIR = Path(os.environ.get('OFFORA_GATE_DIR', str(Path.home() / '.offora' / 'gate')))
HERE = Path(__file__).resolve().parent
RULES = [Path(os.environ['OFFORA_RULES'])] if os.environ.get('OFFORA_RULES') else [HERE.parent / 'integrations' / 'offora' / 'rules.txt', HERE / 'rules.txt']
MAX_DENIALS = 6
TOOL_PREFIXES = ('mcp__offora__', 'mcp__plugin_offora_offora__')
ALWAYS_ALLOWED = {'ToolSearch'}
LISTEN = os.environ.get('OFFORA_LISTEN') == '1'
LISTENER = 'office-listen.mjs'
READ_TOOLS = {'office_read_messages', 'office_read_since'}


def short(name):
    for prefix in TOOL_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix):]
    return None


def server_up():
    try:
        with urllib.request.urlopen(BASE_URL + '/health/ready', timeout=1) as response:
            return response.status == 200
    except Exception:
        return False


def repo_origin(cwd):
    try:
        result = subprocess.run(['git', '-C', cwd or '.', 'remote', 'get-url', 'origin'], capture_output=True, text=True, timeout=2)
    except Exception:
        return ''
    return result.stdout.strip() if result.returncode == 0 else ''


def state_path(session_id):
    return STATE_DIR / (re.sub(r'[^A-Za-z0-9_.-]', '_', session_id)[:80] + '.json')


def load(session_id):
    try:
        return json.loads(state_path(session_id).read_text())
    except Exception:
        return {'done': [], 'turn': [], 'denials': 0, 'need_repo': False}


def save(session_id, state):
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_path(session_id).write_text(json.dumps(state))


def prune():
    cutoff = time.time() - 7 * 86400
    for path in STATE_DIR.glob('*.json'):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def registered(state):
    needed = {'office_describe', 'office_update_summary'}
    if state.get('need_repo'):
        needed.add('office_set_repo')
    return needed <= set(state['done'])


def context(event, text):
    return {'hookSpecificOutput': {'hookEventName': event, 'additionalContext': text}}


def rules_text():
    for path in RULES:
        try:
            return path.read_text(encoding='utf-8')
        except OSError:
            continue
    return ''


START = (
    '【本機辦公室強制規定】本工作階段必須先登記：依序呼叫 office_describe（職責、電腦、環境）、'
    '{repo}office_update_summary（正在做的事與進度）。登記完成前，除 offora 工具與 ToolSearch 外的工具一律被擋。'
    '若 offora 工具未載入，先用 ToolSearch 查詢 offora。'
)

TURN = (
    '【本機辦公室每輪規定】本輪開始先讀訊息（office_read_since 或 office_read_messages）；'
    '有給你的訊息就用 reply_to 回覆。本輪結束前必須呼叫 office_update_summary 回報成果與進度，'
    '否則無法結束本輪。'
)


def handle_start(payload, event):
    session_id = payload.get('session_id') or ''
    if not session_id or not server_up():
        return {}
    prune()
    state = load(session_id)
    if event == 'SessionStart' and payload.get('source') not in ('resume', 'compact'):
        state = {'done': [], 'turn': [], 'denials': 0}
    origin = repo_origin(payload.get('cwd', ''))
    state['need_repo'] = bool(origin)
    state['turn'] = []
    save(session_id, state)
    text = START.format(repo=f'office_set_repo（repo_url 傳 {origin}）、' if origin else '')
    if event == 'UserPromptSubmit':
        text = TURN if registered(state) else text + TURN
    return context(event, rules_text() + '\n' + text)


def handle_pre(payload):
    session_id = payload.get('session_id') or ''
    tool = payload.get('tool_name') or ''
    if not session_id or short(tool) is not None or tool in ALWAYS_ALLOWED:
        return {}
    state = load(session_id)
    if registered(state) or state.get('denials', 0) >= MAX_DENIALS or not server_up():
        return {}
    state['denials'] = state.get('denials', 0) + 1
    save(session_id, state)
    reason = '尚未登記辦公室。請先呼叫：' + '、'.join(missing_registration(state)) + '。'
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny', 'permissionDecisionReason': reason}}


def listener_channels(command):
    if LISTENER not in command:
        return []
    return re.findall(r'(\S+?):[0-9a-f]{64}:\S+', command)


def listening(state):
    channels = state.get('listen') or []
    for path in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            args = path.read_bytes().decode('utf-8', 'replace').split('\0')
        except OSError:
            continue
        if any(arg.endswith(LISTENER) for arg in args) and any(arg.startswith(channel + ':') for arg in args for channel in channels):
            return True
    return False


def handle_post(payload):
    session_id = payload.get('session_id') or ''
    tool = payload.get('tool_name') or ''
    name = short(tool)
    if not session_id:
        return {}
    if name is None:
        channels = listener_channels(str((payload.get('tool_input') or {}).get('command') or '')) if tool == 'Bash' else []
        if channels:
            state = load(session_id)
            state['listen'] = channels
            save(session_id, state)
        return {}
    state = load(session_id)
    if name not in state['done']:
        state['done'].append(name)
    if name not in state['turn']:
        state['turn'].append(name)
    save(session_id, state)
    return {}


def missing_registration(state):
    names = ('office_describe', 'office_set_repo', 'office_update_summary')
    return [name for name in names if name not in state['done'] and (name != 'office_set_repo' or state.get('need_repo'))]


def handle_stop(payload):
    session_id = payload.get('session_id') or ''
    if not session_id or payload.get('stop_hook_active') or not server_up():
        return {}
    state = load(session_id)
    if state.get('denials', 0) >= MAX_DENIALS:
        return {}
    needed = missing_registration(state)
    if 'office_update_summary' not in state['turn']:
        needed.append('office_update_summary')
    if not (READ_TOOLS & set(state['turn'])):
        needed.append('office_read_since')
    needed = list(dict.fromkeys(needed))
    reasons = []
    if needed:
        reasons.append('結束前必須完成辦公室回報：呼叫 ' + '、'.join(needed) + '。')
    if LISTEN and not listening(state):
        reasons.append('結束前必須有常駐監聽：呼叫 office_wake_command，並用 Bash 背景執行它回傳的 command。')
    if not reasons:
        return {}
    return {'decision': 'block', 'reason': ''.join(reasons)}


def main():
    event = sys.argv[1] if len(sys.argv) > 1 else ''
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    handlers = {
        'SessionStart': lambda: handle_start(payload, 'SessionStart'),
        'UserPromptSubmit': lambda: handle_start(payload, 'UserPromptSubmit'),
        'PreToolUse': lambda: handle_pre(payload),
        'PostToolUse': lambda: handle_post(payload),
        'Stop': lambda: handle_stop(payload),
    }
    try:
        result = handlers.get(event, lambda: {})()
    except Exception as error:
        print(f'office_gate: {type(error).__name__}', file=sys.stderr)
        result = {}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
