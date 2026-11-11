"""Fresh, bounded hot-update snapshots shared by all phases of one locked run.

Only the seven small hot-update JSON files are downloaded. Never copy the
general cache (which also contains APKs), credentials, or saved task files.
The real CLI is exec'd once; a failed game task is never retried here.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import tomllib
import urllib.parse
import urllib.request

from maa_cultivation_runtime import dump_document
from maa_activity_cache import publish_activity

DEFAULT_API = 'https://api.maa.plus/MaaAssistantArknights/api'
MAX_AGE = 3 * 3600
CACHE_INTERVAL = 6 * 3600
MAX_BYTES = 8 * 1024 * 1024
TARGETS = [('gui/StageActivityV2.json', 'StageActivityV2.json')]
for prefix in ('', 'platform_diff/iOS/resource/', 'global/YoStarEN/resource/',
               'global/YoStarJP/resource/', 'global/YoStarKR/resource/', 'global/txwy/resource/'):
    TARGETS.append((f'resource/{prefix}tasks.json', f'resource/{prefix}tasks/tasks.json'))


class SessionError(RuntimeError):
    pass


def safe_api_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
            parsed.password or parsed.query or parsed.fragment):
        raise SessionError('hot-update API must be HTTPS without credentials/query')
    return url.rstrip('/')


def fetch(url: str) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={'User-Agent': 'maa-run-session/1'})
    with urllib.request.urlopen(request, timeout=8) as response:
        data = response.read(MAX_BYTES + 1)
        if response.status != 200 or len(data) > MAX_BYTES:
            raise SessionError('invalid status or oversized response')
        return data, response.headers.get('ETag', '')


def _local_fallback_bytes(local: str) -> tuple[bytes, str] | None:
    cache_root = Path(os.environ.get('MAA_CACHE_DIR') or
                      str(Path(os.environ.get('XDG_CACHE_HOME') or Path.home()/'.cache')/'maa'))
    data_root = Path(os.environ.get('MAA_DATA_DIR') or
                     str(Path(os.environ.get('XDG_DATA_HOME') or Path.home()/'.local/share')/'maa'))
    candidates = [cache_root / local, data_root / local]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            raw = candidate.read_bytes()
            if len(raw) <= MAX_BYTES and isinstance(json.loads(raw), dict):
                etag_path = Path(str(candidate) + '.etag')
                etag = etag_path.read_text(encoding='utf-8').strip() if etag_path.is_file() else ''
                return raw, etag
        except Exception:
            continue
    base_tasks = data_root / 'resource/tasks/tasks.json'
    if base_tasks.is_file() and local in (
        'StageActivityV2.json',
        'resource/platform_diff/iOS/resource/tasks/tasks.json',
    ):
        return b'{}', ''
    return None


def download_one(url: str, local: str, target: Path, *, sleep=time.sleep) -> None:
    for attempt in range(1, 3):
        try:
            data, etag = fetch(url)
            if not isinstance(json.loads(data), dict):
                raise SessionError('resource JSON must be an object')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            # This timestamp represents a just-completed, validated HTTP fetch.
            Path(str(target) + '.etag').write_text(etag, encoding='utf-8')
            print(f'[RUN_SESSION] fetched {url}', flush=True)
            return
        except Exception as error:
            # Do not print arbitrary exception text: it can contain proxy credentials.
            print(f'[RUN_SESSION] update attempt={attempt}/2 url={url} error={type(error).__name__}',
                  file=sys.stderr, flush=True)
            if attempt < 2:
                sleep(2)
    fallback = _local_fallback_bytes(local)
    if fallback is not None:
        data, etag = fallback
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        Path(str(target) + '.etag').write_text(etag, encoding='utf-8')
        print(f'[RUN_SESSION] offline/CDN fallback used for {local}', flush=True)
        return
    raise SessionError(f'hot update failed: {url}')


def with_mooncake_announcement(tasks: dict, base_tasks: dict | None = None) -> dict:
    """Close the observed CN tutorial only after its unique heading is recognized.

    Patch the per-run snapshot, never the installed resources. Keep the existing
    announcement routes, including upstream additions. Coordinates use MaaCore's
    1280x720 reference frame; the click is confined to the top-right close icon.
    """
    result = copy.deepcopy(tasks)
    base = base_tasks or {}
    defaults = ['CloseAnno', 'CloseAnnoTexas', 'CloseAnnoSpecialAccess',
                'TodaysSupplies', 'NotDownloadVoice']
    route = result.setdefault('StartUp@CloseAnnos', copy.deepcopy(
        base.get('StartUp@CloseAnnos', {})))
    original = route.get('next', result.get('CloseAnnos', {}).get('next',
                         base.get('CloseAnnos', {}).get('next', defaults)))
    if not isinstance(original, list) or any(not isinstance(x, str) for x in original):
        raise SessionError('invalid announcement routes')
    popup = 'StartUp@MooncakeTutorial'
    routes = [x if '@' in x or x.startswith('#') else 'StartUp@' + x for x in original]
    route['next'] = [popup, *[x for x in routes if x != popup]]
    result[popup] = {
        'algorithm': 'OcrDetect', 'text': ['月饼烘焙小教程'],
        'roi': [460, 145, 360, 70], 'action': 'ClickRect',
        'specificRect': [1157, 80, 22, 24], 'postDelay': 1000,
        'maxTimes': 2, 'next': ['StartUp', 'StartUp@CloseAnnos#next'],
    }
    return result


def with_stop_at_login(tasks: dict) -> dict:
    """Stop StartUp cleanly once the Arknights login screen is reached without logging in."""
    result = copy.deepcopy(tasks)
    result['StartToWakeUp'] = {
        'action': 'Stop',
        'roi': [520, 460, 240, 100],
        'postDelay': 1000,
        'next': [],
    }
    result['StartToWakeUpOCR'] = {
        'algorithm': 'OcrDetect',
        'text': [
            '开始唤醒', '账号管理', '账号登录', '手机号登录',
            '密码登录', '验证码登录', '输入手机号', '获取验证码', '登录',
        ],
        'fullMatch': False,
        'roi': [250, 100, 780, 600],
        'action': 'Stop',
        'postDelay': 1000,
        'next': [],
    }
    result['StartLoginBServer'] = {
        'action': 'Stop',
        'roi': [400, 350, 500, 150],
        'postDelay': 1000,
        'next': [],
    }
    result['AccountManager'] = {
        'action': 'Stop',
        'roi': [697, 663, 144, 39],
        'postDelay': 1000,
        'next': [],
    }
    result['AccountManagerLoginButton'] = {
        'algorithm': 'OcrDetect',
        'text': ['登录'],
        'fullMatch': True,
        'roi': [423, 329, 448, 147],
        'action': 'Stop',
        'postDelay': 1000,
        'next': [],
    }
    for stale in (
        'StartUp@StartToWakeUp',
        'StartUp@StartToWakeUpOCR',
        'StartUp@StartLoginBServer',
        'StartUp@AccountManager',
        'StartUp@AccountManagerLoginButton',
    ):
        result.pop(stale, None)
    return result


def prepare(config_dir: Path, dest: Path) -> None:
    dest.mkdir(mode=0o700, parents=True, exist_ok=False)
    api = DEFAULT_API
    cli_path = config_dir / 'cli.toml'
    if cli_path.exists():
        api = tomllib.loads(cli_path.read_text(encoding='utf-8')).get('hot_update', {}).get('api_url', api)
    api = safe_api_url(api)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(download_one, f'{api}/{url}', local, dest/'cache'/local) for url, local in TARGETS]
        for future in futures:
            future.result()
    res_dir = Path(os.environ.get('MAA_RESOURCE_DIR') or (
        Path(os.environ.get('MAA_DATA_DIR') or
             str(Path(os.environ.get('XDG_DATA_HOME') or Path.home()/'.local/share')/'maa')) / 'resource'
    ))
    base_path = res_dir/'tasks/tasks.json'
    base = json.loads(base_path.read_text(encoding='utf-8')) if base_path.is_file() else {}
    cn_tasks = dest/'cache/resource/tasks/tasks.json'
    patched_tasks = with_mooncake_announcement(
        json.loads(cn_tasks.read_text(encoding='utf-8')), base)
    if os.environ.get('MAA_STARTUP_STOP_AT_LOGIN') == '1':
        patched_tasks = with_stop_at_login(patched_tasks)
        print('[RUN_SESSION] stop-at-login guard enabled for this snapshot', flush=True)
    cn_tasks.write_text(json.dumps(patched_tasks, ensure_ascii=False), encoding='utf-8')
    print('[RUN_SESSION] CN mooncake tutorial close rule added to this snapshot', flush=True)
    hashes = {}
    for _, relative in TARGETS:
        for suffix in ('', '.etag'):
            path = dest/'cache'/(relative + suffix)
            hashes[relative + suffix] = hashlib.sha256(path.read_bytes()).hexdigest()
    (dest/'manifest.json').write_text(json.dumps({
        'created_at': time.time(), 'api': api, 'hashes': hashes,
    }), encoding='utf-8')
    # Keep the guard, planner and WebUI on the same catalog the CLI just fetched.
    publish_activity(json.loads((dest/'cache/StageActivityV2.json').read_text(encoding='utf-8')))
    print('[RUN_SESSION] ready: seven validated resources; max age=3h', flush=True)


def validate_session(root: Path) -> dict:
    manifest = json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    age = time.time() - manifest['created_at']
    if not 0 <= age <= MAX_AGE:
        raise SessionError('run session expired or clock moved backwards')
    expected = {local + suffix for _, local in TARGETS for suffix in ('', '.etag')}
    if set(manifest['hashes']) != expected:
        raise SessionError('incomplete session manifest')
    for relative, digest in manifest['hashes'].items():
        path = root/'cache'/relative
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise SessionError('run resource snapshot changed')
        if relative.endswith('.etag') and not 0 <= time.time() - path.stat().st_mtime < MAX_AGE:
            raise SessionError('resource check timestamp expired')
    return manifest


def without_duplicate_start(document: dict) -> dict:
    result = copy.deepcopy(document)
    tasks = result.get('tasks', [])
    if tasks and tasks[0].get('type') == 'StartUp':
        startup = tasks[0]
        startup.setdefault('params', {})['start_game_enabled'] = False
        for variant in startup.get('variants', []):
            variant.setdefault('params', {})['start_game_enabled'] = False
    return result


def _safe_symlink_or_copy(source: Path, target: Path) -> None:
    try:
        target.symlink_to(source.resolve(), target_is_directory=source.is_dir())
    except OSError:
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)


def build_command(root: Path, original: Path, args: list[str], maa: str,
                  prewarm_token: str = '') -> tuple[list[str], dict[str, str]]:
    manifest = validate_session(root)
    if len(args) < 2 or args[0] != 'run' or Path(args[1]).name != args[1] or args[1] in ('.', '..'):
        raise SessionError('session accepts only maa run <task-name>')
    task_name = args[1]
    path = original/'tasks'/f'{task_name}.toml'
    document = tomllib.loads(path.read_text(encoding='utf-8'))
    if prewarm_token:
        token = hashlib.sha256(prewarm_token.encode()).hexdigest()
        marker = root/f'prewarm-{token}.consumed'
        try:
            with marker.open('x', encoding='utf-8') as handle:
                handle.write('consumed')
        except FileExistsError:
            pass
        else:
            document = without_duplicate_start(document)
            print('[RUN_SESSION] prewarm token consumed; only leading StartUp may skip relaunch', flush=True)
    phase = Path(tempfile.mkdtemp(prefix='phase-', dir=root))
    (phase/'tasks').mkdir()
    (phase/'tasks'/f'{task_name}.toml').write_text(dump_document(document), encoding='utf-8')
    # Link configuration, do not copy profile secrets or modify the originals.
    for name in ('profiles', 'resource', 'asst.toml', 'asst.json', 'asst.yaml', 'asst.yml'):
        source = original/name
        if source.exists():
            _safe_symlink_or_copy(source, phase/name)
    (phase/'cli.toml').write_text(
        '[resource]\nauto_update = false\n[hot_update]\n'
        f'api_url = {json.dumps(manifest["api"])}\ncheck_interval = {CACHE_INTERVAL}\n', encoding='utf-8')
    env = dict(os.environ)
    env.update(MAA_CONFIG_DIR=str(phase), MAA_CACHE_DIR=str(root/'cache'))
    return [maa, *args], env


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('prepare', 'exec'))
    parser.add_argument('--session', required=True, type=Path)
    parser.add_argument('--config-dir', required=True, type=Path)
    parser.add_argument('--maa', default='maa')
    argv = sys.argv[1:]
    boundary = argv.index('--') if '--' in argv else len(argv)
    options = parser.parse_args(argv[:boundary])
    try:
        if options.mode == 'prepare':
            prepare(options.config_dir, options.session)
            return 0
        args = argv[boundary + 1:]
        command, env = build_command(options.session, options.config_dir, args, options.maa,
                                     os.environ.get('MAA_PREWARM_TOKEN', ''))
        os.execvpe(command[0], command, env)
    except Exception as error:
        print(f'[RUN_SESSION] stopped before MaaCore: {type(error).__name__}', file=sys.stderr)
        return 10
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
