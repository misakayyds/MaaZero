#!/usr/bin/env python3
"""Schedule controls, runtime settings, notifications, and backup helpers for MAA."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid


SKIP_FILENAME = "skip-next-daily.json"
RUNTIME_SETTINGS_FILENAME = "runtime-settings.json"
SECRET_UNCHANGED = "__UNCHANGED__"

CLIENT_PACKAGE_MAP: dict[str, str] = {
    "Official": "com.hypergryph.arknights",
    "Bilibili": "com.hypergryph.arknights.bilibili",
    "YoStarEN": "com.YoStarEN.Arknights",
    "YoStarJP": "com.YoStarJP.Arknights",
    "YoStarKR": "com.YoStarKR.Arknights",
}

CLIENT_SERVER_MAP: dict[str, str] = {
    "Official": "CN",
    "Bilibili": "CN",
    "YoStarEN": "US",
    "YoStarJP": "JP",
    "YoStarKR": "KR",
}

SLEEP_MODES = ("off", "scheduled", "always")
WEBHOOK_PROVIDERS = ("feishu", "dingtalk", "wecom", "generic")
WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

DAILY_CRON_RE = re.compile(
    rb"^[ \t]*(\d{1,2})[ \t]+(\d{1,2})[ \t]+\*[ \t]+\*[ \t]+([^ \t]+)"
    rb"[ \t]+(?:\S*/)?maa-run(?:\.sh)?[ \t]+daily(?:[ \t]|$)"
)
SIMPLE_DAILY_CRON_RE = re.compile(
    rb"^[ \t]*(\d{1,2})[ \t]+(\d{1,2})[ \t]+\*[ \t]+\*[ \t]+\*"
    rb"[ \t]+(?:\S*/)?maa-run(?:\.sh)?[ \t]+daily"
    rb"(?:[ \t]+(?:>|>>)[ \t]*\S*cron\.log(?:[ \t]+2>&1)?)?[ \t]*$"
)
ANY_DAILY_RUN_RE = re.compile(
    rb"(?:^|[ \t])(?:\S*/)?maa-run(?:\.sh)?[ \t]+daily(?:[ \t]|$)"
)
AUTO_UPDATE_CRON_RE = re.compile(
    rb"^[ \t]*(\d{1,2})[ \t]+(\d{1,2})[ \t]+\*[ \t]+\*[ \t]+\*"
    rb"[ \t]+(?:\S*/)?maa-auto-update\.sh"
    rb"(?:[ \t]+(?:>|>>)[ \t]*\S*cron\.log(?:[ \t]+2>&1)?)?[ \t]*$"
)
TIME_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

INFRAST_MANAGED_BEGIN = b"# BEGIN MAA_WEBUI_SCHEDULE"
INFRAST_MANAGED_END = b"# END MAA_WEBUI_SCHEDULE"
OPBOX_MANAGED_BEGIN = b"# BEGIN MAA_WEBUI_OPERATOR_SCAN"
OPBOX_MANAGED_END = b"# END MAA_WEBUI_OPERATOR_SCAN"

EPHEMERAL_TASK_STEM_RE = re.compile(
    r"^_|^operator-scan$|-(?:effective|activity|takeover|medicine)-\d+|^cultivation-(?:phase|closing)-"
)
BACKUP_ALLOWED_PATH_RE = re.compile(
    r"^(?:"
    r"cli\.toml"
    r"|profiles/default\.toml"
    r"|weekly-plan\.json"
    r"|runtime-settings\.json"
    r"|infrast-schedule\.json"
    r"|medicine-plan\.json"
    r"|operator-box\.json"
    r"|operator-box-state\.json"
    r"|cultivation/state\.json"
    r"|tasks/[A-Za-z0-9_-]{1,64}\.toml"
    r"|infrast/[A-Za-z0-9_.-]{1,64}\.json"
    r")$"
)
SCHEDULE_BACKUP_REL_RE = re.compile(
    r"^backups/infrast-schedule/(?:"
    r"latest-activation\.json"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/"
    r"(?:transaction\.json|snapshot\.json|original-[A-Za-z0-9_-]{1,64}\.bin)"
    r")$"
)


def skip_file(config_dir: str | Path | None = None) -> Path:
    target_dir = config_dir or os.environ.get("MAA_CONFIG_DIR", "/root/.config/maa")
    return Path(target_dir) / SKIP_FILENAME


def runtime_settings_file(config_dir: str | Path | None = None) -> Path:
    target_dir = config_dir or os.environ.get("MAA_CONFIG_DIR", "/root/.config/maa")
    return Path(target_dir) / RUNTIME_SETTINGS_FILENAME


def _cron_weekdays(value: str) -> set[int]:
    if value == "*":
        return set(range(7))
    result: set[int] = set()
    for token in value.split(","):
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            start, end = int(start_text) % 7, int(end_text) % 7
            current = start
            while True:
                result.add(current)
                if current == end:
                    break
                current = (current + 1) % 7
        else:
            result.add(int(token) % 7)
    return result


def next_daily_run(crontab_raw: bytes, now: datetime | None = None) -> datetime:
    if not isinstance(crontab_raw, bytes):
        raise ValueError("crontab must be bytes")
    now = now or datetime.now().astimezone()
    entries: list[tuple[int, int, set[int]]] = []
    for line in crontab_raw.splitlines():
        if line.lstrip().startswith(b"#"):
            continue
        match = DAILY_CRON_RE.search(line)
        if not match:
            continue
        minute, hour = int(match.group(1)), int(match.group(2))
        if minute > 59 or hour > 23:
            continue
        try:
            weekdays = _cron_weekdays(match.group(3).decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            continue
        entries.append((hour, minute, weekdays))
    if not entries:
        raise ValueError("没有可识别的 maa-run.sh daily 定时")

    candidates = []
    for offset in range(8):
        day = (now + timedelta(days=offset)).date()
        cron_weekday = (day.weekday() + 1) % 7
        for hour, minute, weekdays in entries:
            if cron_weekday not in weekdays:
                continue
            candidate = datetime.combine(day, datetime.min.time(), tzinfo=now.tzinfo).replace(
                hour=hour, minute=minute
            )
            if candidate > now:
                candidates.append(candidate)
    if not candidates:
        raise ValueError("无法计算下一次日常定时")
    return min(candidates)


def _atomic_write_json(path: Path, payload: dict, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _atomic_write_bytes(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def load_skip_next_daily(config_dir: str | Path) -> dict:
    path = skip_file(config_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"active": False, "scheduled_for": "", "armed_at": ""}
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != 1
        or not isinstance(data.get("scheduled_for"), str)
        or not isinstance(data.get("armed_at"), str)
    ):
        return {"active": False, "scheduled_for": "", "armed_at": ""}
    return {
        "active": True,
        "scheduled_for": data["scheduled_for"],
        "armed_at": data["armed_at"],
    }


def arm_skip_next_daily(
    config_dir: str | Path, crontab_raw: bytes, now: datetime | None = None
) -> dict:
    now = now or datetime.now().astimezone()
    scheduled_for = next_daily_run(crontab_raw, now=now)
    payload = {
        "schema_version": 1,
        "armed_at": now.isoformat(timespec="seconds"),
        "scheduled_for": scheduled_for.isoformat(timespec="seconds"),
    }
    _atomic_write_json(skip_file(config_dir), payload)
    return load_skip_next_daily(config_dir)


def cancel_skip_next_daily(config_dir: str | Path) -> bool:
    try:
        skip_file(config_dir).unlink()
        return True
    except FileNotFoundError:
        return False


def consume_skip_next_daily(config_dir: str | Path, *, is_cron: bool) -> bool:
    if not is_cron:
        return False
    path = skip_file(config_dir)
    claim = path.with_name(f".{path.name}.consumed-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        os.replace(path, claim)
    except FileNotFoundError:
        return False
    try:
        try:
            data = json.loads(claim.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return (
            isinstance(data, dict)
            and data.get("schema_version") == 1
            and isinstance(data.get("scheduled_for"), str)
        )
    finally:
        try:
            claim.unlink()
        except FileNotFoundError:
            pass


def _parent_pid(proc_root: Path, pid: int) -> int | None:
    try:
        for line in (proc_root / str(pid) / "status").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            if line.startswith("PPid:"):
                return int(line.split(":", 1)[1].strip())
    except (OSError, ValueError):
        pass
    return None


def is_cron_invocation(parent_pid: int, proc_root: str | Path = "/proc") -> bool:
    root = Path(proc_root)
    pid = int(parent_pid)
    for _ in range(6):
        if pid <= 1:
            return False
        try:
            comm = (root / str(pid) / "comm").read_text(
                encoding="utf-8", errors="replace"
            ).strip()
        except OSError:
            return False
        if comm.lower() in {"cron", "crond"}:
            return True
        parent = _parent_pid(root, pid)
        if parent is None or parent == pid:
            return False
        pid = parent
    return False


# ---------------------------------------------------------------------------
# Runtime settings (client type, mobile device policy, proxy/mirror, notify)
# ---------------------------------------------------------------------------


def _infer_client_from_daily_toml(config_dir: Path) -> str:
    daily_path = Path(config_dir) / "tasks" / "daily.toml"
    try:
        data = tomllib.loads(daily_path.read_text(encoding="utf-8"))
    except Exception:
        return "Official"
    for section in ("tasks", "disabled_tasks"):
        items = data.get(section)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "StartUp":
                continue
            params = item.get("params")
            if isinstance(params, dict):
                candidate = str(params.get("client_type") or "").strip()
                if candidate in CLIENT_PACKAGE_MAP:
                    return candidate
    return "Official"


def default_runtime_settings(default_client: str = "Official") -> dict:
    client = default_client if default_client in CLIENT_PACKAGE_MAP else "Official"
    return {
        "schema_version": 1,
        "client_type": client,
        "device": {
            "mute_during_run": False,
            "sleep_after_run": "scheduled",
        },
        "network": {
            "http_proxy": "",
            "github_mirror": "",
        },
        "notify": {
            "enabled": False,
            "telegram": {
                "enabled": False,
                "bot_token": "",
                "chat_id": "",
                "api_base": "https://api.telegram.org",
            },
            "pushplus": {
                "enabled": False,
                "token": "",
                "topic": "",
                "endpoint": "",
            },
            "serverchan": {
                "enabled": False,
                "sendkey": "",
                "endpoint": "",
            },
            "bark": {
                "enabled": False,
                "server_url": "https://api.day.app",
                "device_key": "",
            },
            "webhook": {
                "enabled": False,
                "provider": "generic",
                "url": "",
                "sign_secret": "",
            },
        },
    }


def _clean_single_line(value: object, max_len: int = 512) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    if any(ch in cleaned for ch in ("\r", "\n", "\x00")) or len(cleaned) > max_len:
        raise ValueError("字段包含非法换行或超出长度限制")
    return cleaned


def _is_loopback_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    host = hostname.strip().lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        addr = ipaddress.ip_address(host)
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        return addr.is_loopback or addr.is_unspecified
    except ValueError:
        return False


def _validate_url_field(
    value: str,
    field_name: str,
    *,
    allow_empty: bool = True,
    allowed_schemes: tuple[str, ...] = ("http", "https"),
    disallow_loopback: bool = False,
    require_https_unless_loopback: bool = False,
) -> str:
    if not value:
        if allow_empty:
            return ""
        raise ValueError(f"{field_name} 不能为空")
    if value.startswith("-") or any(ch.isspace() or ch in ("\r", "\n", "\x00") for ch in value):
        raise ValueError(f"{field_name} 包含非法前缀或空白字符")
    parsed = urllib.parse.urlparse(value)
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field_name} 端口无效") from exc
    scheme = parsed.scheme.lower()
    if scheme not in allowed_schemes or not parsed.netloc or not parsed.hostname:
        schemes = "/".join(allowed_schemes)
        raise ValueError(f"{field_name} 必须是合法的 {schemes} 地址")
    if require_https_unless_loopback and scheme != "https" and not _is_loopback_host(parsed.hostname):
        raise ValueError(f"{field_name} 必须使用 https:// 协议")
    if (
        disallow_loopback
        and _is_loopback_host(parsed.hostname)
        and os.environ.get("MAA_ALLOW_LOOPBACK_NOTIFY") != "1"
    ):
        raise ValueError(f"{field_name} 不允许指向本机回环地址")
    return value


def _format_hostport(hostname: str | None, port: int | None) -> str:
    host = hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{port}" if port is not None else host


def _mask_proxy_url(url: str) -> tuple[str, bool]:
    if not url:
        return "", False
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
        username = parsed.username
        password = parsed.password
        hostname = parsed.hostname
    except ValueError:
        return "", False
    if username is None and password is None:
        return url, False
    netloc = f"***:***@{_format_hostport(hostname, port)}"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)), True


def _strip_proxy_credentials(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
        username = parsed.username
        password = parsed.password
        hostname = parsed.hostname
    except ValueError:
        return ""
    if username is None and password is None:
        return url
    netloc = _format_hostport(hostname, port)
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _validate_runtime_urls(rt: dict, *, strict: bool) -> dict:
    checks = (
        (
            rt["network"],
            "http_proxy",
            "",
            {
                "field_name": "HTTP 代理地址",
                "allow_empty": True,
                "allowed_schemes": ("http", "https", "socks5", "socks5h"),
            },
        ),
        (
            rt["network"],
            "github_mirror",
            "",
            {
                "field_name": "GitHub 镜像前缀",
                "allow_empty": True,
                "allowed_schemes": ("http", "https"),
                "require_https_unless_loopback": True,
            },
        ),
        (
            rt["notify"]["telegram"],
            "api_base",
            "https://api.telegram.org",
            {
                "field_name": "Telegram API 地址",
                "allow_empty": False,
                "allowed_schemes": ("http", "https"),
                "disallow_loopback": True,
            },
        ),
        (
            rt["notify"]["pushplus"],
            "endpoint",
            "",
            {
                "field_name": "PushPlus 地址",
                "allow_empty": True,
                "allowed_schemes": ("http", "https"),
                "disallow_loopback": True,
            },
        ),
        (
            rt["notify"]["serverchan"],
            "endpoint",
            "",
            {
                "field_name": "Server酱地址",
                "allow_empty": True,
                "allowed_schemes": ("http", "https"),
                "disallow_loopback": True,
            },
        ),
        (
            rt["notify"]["bark"],
            "server_url",
            "https://api.day.app",
            {
                "field_name": "Bark 服务器地址",
                "allow_empty": False,
                "allowed_schemes": ("http", "https"),
                "disallow_loopback": True,
            },
        ),
        (
            rt["notify"]["webhook"],
            "url",
            "",
            {
                "field_name": "Webhook URL",
                "allow_empty": True,
                "allowed_schemes": ("http", "https"),
                "disallow_loopback": True,
            },
        ),
    )
    for container, key, fallback_val, kwargs in checks:
        val = str(container.get(key) or "")
        try:
            validated = _validate_url_field(val, **kwargs)
            if key in ("api_base", "server_url"):
                validated = validated.rstrip("/")
            container[key] = validated
        except ValueError:
            if strict:
                raise
            container[key] = fallback_val
    return rt


def _normalize_runtime_dict(raw: object, fallback_client: str = "Official") -> dict:
    base = default_runtime_settings(fallback_client)
    if not isinstance(raw, dict):
        return base

    client_type = str(raw.get("client_type") or base["client_type"]).strip()
    if client_type in CLIENT_PACKAGE_MAP:
        base["client_type"] = client_type

    device_in = raw.get("device")
    if isinstance(device_in, dict):
        base["device"]["mute_during_run"] = bool(device_in.get("mute_during_run", False))
        sleep_mode = str(device_in.get("sleep_after_run") or "scheduled").strip()
        if sleep_mode in SLEEP_MODES:
            base["device"]["sleep_after_run"] = sleep_mode

    net_in = raw.get("network")
    if isinstance(net_in, dict):
        for key in ("http_proxy", "github_mirror"):
            val = net_in.get(key)
            if isinstance(val, str) and not any(c in val for c in ("\r", "\n", "\x00")):
                base["network"][key] = val.strip()

    notify_in = raw.get("notify")
    if isinstance(notify_in, dict):
        base["notify"]["enabled"] = bool(notify_in.get("enabled", False))

        tg_in = notify_in.get("telegram")
        if isinstance(tg_in, dict):
            base["notify"]["telegram"]["enabled"] = bool(tg_in.get("enabled", False))
            for k in ("bot_token", "chat_id", "api_base"):
                v = tg_in.get(k)
                if isinstance(v, str) and not any(c in v for c in ("\r", "\n", "\x00")):
                    base["notify"]["telegram"][k] = v.strip()
            if not base["notify"]["telegram"]["api_base"]:
                base["notify"]["telegram"]["api_base"] = "https://api.telegram.org"

        pp_in = notify_in.get("pushplus")
        if isinstance(pp_in, dict):
            base["notify"]["pushplus"]["enabled"] = bool(pp_in.get("enabled", False))
            for k in ("token", "topic", "endpoint"):
                v = pp_in.get(k)
                if isinstance(v, str) and not any(c in v for c in ("\r", "\n", "\x00")):
                    base["notify"]["pushplus"][k] = v.strip()

        sc_in = notify_in.get("serverchan")
        if isinstance(sc_in, dict):
            base["notify"]["serverchan"]["enabled"] = bool(sc_in.get("enabled", False))
            for k in ("sendkey", "endpoint"):
                v = sc_in.get(k)
                if isinstance(v, str) and not any(c in v for c in ("\r", "\n", "\x00")):
                    base["notify"]["serverchan"][k] = v.strip()

        bark_in = notify_in.get("bark")
        if isinstance(bark_in, dict):
            base["notify"]["bark"]["enabled"] = bool(bark_in.get("enabled", False))
            for k in ("server_url", "device_key"):
                v = bark_in.get(k)
                if isinstance(v, str) and not any(c in v for c in ("\r", "\n", "\x00")):
                    base["notify"]["bark"][k] = v.strip()
            if not base["notify"]["bark"]["server_url"]:
                base["notify"]["bark"]["server_url"] = "https://api.day.app"

        wh_in = notify_in.get("webhook")
        if isinstance(wh_in, dict):
            base["notify"]["webhook"]["enabled"] = bool(wh_in.get("enabled", False))
            provider = str(wh_in.get("provider") or "generic").strip().lower()
            if provider in WEBHOOK_PROVIDERS:
                base["notify"]["webhook"]["provider"] = provider
            for k in ("url", "sign_secret"):
                v = wh_in.get(k)
                if isinstance(v, str) and not any(c in v for c in ("\r", "\n", "\x00")):
                    base["notify"]["webhook"][k] = v.strip()

    return _validate_runtime_urls(base, strict=False)


def load_runtime_settings(config_dir: str | Path | None = None) -> dict:
    target_dir = Path(config_dir or os.environ.get("MAA_CONFIG_DIR", "/root/.config/maa"))
    fallback_client = _infer_client_from_daily_toml(target_dir)
    path = runtime_settings_file(target_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_runtime_settings(fallback_client)
    return _normalize_runtime_dict(raw, fallback_client)


def resolve_active_client(config_dir: str | Path | None = None) -> tuple[str, str, str]:
    settings = load_runtime_settings(config_dir)
    client_type = settings.get("client_type", "Official")
    if client_type not in CLIENT_PACKAGE_MAP:
        client_type = "Official"
    return (
        client_type,
        CLIENT_PACKAGE_MAP[client_type],
        CLIENT_SERVER_MAP[client_type],
    )


def _read_legacy_tg(config_dir: Path, script_dir: Path | None = None) -> tuple[str, str] | None:
    candidates: list[Path] = []
    env_tg = os.environ.get("MAA_TG_FILE", "").strip()
    if env_tg:
        candidates.append(Path(env_tg))
    if script_dir is not None:
        candidates.append(Path(script_dir) / "tg.txt")
    candidates.append(Path(config_dir) / "tg.txt")
    for candidate in candidates:
        try:
            if not candidate.is_file():
                continue
            lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if len(lines) >= 2 and lines[0].strip() and lines[1].strip():
            return lines[0].strip(), lines[1].strip()
    return None


def public_runtime_settings(
    config_dir: str | Path | None = None,
    script_dir: str | Path | None = None,
) -> dict:
    target_dir = Path(config_dir or os.environ.get("MAA_CONFIG_DIR", "/root/.config/maa"))
    data = load_runtime_settings(target_dir)
    result = json.loads(json.dumps(data))

    net = result["network"]
    masked_proxy, has_proxy_auth = _mask_proxy_url(net.get("http_proxy", ""))
    net["http_proxy"] = masked_proxy
    net["has_proxy_auth"] = has_proxy_auth

    tg = result["notify"]["telegram"]
    tg["has_bot_token"] = bool(tg.get("bot_token"))
    tg["bot_token"] = ""

    pp = result["notify"]["pushplus"]
    pp["has_token"] = bool(pp.get("token"))
    pp["token"] = ""
    pp.pop("endpoint", None)

    sc = result["notify"]["serverchan"]
    sc["has_sendkey"] = bool(sc.get("sendkey"))
    sc["sendkey"] = ""
    sc.pop("endpoint", None)

    bark = result["notify"]["bark"]
    bark["has_device_key"] = bool(bark.get("device_key"))
    bark["device_key"] = ""

    wh = result["notify"]["webhook"]
    wh["has_url"] = bool(wh.get("url"))
    wh["url"] = ""
    wh["has_sign_secret"] = bool(wh.get("sign_secret"))
    wh["sign_secret"] = ""

    result["package_name"] = CLIENT_PACKAGE_MAP.get(result["client_type"], CLIENT_PACKAGE_MAP["Official"])
    result["server_code"] = CLIENT_SERVER_MAP.get(result["client_type"], "CN")
    result["legacy_tg_configured"] = _read_legacy_tg(target_dir, Path(script_dir) if script_dir else None) is not None
    return result


def _merge_secret(incoming: object, current: str, max_len: int = 1024) -> str:
    if incoming is None or incoming == SECRET_UNCHANGED:
        return current
    return _clean_single_line(incoming, max_len=max_len)


def sync_client_type_to_tasks(config_dir: str | Path, client_type: str) -> list[str]:
    if client_type not in CLIENT_PACKAGE_MAP:
        raise ValueError(f"不支持的客户端类型: {client_type}")
    from maa_task_config import StrictTaskConfigError, load_tasks_strict, save_tasks

    server_code = CLIENT_SERVER_MAP[client_type]
    tasks_dir = Path(config_dir) / "tasks"
    targets = [tasks_dir / "daily.toml"] + [
        tasks_dir / f"daily-{day}.toml" for day in WEEKDAY_NAMES
    ]
    synced: list[str] = []
    for path in targets:
        if not path.is_file():
            continue
        try:
            tasks = load_tasks_strict(path)
        except StrictTaskConfigError:
            continue
        changed = False
        for task in tasks:
            task_type = task.get("type")
            params = task.get("params")
            if not isinstance(params, dict):
                params = {}
                task["params"] = params
            if task_type in ("StartUp", "CloseDown"):
                if params.get("client_type") != client_type:
                    params["client_type"] = client_type
                    changed = True
            elif task_type == "Fight":
                if params.get("server") != server_code:
                    params["server"] = server_code
                    changed = True
                if params.get("client_type") and params.get("client_type") != client_type:
                    params["client_type"] = client_type
                    changed = True
            elif task_type == "Recruit":
                if params.get("server") != server_code:
                    params["server"] = server_code
                    changed = True
        if changed:
            if save_tasks(tasks, path):
                synced.append(path.name)
    return synced


def save_runtime_settings(
    config_dir: str | Path,
    payload: dict,
    *,
    sync_tasks: bool = False,
    script_dir: str | Path | None = None,
) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("设置请求体必须是 JSON 对象")
    target_dir = Path(config_dir)
    current = load_runtime_settings(target_dir)

    if "client_type" in payload:
        client_type = _clean_single_line(payload.get("client_type"), max_len=32)
        if client_type not in CLIENT_PACKAGE_MAP:
            raise ValueError(f"不支持的客户端版本: {client_type}")
        current["client_type"] = client_type

    if "device" in payload:
        dev = payload.get("device")
        if not isinstance(dev, dict):
            raise ValueError("device 必须是对象")
        if "mute_during_run" in dev:
            if not isinstance(dev["mute_during_run"], bool):
                raise ValueError("mute_during_run 必须是布尔值")
            current["device"]["mute_during_run"] = dev["mute_during_run"]
        if "sleep_after_run" in dev:
            sleep_mode = _clean_single_line(dev.get("sleep_after_run"), max_len=32)
            if sleep_mode not in SLEEP_MODES:
                raise ValueError("sleep_after_run 必须是 off / scheduled / always")
            current["device"]["sleep_after_run"] = sleep_mode

    if "network" in payload:
        net = payload.get("network")
        if not isinstance(net, dict):
            raise ValueError("network 必须是对象")
        if "http_proxy" in net:
            raw_proxy = net.get("http_proxy")
            cur_proxy = current["network"].get("http_proxy", "")
            masked_cur_proxy, _ = _mask_proxy_url(cur_proxy)
            if raw_proxy == SECRET_UNCHANGED:
                proxy = cur_proxy
            else:
                proxy = _clean_single_line(raw_proxy, max_len=512)
                if proxy and masked_cur_proxy and proxy == masked_cur_proxy:
                    proxy = cur_proxy
            current["network"]["http_proxy"] = _validate_url_field(
                proxy,
                "HTTP 代理地址",
                allow_empty=True,
                allowed_schemes=("http", "https", "socks5", "socks5h"),
            )
        if "github_mirror" in net:
            mirror = _clean_single_line(net.get("github_mirror"), max_len=512)
            current["network"]["github_mirror"] = _validate_url_field(
                mirror,
                "GitHub 镜像前缀",
                allow_empty=True,
                allowed_schemes=("http", "https"),
                require_https_unless_loopback=True,
            )

    if "notify" in payload:
        notify = payload.get("notify")
        if not isinstance(notify, dict):
            raise ValueError("notify 必须是对象")
        if "enabled" in notify:
            if not isinstance(notify["enabled"], bool):
                raise ValueError("notify.enabled 必须是布尔值")
            current["notify"]["enabled"] = notify["enabled"]

        if "telegram" in notify and isinstance(notify["telegram"], dict):
            tg_in = notify["telegram"]
            tg_cur = current["notify"]["telegram"]
            if "enabled" in tg_in:
                tg_cur["enabled"] = bool(tg_in["enabled"])
            tg_cur["bot_token"] = _merge_secret(tg_in.get("bot_token", SECRET_UNCHANGED), tg_cur["bot_token"], max_len=256)
            if "chat_id" in tg_in:
                tg_cur["chat_id"] = _clean_single_line(tg_in.get("chat_id"), max_len=128)
            if "api_base" in tg_in:
                api_base = _clean_single_line(tg_in.get("api_base"), max_len=256) or "https://api.telegram.org"
                tg_cur["api_base"] = _validate_url_field(
                    api_base,
                    "Telegram API 地址",
                    allow_empty=False,
                    allowed_schemes=("http", "https"),
                    disallow_loopback=True,
                ).rstrip("/")
            if tg_cur["enabled"] and (not tg_cur["bot_token"] or not tg_cur["chat_id"]):
                raise ValueError("启用 Telegram 通知时必须填写 Bot Token 和 Chat ID")

        if "pushplus" in notify and isinstance(notify["pushplus"], dict):
            pp_in = notify["pushplus"]
            pp_cur = current["notify"]["pushplus"]
            if "enabled" in pp_in:
                pp_cur["enabled"] = bool(pp_in["enabled"])
            pp_cur["token"] = _merge_secret(pp_in.get("token", SECRET_UNCHANGED), pp_cur["token"], max_len=256)
            if "topic" in pp_in:
                pp_cur["topic"] = _clean_single_line(pp_in.get("topic"), max_len=128)
            if "endpoint" in pp_in:
                ep = _clean_single_line(pp_in.get("endpoint"), max_len=256)
                pp_cur["endpoint"] = _validate_url_field(
                    ep, "PushPlus 地址", allow_empty=True, disallow_loopback=True
                )
            if pp_cur["enabled"] and not pp_cur["token"]:
                raise ValueError("启用 PushPlus 通知时必须填写 Token")

        if "serverchan" in notify and isinstance(notify["serverchan"], dict):
            sc_in = notify["serverchan"]
            sc_cur = current["notify"]["serverchan"]
            if "enabled" in sc_in:
                sc_cur["enabled"] = bool(sc_in["enabled"])
            sc_cur["sendkey"] = _merge_secret(sc_in.get("sendkey", SECRET_UNCHANGED), sc_cur["sendkey"], max_len=256)
            if "endpoint" in sc_in:
                ep = _clean_single_line(sc_in.get("endpoint"), max_len=256)
                sc_cur["endpoint"] = _validate_url_field(
                    ep, "Server酱地址", allow_empty=True, disallow_loopback=True
                )
            if sc_cur["enabled"] and not sc_cur["sendkey"]:
                raise ValueError("启用 Server酱 通知时必须填写 SendKey")

        if "bark" in notify and isinstance(notify["bark"], dict):
            bk_in = notify["bark"]
            bk_cur = current["notify"]["bark"]
            if "enabled" in bk_in:
                bk_cur["enabled"] = bool(bk_in["enabled"])
            if "server_url" in bk_in:
                srv = _clean_single_line(bk_in.get("server_url"), max_len=256) or "https://api.day.app"
                bk_cur["server_url"] = _validate_url_field(
                    srv,
                    "Bark 服务器地址",
                    allow_empty=False,
                    allowed_schemes=("http", "https"),
                    disallow_loopback=True,
                ).rstrip("/")
            bk_cur["device_key"] = _merge_secret(
                bk_in.get("device_key", SECRET_UNCHANGED), bk_cur["device_key"], max_len=256
            )
            if bk_cur["enabled"] and not bk_cur["device_key"]:
                raise ValueError("启用 Bark 通知时必须填写 Device Key")

        if "webhook" in notify and isinstance(notify["webhook"], dict):
            wh_in = notify["webhook"]
            wh_cur = current["notify"]["webhook"]
            if "enabled" in wh_in:
                wh_cur["enabled"] = bool(wh_in["enabled"])
            if "provider" in wh_in:
                provider = _clean_single_line(wh_in.get("provider"), max_len=32).lower()
                if provider not in WEBHOOK_PROVIDERS:
                    raise ValueError("Webhook 类型必须是 feishu / dingtalk / wecom / generic")
                wh_cur["provider"] = provider
            wh_cur["url"] = _merge_secret(wh_in.get("url", SECRET_UNCHANGED), wh_cur["url"], max_len=1024)
            if wh_cur["url"]:
                wh_cur["url"] = _validate_url_field(
                    wh_cur["url"],
                    "Webhook URL",
                    allow_empty=False,
                    allowed_schemes=("http", "https"),
                    disallow_loopback=True,
                )
            wh_cur["sign_secret"] = _merge_secret(
                wh_in.get("sign_secret", SECRET_UNCHANGED), wh_cur["sign_secret"], max_len=256
            )
            if wh_cur["enabled"] and not wh_cur["url"]:
                raise ValueError("启用 Webhook 通知时必须填写 Webhook URL")

    _atomic_write_json(runtime_settings_file(target_dir), current, mode=0o600)
    synced_files = sync_client_type_to_tasks(target_dir, current["client_type"]) if sync_tasks else []
    result = public_runtime_settings(target_dir, script_dir=script_dir)
    result["synced_files"] = synced_files
    return result


# ---------------------------------------------------------------------------
# Visual crontab parser & editor
# ---------------------------------------------------------------------------


def _default_runner_path() -> str:
    raw = os.environ.get("MAA_RUN_SCRIPT") or str(
        Path(os.environ.get("MAA_HOME", "/root/maa-scripts")) / "maa-run.sh"
    )
    return raw.replace("\\", "/")


def _default_auto_update_path() -> str:
    return str(
        Path(os.environ.get("MAA_HOME", "/root/maa-scripts")) / "maa-auto-update.sh"
    ).replace("\\", "/")


def _default_cron_log_path() -> str:
    return str(
        Path(os.environ.get("MAA_LOG_DIR", "/var/log/maa")) / "cron.log"
    ).replace("\\", "/")


def _managed_ranges(logical_lines: list[bytes]) -> tuple[bool, set[int]]:
    protected: set[int] = set()
    infrast_managed = False
    for begin_mark, end_mark, is_infrast in (
        (INFRAST_MANAGED_BEGIN, INFRAST_MANAGED_END, True),
        (OPBOX_MANAGED_BEGIN, OPBOX_MANAGED_END, False),
    ):
        begins = [i for i, line in enumerate(logical_lines) if line == begin_mark]
        ends = [i for i, line in enumerate(logical_lines) if line == end_mark]
        if begins or ends:
            if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0]:
                raise ValueError("crontab 中的受管排班区块标记不完整，请在高级模式检查")
            if is_infrast:
                infrast_managed = True
            protected.update(range(begins[0], ends[0] + 1))
    return infrast_managed, protected


def parse_visual_crontab(crontab_raw: bytes, now: datetime | None = None) -> dict:
    if not isinstance(crontab_raw, bytes):
        raise ValueError("crontab 必须是字节串")
    raw_lines = crontab_raw.splitlines()
    logical = [line.rstrip(b"\r") for line in raw_lines]
    infrast_managed, protected = _managed_ranges(logical)

    daily_times: list[str] = []
    custom_daily_count = 0
    update_times: list[str] = []

    for idx, line in enumerate(logical):
        if idx in protected:
            continue
        stripped = line.lstrip()
        if not stripped or stripped.startswith(b"#"):
            continue
        m_daily = SIMPLE_DAILY_CRON_RE.match(line)
        if m_daily:
            minute, hour = int(m_daily.group(1)), int(m_daily.group(2))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                hhmm = f"{hour:02d}:{minute:02d}"
                if hhmm not in daily_times:
                    daily_times.append(hhmm)
                continue
        if ANY_DAILY_RUN_RE.search(line):
            custom_daily_count += 1
            continue
        m_upd = AUTO_UPDATE_CRON_RE.match(line)
        if m_upd:
            minute, hour = int(m_upd.group(1)), int(m_upd.group(2))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                hhmm = f"{hour:02d}:{minute:02d}"
                if hhmm not in update_times:
                    update_times.append(hhmm)

    daily_times.sort()
    update_times.sort()
    try:
        next_iso = next_daily_run(crontab_raw, now=now).isoformat(timespec="seconds")
    except ValueError:
        next_iso = ""

    return {
        "infrast_managed": infrast_managed,
        "daily_enabled": bool(daily_times),
        "daily_times": daily_times,
        "custom_daily_count": custom_daily_count,
        "update_enabled": bool(update_times),
        "update_time": update_times[0] if update_times else "03:30",
        "next_daily_for": next_iso,
    }


def _normalize_hhmm(value: object, label: str) -> tuple[int, int, str]:
    if not isinstance(value, str):
        raise ValueError(f"{label} 必须是 HH:MM 字符串")
    text = value.strip()
    match = TIME_HHMM_RE.fullmatch(text)
    if not match:
        raise ValueError(f"{label} 格式无效（需要 00:00 至 23:59）: {text}")
    hour, minute = int(match.group(1)), int(match.group(2))
    return hour, minute, f"{hour:02d}:{minute:02d}"


def apply_visual_crontab(
    crontab_raw: bytes,
    *,
    daily_enabled: bool | None,
    daily_times: list[str] | None = None,
    update_enabled: bool = False,
    update_time: str = "03:30",
) -> bytes:
    if not isinstance(crontab_raw, bytes):
        raise ValueError("crontab 必须是字节串")
    raw_lines = crontab_raw.splitlines()
    logical = [line.rstrip(b"\r") for line in raw_lines]
    infrast_managed, protected = _managed_ranges(logical)

    if daily_enabled is not None and infrast_managed:
        raise ValueError("当前日常定时已由“周基建排班”托管；请先在周基建排班页停用托管，或仅保存自动更新时间")

    normalized_daily: list[tuple[int, int, str]] = []
    if daily_enabled:
        if not isinstance(daily_times, list) or not daily_times:
            raise ValueError("启用每日定时刷取时请至少保留 1 个运行时间点")
        if len(daily_times) > 12:
            raise ValueError("每日运行时间点最多支持 12 个")
        seen_hhmm: set[str] = set()
        for item in daily_times:
            h, m, hhmm = _normalize_hhmm(item, "每日运行时间")
            if hhmm not in seen_hhmm:
                seen_hhmm.add(hhmm)
                normalized_daily.append((h, m, hhmm))
        normalized_daily.sort()

    if update_enabled or (isinstance(update_time, str) and update_time.strip()):
        upd_hour, upd_min, _ = _normalize_hhmm(update_time, "每日自动更新时间")
    else:
        upd_hour, upd_min = 3, 30

    # Ensure we do not clobber custom non-standard daily cron lines when modifying daily schedule.
    if daily_enabled is not None:
        for idx, line in enumerate(logical):
            if idx in protected:
                continue
            stripped = line.lstrip()
            if not stripped or stripped.startswith(b"#"):
                continue
            if ANY_DAILY_RUN_RE.search(line) and not SIMPLE_DAILY_CRON_RE.match(line):
                raise ValueError("检测到自定义复杂参数的 maa-run.sh daily 定时行，请展开下方高级 crontab 编辑器修改")

    runner = _default_runner_path()
    updater = _default_auto_update_path()
    cron_log = _default_cron_log_path()
    for p in (runner, updater, cron_log):
        if not p or any(ch.isspace() or ch in ';&|`$()<>' for ch in p):
            raise ValueError("运行脚本或日志路径包含空白或控制字符，请在高级编辑器中设置 crontab")

    kept_lines: list[bytes] = []
    daily_insert_at: int | None = None
    update_insert_at: int | None = None

    for idx, line in enumerate(logical):
        if idx in protected:
            kept_lines.append(line)
            continue
        stripped = line.lstrip()
        if stripped and not stripped.startswith(b"#"):
            if daily_enabled is not None and SIMPLE_DAILY_CRON_RE.match(line):
                if daily_insert_at is None:
                    daily_insert_at = len(kept_lines)
                continue
            if AUTO_UPDATE_CRON_RE.match(line):
                if update_insert_at is None:
                    update_insert_at = len(kept_lines)
                continue
        kept_lines.append(line)

    new_daily_lines = [
        f"{minute} {hour} * * * {runner} daily >> {cron_log} 2>&1".encode("utf-8")
        for hour, minute, _ in normalized_daily
    ]
    new_update_lines = (
        [f"{upd_min} {upd_hour} * * * {updater} >> {cron_log} 2>&1".encode("utf-8")]
        if update_enabled
        else []
    )

    if daily_enabled is not None and new_daily_lines:
        pos = len(kept_lines) if daily_insert_at is None else daily_insert_at
        kept_lines[pos:pos] = new_daily_lines
        if update_insert_at is not None and update_insert_at >= pos:
            update_insert_at += len(new_daily_lines)

    if new_update_lines:
        pos = len(kept_lines) if update_insert_at is None else update_insert_at
        kept_lines[pos:pos] = new_update_lines

    while kept_lines and kept_lines[-1] == b"":
        kept_lines.pop()
    if not kept_lines:
        return b""
    return b"\n".join(kept_lines) + b"\n"


# ---------------------------------------------------------------------------
# Multi-channel notification dispatcher
# ---------------------------------------------------------------------------


def _curl_cfg_quote(val: str) -> str:
    return val.replace("\\", "\\\\").replace('"', '\\"')


def _http_post_json(
    url: str,
    payload: dict,
    *,
    proxy_url: str = "",
    timeout_s: float = 15.0,
) -> tuple[int, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if proxy_url.lower().startswith(("socks5://", "socks5h://")):
        fd, body_path = tempfile.mkstemp(prefix=".maa-notify-body.")
        try:
            os.chmod(body_path, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
            curl_cfg = (
                f'url = "{_curl_cfg_quote(url)}"\n'
                f'proxy = "{_curl_cfg_quote(proxy_url)}"\n'
                'request = "POST"\n'
                'header = "Content-Type: application/json; charset=utf-8"\n'
                'header = "User-Agent: MaaZero-Notifier/1.0"\n'
                f'data-binary = "@{_curl_cfg_quote(body_path)}"\n'
                'write-out = "\\n%{http_code}"\n'
                "connect-timeout = 8\n"
                f"max-time = {max(5, int(timeout_s))}\n"
            )
            completed = subprocess.run(
                ["curl", "-sS", "--config", "-"],
                input=curl_cfg.encode("utf-8"),
                capture_output=True,
                timeout=timeout_s + 5,
                check=False,
            )
        finally:
            try:
                os.unlink(body_path)
            except FileNotFoundError:
                pass
        if completed.returncode != 0:
            return 0, "curl_failed"
        out = completed.stdout.decode("utf-8", errors="replace")
        body_text, _, code_text = out.rpartition("\n")
        try:
            return int(code_text.strip()), body_text[:400]
        except ValueError:
            return 0, "invalid_http_code"

    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "MaaZero-Notifier/1.0",
        },
        method="POST",
    )
    try:
        if proxy_url:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            )
            ctx = opener.open(request, timeout=timeout_s)
        else:
            ctx = urllib.request.urlopen(request, timeout=timeout_s)
        with ctx as response:
            resp_body = response.read(2048).decode("utf-8", errors="replace")
            return int(response.status), resp_body
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read(512).decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        return int(exc.code), err_body
    except Exception as exc:
        return 0, type(exc).__name__


def _send_with_retries(
    url: str,
    payload: dict,
    *,
    proxy_url: str,
    retries: int = 2,
    retry_delay_s: float = 1.5,
    validator=None,
) -> tuple[bool, str]:
    total = max(1, int(retries))
    last_reason = "request_failed"
    for attempt in range(total):
        status, resp_text = _http_post_json(url, payload, proxy_url=proxy_url)
        if 200 <= status < 300:
            if validator is None:
                return True, "ok"
            ok, reason = validator(resp_text)
            if ok:
                return True, "ok"
            last_reason = reason
        elif status > 0:
            last_reason = f"HTTP {status}"
        else:
            last_reason = resp_text or "network_error"
        if attempt + 1 < total and retry_delay_s > 0:
            time.sleep(retry_delay_s)
    return False, last_reason


def _check_json_code(expected_codes: set[int]):
    def _validate(resp_text: str) -> tuple[bool, str]:
        if not resp_text.strip():
            return True, "ok"
        try:
            obj = json.loads(resp_text)
        except json.JSONDecodeError:
            return True, "ok"
        if not isinstance(obj, dict):
            return True, "ok"
        for key in ("code", "errcode", "StatusCode"):
            if key in obj and isinstance(obj[key], int):
                if obj[key] in expected_codes:
                    return True, "ok"
                return False, f"api_{key}={obj[key]}"
        if obj.get("ok") is False:
            return False, "api_ok=false"
        return True, "ok"

    return _validate


def _serverchan_url(sendkey: str, custom_endpoint: str = "") -> str:
    if custom_endpoint:
        return custom_endpoint
    match = re.match(r"^sctp(\d+)t", sendkey)
    if match:
        uid = match.group(1)
        return f"https://{uid}.push.ft07.com/send/{urllib.parse.quote(sendkey)}.send"
    return f"https://sctapi.ftqq.com/{urllib.parse.quote(sendkey)}.send"


def _webhook_request_spec(wh: dict, title: str, text: str) -> tuple[str, dict]:
    provider = wh.get("provider", "generic")
    url = wh["url"]
    secret = wh.get("sign_secret", "")
    full_text = f"{title}\n{text}" if title and not text.startswith(title) else text

    if provider == "feishu":
        payload: dict = {"msg_type": "text", "content": {"text": full_text}}
        if secret:
            ts = str(int(time.time()))
            to_sign = f"{ts}\n{secret}".encode("utf-8")
            digest = hmac.new(to_sign, digestmod=hashlib.sha256).digest()
            payload["timestamp"] = ts
            payload["sign"] = base64.b64encode(digest).decode("ascii")
        return url, payload

    if provider == "dingtalk":
        target_url = url
        if secret:
            ts = str(int(time.time() * 1000))
            to_sign = f"{ts}\n{secret}".encode("utf-8")
            digest = hmac.new(secret.encode("utf-8"), to_sign, digestmod=hashlib.sha256).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(digest))
            sep = "&" if "?" in target_url else "?"
            target_url = f"{target_url}{sep}timestamp={ts}&sign={sign}"
        return target_url, {"msgtype": "text", "text": {"content": full_text}}

    if provider == "wecom":
        return url, {"msgtype": "text", "text": {"content": full_text}}

    return url, {"title": title, "text": text, "content": full_text}


def send_notifications(
    text: str,
    *,
    title: str = "[MaaZero] 运行通知",
    config_dir: str | Path | None = None,
    script_dir: str | Path | None = None,
    force_test: bool = False,
    retries: int = 2,
    retry_delay_s: float = 1.5,
) -> dict:
    target_dir = Path(config_dir or os.environ.get("MAA_CONFIG_DIR", "/root/.config/maa"))
    settings = load_runtime_settings(target_dir)
    proxy_url = settings.get("network", {}).get("http_proxy", "")
    notify = settings.get("notify", {})

    results: list[dict[str, object]] = []
    use_configured = bool(notify.get("enabled")) or force_test

    if use_configured:
        tg = notify.get("telegram", {})
        if tg.get("enabled") and tg.get("bot_token") and tg.get("chat_id"):
            api_base = (tg.get("api_base") or "https://api.telegram.org").rstrip("/")
            url = f"{api_base}/bot{tg['bot_token']}/sendMessage"
            ok, detail = _send_with_retries(
                url,
                {"chat_id": tg["chat_id"], "text": text},
                proxy_url=proxy_url,
                retries=retries,
                retry_delay_s=retry_delay_s,
                validator=_check_json_code({0, 200}),
            )
            results.append({"channel": "telegram", "ok": ok, "detail": detail})

        pp = notify.get("pushplus", {})
        if pp.get("enabled") and pp.get("token"):
            url = pp.get("endpoint") or "https://www.pushplus.plus/send"
            payload = {
                "token": pp["token"],
                "title": title,
                "content": text,
                "template": "txt",
            }
            if pp.get("topic"):
                payload["topic"] = pp["topic"]
            ok, detail = _send_with_retries(
                url,
                payload,
                proxy_url=proxy_url,
                retries=retries,
                retry_delay_s=retry_delay_s,
                validator=_check_json_code({200}),
            )
            results.append({"channel": "pushplus", "ok": ok, "detail": detail})

        sc = notify.get("serverchan", {})
        if sc.get("enabled") and sc.get("sendkey"):
            url = _serverchan_url(sc["sendkey"], sc.get("endpoint", ""))
            ok, detail = _send_with_retries(
                url,
                {"title": title, "desp": text},
                proxy_url=proxy_url,
                retries=retries,
                retry_delay_s=retry_delay_s,
                validator=_check_json_code({0}),
            )
            results.append({"channel": "serverchan", "ok": ok, "detail": detail})

        bk = notify.get("bark", {})
        if bk.get("enabled") and bk.get("device_key"):
            base_url = (bk.get("server_url") or "https://api.day.app").rstrip("/")
            ok, detail = _send_with_retries(
                f"{base_url}/push",
                {
                    "device_key": bk["device_key"],
                    "title": title,
                    "body": text,
                    "group": "MaaZero",
                },
                proxy_url=proxy_url,
                retries=retries,
                retry_delay_s=retry_delay_s,
                validator=_check_json_code({200}),
            )
            results.append({"channel": "bark", "ok": ok, "detail": detail})

        wh = notify.get("webhook", {})
        if wh.get("enabled") and wh.get("url"):
            url, payload = _webhook_request_spec(wh, title, text)
            ok, detail = _send_with_retries(
                url,
                payload,
                proxy_url=proxy_url,
                retries=retries,
                retry_delay_s=retry_delay_s,
                validator=_check_json_code({0, 200}),
            )
            results.append({"channel": f"webhook:{wh.get('provider', 'generic')}", "ok": ok, "detail": detail})

    has_rt_file = runtime_settings_file(target_dir).is_file()
    if not results and ((not has_rt_file) or use_configured):
        legacy = _read_legacy_tg(target_dir, Path(script_dir) if script_dir else None)
        if legacy is not None:
            token, chat_id = legacy
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            ok, detail = _send_with_retries(
                url,
                {"chat_id": chat_id, "text": text},
                proxy_url=proxy_url,
                retries=retries,
                retry_delay_s=retry_delay_s,
                validator=_check_json_code({0, 200}),
            )
            results.append({"channel": "telegram_legacy", "ok": ok, "detail": detail})

    attempted = len(results)
    succeeded = sum(1 for item in results if item.get("ok") is True)
    return {
        "attempted": attempted,
        "succeeded": succeeded,
        "any_ok": succeeded > 0,
        "all_ok": attempted > 0 and succeeded == attempted,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Configuration backup export & restore
# ---------------------------------------------------------------------------


def _has_symlink_component(root: Path, rel: str = "") -> bool:
    if root.is_symlink():
        return True
    cur = root
    for part in Path(rel).parts:
        if part in ("", "."):
            continue
        cur = cur / part
        if cur.is_symlink():
            return True
    return False


def export_config_backup(
    config_dir: str | Path,
    crontab_raw: bytes = b"",
    *,
    include_secrets: bool = False,
) -> dict:
    root = Path(config_dir)
    if root.is_symlink():
        raise ValueError("配置根目录不能是符号链接")
    files: dict[str, str] = {}

    fixed_candidates = (
        "cli.toml",
        "profiles/default.toml",
        "weekly-plan.json",
        "infrast-schedule.json",
        "medicine-plan.json",
        "operator-box.json",
        "operator-box-state.json",
        "cultivation/state.json",
    )
    for rel in fixed_candidates:
        if _has_symlink_component(root, rel):
            continue
        full = root / rel
        if full.is_file():
            try:
                files[rel] = full.read_text(encoding="utf-8")
            except OSError:
                pass

    if not _has_symlink_component(root, RUNTIME_SETTINGS_FILENAME):
        rt_file = root / RUNTIME_SETTINGS_FILENAME
        if rt_file.is_file():
            rt_payload = load_runtime_settings(root)
            if not include_secrets:
                rt_payload["network"]["http_proxy"] = _strip_proxy_credentials(
                    rt_payload["network"].get("http_proxy", "")
                )
                rt_payload["notify"]["telegram"]["bot_token"] = ""
                rt_payload["notify"]["pushplus"]["token"] = ""
                rt_payload["notify"]["serverchan"]["sendkey"] = ""
                rt_payload["notify"]["bark"]["device_key"] = ""
                rt_payload["notify"]["webhook"]["url"] = ""
                rt_payload["notify"]["webhook"]["sign_secret"] = ""
            files[RUNTIME_SETTINGS_FILENAME] = (
                json.dumps(rt_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            )

    tasks_dir = root / "tasks"
    if tasks_dir.is_dir() and not _has_symlink_component(root, "tasks"):
        for path in sorted(tasks_dir.glob("*.toml")):
            if path.is_symlink() or EPHEMERAL_TASK_STEM_RE.search(path.stem):
                continue
            rel = f"tasks/{path.name}"
            if BACKUP_ALLOWED_PATH_RE.fullmatch(rel):
                try:
                    files[rel] = path.read_text(encoding="utf-8")
                except OSError:
                    pass

    infrast_dir = root / "infrast"
    if infrast_dir.is_dir() and not _has_symlink_component(root, "infrast"):
        for path in sorted(infrast_dir.glob("*.json")):
            if path.is_symlink():
                continue
            rel = f"infrast/{path.name}"
            if BACKUP_ALLOWED_PATH_RE.fullmatch(rel):
                try:
                    files[rel] = path.read_text(encoding="utf-8")
                except OSError:
                    pass

    schedule_activation: dict[str, str] = {}
    ptr_rel = "backups/infrast-schedule/latest-activation.json"
    if not _has_symlink_component(root, ptr_rel) and (root / ptr_rel).is_file():
        try:
            ptr_raw = (root / ptr_rel).read_bytes()
            ptr_obj = json.loads(ptr_raw.decode("utf-8"))
            snap_id = str(ptr_obj.get("snapshot_id") or "")
            parsed_uuid = uuid.UUID(snap_id)
            if str(parsed_uuid) == snap_id:
                tx_rel_dir = f"backups/infrast-schedule/{snap_id}"
                tx_dir = root / tx_rel_dir
                if not _has_symlink_component(root, tx_rel_dir) and tx_dir.is_dir():
                    act_items: dict[str, str] = {
                        ptr_rel: base64.b64encode(ptr_raw).decode("ascii")
                    }
                    total_bytes = len(ptr_raw)
                    for child in sorted(tx_dir.iterdir()):
                        if child.is_symlink() or not child.is_file():
                            continue
                        child_rel = f"{tx_rel_dir}/{child.name}"
                        if not SCHEDULE_BACKUP_REL_RE.fullmatch(child_rel):
                            continue
                        child_bytes = child.read_bytes()
                        total_bytes += len(child_bytes)
                        if total_bytes > 2_097_152:
                            act_items = {}
                            break
                        act_items[child_rel] = base64.b64encode(child_bytes).decode("ascii")
                    if (
                        f"{tx_rel_dir}/transaction.json" in act_items
                        and f"{tx_rel_dir}/snapshot.json" in act_items
                    ):
                        schedule_activation = act_items
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            schedule_activation = {}

    result = {
        "schema_version": 1,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "includes_secrets": bool(include_secrets),
        "crontab": crontab_raw.decode("utf-8", errors="replace") if isinstance(crontab_raw, bytes) else "",
        "files": files,
    }
    if schedule_activation:
        result["schedule_activation"] = schedule_activation
    return result


def import_config_backup(
    config_dir: str | Path,
    bundle: dict,
    *,
    install_crontab_fn=None,
) -> dict:
    if not isinstance(bundle, dict) or bundle.get("schema_version") != 1:
        raise ValueError("备份文件格式无效（schema_version 必须为 1）")
    files = bundle.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("备份文件中没有可恢复的配置项")
    if len(files) > 64:
        raise ValueError("备份文件条目数量超出上限 (64)")

    from maa_infrast_plan import parse_plan_document
    from maa_task_config import validate_task_bytes

    raw_root = Path(config_dir)
    if raw_root.is_symlink():
        raise ValueError("配置根目录不能是符号链接")
    root = raw_root.resolve()
    includes_secrets = bool(bundle.get("includes_secrets"))
    existing_rt = load_runtime_settings(root)
    staged: list[tuple[Path, bytes, int]] = []

    for rel, content in sorted(files.items()):
        if not isinstance(rel, str) or not BACKUP_ALLOWED_PATH_RE.fullmatch(rel):
            raise ValueError(f"拒绝恢复不允许的路径: {rel}")
        if not isinstance(content, str):
            raise ValueError(f"文件内容必须是字符串: {rel}")
        encoded = content.encode("utf-8")
        if len(encoded) > 524_288:
            raise ValueError(f"文件体积超出上限 (512 KiB): {rel}")

        if _has_symlink_component(root, rel):
            raise ValueError(f"拒绝写入符号链接路径: {rel}")
        dest = (root / rel).resolve()
        try:
            dest.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"路径越界: {rel}") from exc

        if rel.startswith("tasks/") and rel.endswith(".toml"):
            valid, err = validate_task_bytes(encoded)
            if not valid:
                raise ValueError(f"任务配置校验失败 ({rel}): {err}")
        elif rel.endswith(".toml"):
            if content.strip():
                try:
                    tomllib.loads(content)
                except tomllib.TOMLDecodeError as exc:
                    raise ValueError(f"TOML 语法无效 ({rel}): {exc}") from exc
        elif rel == RUNTIME_SETTINGS_FILENAME:
            try:
                parsed_rt = json.loads(content)
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSON 语法无效 ({rel}): {exc}") from exc
            if not isinstance(parsed_rt, dict):
                raise ValueError(f"运行设置必须是 JSON 对象 ({rel})")
            # Validate raw URL fields strictly before normalization resets any invalid entry
            net_raw = parsed_rt.get("network")
            if isinstance(net_raw, dict):
                if isinstance(net_raw.get("http_proxy"), str):
                    _validate_url_field(
                        net_raw["http_proxy"].strip(),
                        "HTTP 代理地址",
                        allow_empty=True,
                        allowed_schemes=("http", "https", "socks5", "socks5h"),
                    )
                if isinstance(net_raw.get("github_mirror"), str):
                    _validate_url_field(
                        net_raw["github_mirror"].strip(),
                        "GitHub 镜像前缀",
                        allow_empty=True,
                        allowed_schemes=("http", "https"),
                        require_https_unless_loopback=True,
                    )
            notify_raw = parsed_rt.get("notify")
            if isinstance(notify_raw, dict):
                for ch_name, fld_name, label, allow_emp in (
                    ("telegram", "api_base", "Telegram API 地址", True),
                    ("pushplus", "endpoint", "PushPlus 地址", True),
                    ("serverchan", "endpoint", "Server酱地址", True),
                    ("bark", "server_url", "Bark 服务器地址", True),
                    ("webhook", "url", "Webhook URL", True),
                ):
                    ch_obj = notify_raw.get(ch_name)
                    if isinstance(ch_obj, dict) and isinstance(ch_obj.get(fld_name), str):
                        _validate_url_field(
                            ch_obj[fld_name].strip(),
                            label,
                            allow_empty=allow_emp,
                            allowed_schemes=("http", "https"),
                            disallow_loopback=True,
                        )
            normalized_rt = _normalize_runtime_dict(parsed_rt)
            _validate_runtime_urls(normalized_rt, strict=True)
            if not includes_secrets:
                inc_proxy = normalized_rt["network"].get("http_proxy", "")
                cur_proxy = existing_rt["network"].get("http_proxy", "")
                if (
                    inc_proxy
                    and not _mask_proxy_url(inc_proxy)[1]
                    and _mask_proxy_url(cur_proxy)[1]
                    and inc_proxy == _strip_proxy_credentials(cur_proxy)
                ):
                    normalized_rt["network"]["http_proxy"] = cur_proxy
                for ch, field in (
                    ("telegram", "bot_token"),
                    ("pushplus", "token"),
                    ("serverchan", "sendkey"),
                    ("bark", "device_key"),
                    ("webhook", "url"),
                    ("webhook", "sign_secret"),
                ):
                    if not normalized_rt["notify"][ch].get(field):
                        normalized_rt["notify"][ch][field] = existing_rt["notify"][ch].get(field, "")
            encoded = (
                json.dumps(normalized_rt, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            ).encode("utf-8")
        elif rel.startswith("infrast/") and rel.endswith(".json"):
            try:
                parse_plan_document(encoded, Path(rel).name)
            except Exception as exc:
                raise ValueError(f"基建排班文件校验失败 ({rel}): {exc}") from exc
        elif rel.endswith(".json"):
            try:
                json.loads(content)
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSON 语法无效 ({rel}): {exc}") from exc

        mode = 0o600 if rel == RUNTIME_SETTINGS_FILENAME else 0o644
        staged.append((dest, encoded, mode))

    staged_activation: list[tuple[Path, bytes]] = []
    sched_act = bundle.get("schedule_activation")
    if sched_act is not None:
        if not isinstance(sched_act, dict) or len(sched_act) > 32:
            raise ValueError("备份中的排班激活基线格式无效")
        for rel, b64_text in sorted(sched_act.items()):
            if not isinstance(rel, str) or not SCHEDULE_BACKUP_REL_RE.fullmatch(rel):
                raise ValueError(f"拒绝恢复不允许的排班基线路径: {rel}")
            if not isinstance(b64_text, str):
                raise ValueError(f"排班基线内容无效: {rel}")
            try:
                raw_bytes = base64.b64decode(b64_text.encode("ascii"), validate=True)
            except Exception as exc:
                raise ValueError(f"排班基线编码无效 ({rel})") from exc
            if len(raw_bytes) > 524_288:
                raise ValueError(f"排班基线文件体积超限: {rel}")
            if _has_symlink_component(root, rel):
                raise ValueError(f"拒绝写入符号链接路径: {rel}")
            dest = (root / rel).resolve()
            try:
                dest.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"路径越界: {rel}") from exc
            if rel.endswith(".json"):
                try:
                    parsed_act_json = json.loads(raw_bytes.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError(f"排班基线 JSON 无效 ({rel})") from exc
                if not isinstance(parsed_act_json, dict):
                    raise ValueError(f"排班基线 JSON 必须是对象 ({rel})")
                if rel.endswith("/transaction.json") and (
                    parsed_act_json.get("state") != "committed"
                    or parsed_act_json.get("activation") is not True
                ):
                    raise ValueError(f"排班基线事务状态无效 ({rel})")
            staged_activation.append((dest, raw_bytes))

    crontab_bytes: bytes | None = None
    if "crontab" in bundle and install_crontab_fn is not None:
        crontab_text = bundle.get("crontab")
        if not isinstance(crontab_text, str) or "\x00" in crontab_text or len(crontab_text) > 65_536:
            raise ValueError("备份中的 crontab 内容无效")
        if not crontab_text.strip():
            crontab_bytes = b""
        else:
            crontab_bytes = crontab_text.replace("\r\n", "\n").encode("utf-8")
            if not crontab_bytes.endswith(b"\n"):
                crontab_bytes += b"\n"

    for dest, encoded, mode in staged:
        _atomic_write_bytes(dest, encoded, mode=mode)
    for dest, raw_bytes in staged_activation:
        _atomic_write_bytes(dest, raw_bytes, mode=0o600)

    if "infrast-schedule.json" not in files and (
        crontab_bytes is None or INFRAST_MANAGED_BEGIN not in crontab_bytes
    ):
        for stale_rel in (
            "infrast-schedule.json",
            "backups/infrast-schedule/latest-activation.json",
        ):
            if not _has_symlink_component(root, stale_rel):
                stale_path = root / stale_rel
                if stale_path.is_file():
                    try:
                        stale_path.unlink()
                    except OSError:
                        pass

    crontab_restored = False
    if crontab_bytes is not None and install_crontab_fn is not None:
        install_crontab_fn(crontab_bytes)
        crontab_restored = True

    return {
        "restored_count": len(staged),
        "restored_files": [str(dest.relative_to(root)).replace("\\", "/") for dest, _, _ in staged],
        "crontab_restored": crontab_restored,
    }


# ---------------------------------------------------------------------------
# CLI Entrypoints
# ---------------------------------------------------------------------------


def _emit_shell_env(config_dir: str | Path) -> int:
    settings = load_runtime_settings(config_dir)
    client_type, package_name, server_code = resolve_active_client(config_dir)
    mute = "1" if settings["device"].get("mute_during_run") else "0"
    sleep_mode = settings["device"].get("sleep_after_run", "scheduled")
    proxy_url = settings["network"].get("http_proxy", "")
    mirror_url = settings["network"].get("github_mirror", "")
    pairs = (
        ("MAA_RT_CLIENT_TYPE", client_type),
        ("MAA_RT_AK_PACKAGE", package_name),
        ("MAA_RT_SERVER", server_code),
        ("MAA_RT_MUTE_DURING_RUN", mute),
        ("MAA_RT_SLEEP_AFTER_RUN", sleep_mode),
        ("MAA_RT_HTTP_PROXY", proxy_url),
        ("MAA_RT_GITHUB_MIRROR", mirror_url),
    )
    for key, val in pairs:
        print(f"{key}={shlex.quote(str(val))}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    consume = subparsers.add_parser("consume")
    consume.add_argument("--config-dir", required=True)
    consume.add_argument("--parent-pid", type=int, required=True)
    consume.add_argument("--proc-root", default="/proc")

    is_cron_cmd = subparsers.add_parser("is-cron")
    is_cron_cmd.add_argument("--parent-pid", type=int, required=True)
    is_cron_cmd.add_argument("--proc-root", default="/proc")

    env_cmd = subparsers.add_parser("env")
    env_cmd.add_argument("--config-dir", required=True)

    notify_cmd = subparsers.add_parser("notify")
    notify_cmd.add_argument("--config-dir", required=True)
    notify_cmd.add_argument("--script-dir", default="")
    notify_cmd.add_argument("--title", default="[MaaZero] 运行通知")
    notify_cmd.add_argument("--message", default="")

    args = parser.parse_args(argv)
    if args.command == "consume":
        cron = is_cron_invocation(args.parent_pid, args.proc_root)
        if consume_skip_next_daily(args.config_dir, is_cron=cron):
            print("SKIP_NEXT_DAILY_CONSUMED")
            return 0
        return 1
    if args.command == "is-cron":
        return 0 if is_cron_invocation(args.parent_pid, args.proc_root) else 1
    if args.command == "env":
        return _emit_shell_env(args.config_dir)
    if args.command == "notify":
        message = args.message or sys.stdin.read()
        if not message.strip():
            return 0
        res = send_notifications(
            message,
            title=args.title,
            config_dir=args.config_dir,
            script_dir=args.script_dir or None,
        )
        print(
            f"NOTIFY attempted={res['attempted']} succeeded={res['succeeded']}",
            file=sys.stderr,
        )
        return 0 if (res["attempted"] == 0 or res["any_ok"]) else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
