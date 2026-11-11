import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

RAW_BY_NC = '[Adb] -s [AdbSerial] exec-out "screencap 2>/dev/null | nc -w 3 [NcAddress] [NcPort]"'
RAW_WITH_GZIP = '[Adb] -s [AdbSerial] exec-out "screencap 2>/dev/null | gzip -1"'
ENCODE = '[Adb] -s [AdbSerial] exec-out "screencap -p 2>/dev/null"'

MODES = {
    # Used after resource updates and as the safe fallback.
    "raw_with_gzip": {
        "screencapRawByNC": "",
        "screencapRawWithGzip": RAW_WITH_GZIP,
        "screencapEncode": ENCODE,
    },
    # Used only after the per-run benchmark proves that the socket path is
    # fast and raw-size stable for the current display state.
    "raw_by_nc": {
        "screencapRawByNC": RAW_BY_NC,
        "screencapRawWithGzip": "",
        "screencapEncode": ENCODE,
    },
    "encode": {
        "screencapRawByNC": "",
        "screencapRawWithGzip": "",
        "screencapEncode": ENCODE,
    },
}

def get_default_config_path() -> Path:
    res_dir = os.environ.get("MAA_RESOURCE_DIR")
    if res_dir:
        return Path(res_dir) / "config.json"
    data_dir = os.environ.get("MAA_DATA_DIR")
    if data_dir:
        return Path(data_dir) / "resource" / "config.json"
    return Path("/root/.local/share/maa/resource/config.json")

def write_atomic(target: Path, data: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, target)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise

def main():
    parser = argparse.ArgumentParser(description="Patch CompatPOSIXShell into MaaCore config.json")
    parser.add_argument("--source", "-s", type=Path, default=None, help="Input config.json path")
    parser.add_argument("--out", "-o", type=Path, default=None, help="Output config.json path")
    parser.add_argument("--mode", "-m", choices=sorted(MODES), default=None, help="Screencap mode")
    parser.add_argument("--in-place", action="store_true", help="Force overwrite source file")
    parser.add_argument("--dry-run", action="store_true", help="Do not write output to disk")
    args = parser.parse_args()

    mode = args.mode or os.environ.get("MAA_SCREENCAP_MODE", "raw_with_gzip").strip().lower()
    if mode not in MODES:
        print(f"Unknown MAA_SCREENCAP_MODE={mode!r}; expected one of {', '.join(sorted(MODES))}", file=sys.stderr)
        sys.exit(2)

    src_path = args.source or get_default_config_path()
    print("Loading config from", src_path)
    if not src_path.is_file():
        print(f"Config file not found: {src_path}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(src_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"Failed to load: {e}", file=sys.stderr)
        sys.exit(1)

    found = False
    for conn in data.get("connection", []):
        if conn.get("configName") == "CompatPOSIXShell":
            # Add trailing space so it matches `find(' ')` logic in AdbController.cpp
            conn["ncAddress"] = "echo '127.0.0.1 '"
            conn.update(MODES[mode])
            found = True
            print(f"Patched CompatPOSIXShell config successfully: mode={mode}.")
            break

    if not found:
        print("CompatPOSIXShell not found in connection list.", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print(f"Dry run successful: patched {src_path} (mode={mode})")
        return

    dest_path = args.out
    if not dest_path:
        if args.in_place or os.access(src_path, os.W_OK):
            dest_path = src_path
        else:
            state_dir = Path(os.environ.get("MAA_STATE_DIR", "/root/.local/state/maa"))
            dest_path = state_dir / "resource" / "config.json"
            print(f"Source {src_path} is read-only; writing working copy to {dest_path}")

    try:
        write_atomic(dest_path, data)
        print(f"Config successfully written to {dest_path} (mode={mode})")
    except Exception as e:
        print(f"Failed to write: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
