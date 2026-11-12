"""Shared, atomically published official activity catalog; no game operations."""
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request

from maa_weekly_plan import atomic_write_text

ACTIVITY_API='https://api.maa.plus/MaaAssistantArknights/api/gui/StageActivityV2.json'
_CACHE_ROOT = Path(
    os.environ.get('MAA_CACHE_DIR')
    or (Path(os.environ.get('XDG_CACHE_HOME') or Path.home()/'.cache') / 'maa')
)
DEFAULT_CACHE = _CACHE_ROOT / 'StageActivityV2.json'
MAX_AGE=3600
MAX_BYTES=2*1024*1024


def validate_activity(data):
    if (not isinstance(data,dict) or not isinstance(data.get('Official'),dict) or
        not isinstance(data['Official'].get('sideStoryStage'),dict)):
        raise ValueError('官方活动目录格式无效')
    return data


def publish_activity(data, path=None):
    path=Path(path) if path is not None else DEFAULT_CACHE
    validate_activity(data)
    atomic_write_text(path,json.dumps(data,ensure_ascii=False)+'\n')


def _configured_proxy_url(config_dir=None):
    try:
        from maa_schedule_control import load_runtime_settings
        settings = load_runtime_settings(config_dir)
        return str((settings.get('network') or {}).get('http_proxy') or '').strip()
    except Exception:
        return ''


def _fetch_activity_bytes(proxy_url: str) -> bytes:
    if proxy_url.lower().startswith(('socks5://', 'socks5h://')):
        esc_url = ACTIVITY_API.replace('\\', '\\\\').replace('"', '\\"')
        esc_proxy = proxy_url.replace('\\', '\\\\').replace('"', '\\"')
        cfg = (
            f'url = "{esc_url}"\n'
            f'proxy = "{esc_proxy}"\n'
            'user-agent = "MAA-ActivityCache/1"\n'
            'silent\nshow-error\nlocation\nfail\n'
            'connect-timeout = 8\nmax-time = 12\n'
        ).encode('utf-8')
        res = subprocess.run(
            ['curl', '--config', '-'],
            input=cfg,
            capture_output=True,
            timeout=15,
            check=True,
        )
        return res.stdout[:MAX_BYTES + 1]
    request = urllib.request.Request(ACTIVITY_API, headers={'User-Agent': 'MAA-ActivityCache/1'})
    if proxy_url:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({'http': proxy_url, 'https': proxy_url})
        )
        with opener.open(request, timeout=10) as response:
            return response.read(MAX_BYTES + 1)
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read(MAX_BYTES + 1)


def get_activity(path=None, *, now=None, config_dir=None):
    """Return (catalog, warning); a failed refresh never overwrites good data."""
    path=Path(path) if path is not None else DEFAULT_CACHE
    now=time.time() if now is None else now
    cached={}
    try:
        cached=validate_activity(json.loads(path.read_text(encoding='utf-8-sig')))
        if 0<=now-path.stat().st_mtime<MAX_AGE:
            return cached,''
    except (OSError,ValueError,TypeError):
        cached={}
    try:
        raw=_fetch_activity_bytes(_configured_proxy_url(config_dir))
        if len(raw)>MAX_BYTES:
            raise ValueError('活动目录超过大小限制')
        data=validate_activity(json.loads(raw))
        publish_activity(data,path)
        return data,''
    except Exception:
        # Proxy/network exception text can contain credentials; expose no raw text.
        return cached,'活动目录刷新失败，保留已验证缓存' if cached else '活动目录不可用，等待重新获取'
