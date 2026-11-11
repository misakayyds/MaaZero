import json
import os
import sys

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

def main():
    mode = os.environ.get("MAA_SCREENCAP_MODE", "raw_with_gzip").strip().lower()
    if mode not in MODES:
        print(f"Unknown MAA_SCREENCAP_MODE={mode!r}; expected one of {', '.join(sorted(MODES))}")
        sys.exit(2)

    path = '/root/.local/share/maa/resource/config.json'
    print("Loading config from", path)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print("Failed to load:", e)
        return

    found = False
    for conn in data.get('connection', []):
        if conn.get('configName') == 'CompatPOSIXShell':
            # Add trailing space so it matches `find(' ')` logic in AdbController.cpp
            conn['ncAddress'] = "echo '127.0.0.1 '"
            conn.update(MODES[mode])
            found = True
            print(f"Patched CompatPOSIXShell config successfully: mode={mode}.")
            break
            
    if not found:
        print("CompatPOSIXShell not found in connection list.")
        return

    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        print("Config written back successfully!")
    except Exception as e:
        print("Failed to write back:", e)

if __name__ == "__main__":
    main()
