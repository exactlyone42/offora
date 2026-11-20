import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home()
INSTALL_DIR = HOME / '.offora' / 'hook'
URL = os.environ.get('OFFORA_URL', 'http://127.0.0.1:18780').rstrip('/')
EVENTS = ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop')
TOOLS = ('office_describe', 'office_set_repo', 'office_update_summary', 'office_read_since', 'office_read_messages', 'office_send_message', 'office_list_sessions', 'office_mark_read', 'office_whoami', 'office_wake_command')
STAMP = time.strftime('%Y%m%d%H%M%S')


def office_key():
    machine_id = Path('/etc/machine-id').read_text().strip()
    return hashlib.sha256(('offora:' + machine_id).encode()).hexdigest()


def install_files():
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / 'hooks' / 'office_gate.py', INSTALL_DIR / 'office_gate.py')
    shutil.copy2(ROOT / 'integrations' / 'offora' / 'rules.txt', INSTALL_DIR / 'rules.txt')
    shutil.copy2(ROOT / 'clients' / 'typescript' / 'office-listen.mjs', INSTALL_DIR / 'office-listen.mjs')


def merge_hooks(path, extra_allow=None, append=False):
    if not path.exists():
        return False
    data = json.loads(path.read_text())
    shutil.copy2(path, path.with_name(path.name + '.bak-offora-' + STAMP))
    hooks = data.setdefault('hooks', {})
    for event in EVENTS:
        groups = [g for g in hooks.get(event, []) if not any('office_gate.py' in h.get('command', '') for h in g.get('hooks', []))]
        command = f'OFFORA_URL={URL} {"" if append else "OFFORA_LISTEN=1 "}/usr/bin/python3 -B {INSTALL_DIR / "office_gate.py"} {event}'
        group = {'hooks': [{'type': 'command', 'command': command, 'timeout': 5}]}
        groups.append(group) if append else groups.insert(0, group)
        hooks[event] = groups
    if extra_allow:
        allow = data.setdefault('permissions', {}).setdefault('allow', [])
        if extra_allow not in allow:
            allow.append(extra_allow)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    return True


def claude_dirs():
    dirs = [(None, HOME / '.claude' / 'settings.json')]
    for profile in sorted((HOME / '.config' / 'orca' / 'claude-profiles').glob('*/home')):
        dirs.append((profile, profile / 'settings.json'))
    return dirs


def codex_homes():
    homes = [HOME / '.codex']
    homes += sorted((HOME / '.config' / 'orca' / 'codex-accounts').glob('*/home'))
    return homes


def run(cmd, env_extra):
    env = {k: v for k, v in os.environ.items() if k not in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME')}
    env.update(env_extra)
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)


def claude_mcp(config_dir):
    endpoint = f'{URL}/mcp/{office_key()}'
    env = {'CLAUDE_CONFIG_DIR': str(config_dir)} if config_dir else {}
    run(['claude', 'mcp', 'remove', 'offora', '-s', 'user'], env)
    result = run(['claude', 'mcp', 'add', '--scope', 'user', '--transport', 'http', 'offora', endpoint], env)
    return result.returncode == 0


def codex_mcp(home):
    endpoint = f'{URL}/mcp/{office_key()}'
    env = {'CODEX_HOME': str(home)}
    run(['codex', 'mcp', 'remove', 'offora'], env)
    result = run(['codex', 'mcp', 'add', 'offora', '--url', endpoint], env)
    if result.returncode != 0:
        return False
    config = home / 'config.toml'
    existing = tomllib.loads(config.read_text())
    approved = existing.get('mcp_servers', {}).get('offora', {}).get('tools', {})
    missing = [t for t in TOOLS if approved.get(t, {}).get('approval_mode') != 'approve']
    if missing:
        shutil.copy2(config, config.with_name(config.name + '.bak-offora-' + STAMP))
        with config.open('a') as handle:
            for tool in missing:
                handle.write(f'\n[mcp_servers.offora.tools.{tool}]\napproval_mode = "approve"\n')
    return True


def main():
    install_files()
    for config_dir, settings in claude_dirs():
        print('claude hooks', settings, merge_hooks(settings, 'mcp__offora'))
        print('claude mcp', config_dir or HOME, claude_mcp(config_dir))
    for home in codex_homes():
        print('codex hooks', home, merge_hooks(home / 'hooks.json', append=True))
        print('codex mcp', home, codex_mcp(home))


if __name__ == '__main__':
    sys.exit(main())
