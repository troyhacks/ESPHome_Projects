"""
send_cmd.py - Serial + web test harness for the ha_autopanel Crowpanel.

The C++ component at components/ha_autopanel/ha_autopanel.cpp (loop(),
~line 2774) reads single ASCII chars from UART0 and dispatches them as
commands. This script is the host-side companion: it tails the device's
serial log, sends commands, and verifies the web endpoints. Everything
the harness needs lives in this one file - no external test framework.

Subcommands:

  python send_cmd.py <port> <cmd> [--wait N]
      Send a single-char command to UART0 and read back N seconds of log.
      Examples:
        python send_cmd.py COM47 r         # re-probe authorization
        python send_cmd.py COM47 s         # force SETUP_REQUIRED state
        python send_cmd.py COM47 a         # force AUTH_FAILED state
        python send_cmd.py COM47 n         # force NOT_AUTHORIZED state
        python send_cmd.py COM47 c         # force CONNECTING state
        python send_cmd.py COM47 g         # force READY (re-render grid)
        python send_cmd.py COM47 d         # re-run full discovery
        python send_cmd.py COM47 0         # open detail view for room 0
        python send_cmd.py COM47 n --wait 8  # wait 8s for response

  python send_cmd.py web <verb> [args...]
      Hit the device's HTTP API directly.
        web get <ip>              # fetch the setup form HTML
        web save <ip> <url> <tok> # POST a new config
        web find                  # scan the local /24 for /autopanel
        web status <ip>           # GET /, /autopanel, /autopanel/customizations

  python send_cmd.py discover [--port COM47] [--timeout 30]
      Tail the serial log until the device publishes its IP. The C++
      side logs it with the [ip] tag whenever the title bar's HA
      label refreshes (only when the IP changes - not spammy).
      Falls back to web/find on the local /24 if no IP appears in
      the log within the timeout.
      Prints the discovered IP and exits 0 on success, 1 on failure.

  python send_cmd.py verify [--port COM47] [--ip X.X.X.X] [--timeout 30]
      End-to-end health check. Sequence:
        1. Discover the device IP (from logs, then mDNS, then /24 scan)
        2. GET / and assert 200 + has "ha_autopanel" marker
        3. GET /autopanel and assert 200 + contains the setup form
        4. GET /autopanel/customizations and assert 200 + valid JSON
      Prints a one-line per check summary with PASS/FAIL.
      Exits 0 if all checks pass, 1 otherwise.

  python send_cmd.py monitor [--port COM47] [--ip X.X.X.X] [--interval 30]
      Continuous mode. Tails the serial log (with ANSI stripped and
      timestamps) in the foreground, and in a background thread
      hits the web endpoints every --interval seconds. Each periodic
      check prints a single line. Use Ctrl-C to stop.

  python send_cmd.py tail [--port COM47] [--duration N]
      Just tail the serial log. Strips ANSI, decodes UTF-8, and
      keeps running until Ctrl-C or --duration elapses. Useful as
      a live read-only window into what the device is doing.

The C++ side's full command set:
  p, r     Re-probe authorization (same as Retry button)
  s        Set state to SETUP_REQUIRED
  a        Set state to AUTH_FAILED
  n        Set state to NOT_AUTHORIZED
  c        Set state to CONNECTING
  g        Set state to READY (re-render the room grid)
  d        Re-run full discovery
  0..9     Open detail view for room N (0 = first card)
"""
import json
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import serial
except ImportError:
    print("ERROR: pyserial not installed. Run: pip install pyserial", file=sys.stderr)
    sys.exit(2)

# Defaults - change here if your setup differs.
DEFAULT_PORT = "COM47"
DEFAULT_BAUD = 115200
DEVICE_NAME = "test-dynamic-discovery"  # used for the mDNS .local lookup

# ANSI colour escapes ESPHome's logger emits. Stripped for readability.
ANSI_ESCAPES = (
    "\x1b[0;32m",  # green
    "\x1b[0;33m",  # yellow
    "\x1b[0;31m",  # red
    "\x1b[0;36m",  # cyan
    "\x1b[0;35m",  # magenta
    "\x1b[0m",     # reset
)


# ---------------------------------------------------------------------------
# Serial primitives
# ---------------------------------------------------------------------------

def strip_ansi(text):
    """Drop the small fixed set of ANSI escapes ESPHome's logger emits."""
    for esc in ANSI_ESCAPES:
        text = text.replace(esc, "")
    return text


def decode_line(raw):
    """Decode a serial-read bytes line into a clean UTF-8 string."""
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw)
    return strip_ansi(text).rstrip("\r\n")


def open_serial(port, baud=DEFAULT_BAUD):
    """Open the serial port, return the Serial object (caller closes)."""
    s = serial.Serial(port, baud, timeout=1)
    s.reset_input_buffer()
    return s


def drain_boot_noise(s, settle_s=2.0, max_lines=5, echo=False):
    """Sleep settle_s seconds, then drain any boot-log lines so the
    next read starts from a clean state. Echoes up to max_lines so
    the user can see what we drained.
    """
    time.sleep(settle_s)
    drained = 0
    while s.in_waiting:
        line = s.readline()
        drained += 1
        if echo and drained <= max_lines:
            print(f"[boot] {decode_line(line)[:200]}")
    return drained


# ---------------------------------------------------------------------------
# IP discovery
# ---------------------------------------------------------------------------

# Matches the C++-side log: `ESP_LOGI(TAG, "[ip] %s", ip_buf);`
# Tag can be any of the lines we emit: "[ip] X.X.X.X" or
# the legacy setup() log "  IP: X.X.X.X".
IP_REGEX = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")


def discover_ip_from_logs(port, timeout_s=30.0, settle_s=2.0):
    """Open the serial port, tail the log, return the first valid IP
    that appears, or None on timeout. The IP is extracted from a log
    line that matches the `[ip]` tag the C++ side emits in
    update_title_bar_(). If no tagged line appears within the
    timeout, also accept any other plausible IP-looking token in the
    log (e.g. the legacy `  IP:` log from setup() at line 135).
    """
    print(f"[discover] opening {port} at {DEFAULT_BAUD} (timeout {timeout_s}s)")
    s = open_serial(port)
    try:
        time.sleep(settle_s)  # let boot logs settle
        # Drain any boot noise
        while s.in_waiting:
            s.readline()

        deadline = time.time() + timeout_s
        first_ip = None
        while time.time() < deadline:
            if s.in_waiting:
                line = s.readline()
                text = decode_line(line)
                if text:
                    print(f"[log] {text[:300]}")
                if "[ip]" in text:
                    m = IP_REGEX.search(text)
                    if m:
                        ip = m.group(1)
                        # Sanity: skip the obvious noise (0.0.0.0)
                        if not ip.startswith("0."):
                            print(f"[discover] found tagged IP: {ip}")
                            return ip
                # Track any other IP-looking token, in case the tagged
                # log hasn't fired yet (e.g. panel still on the
                # "HA: connecting..." label).
                if first_ip is None:
                    for tok in IP_REGEX.findall(text):
                        if not tok.startswith("0.") and not tok.startswith("255."):
                            first_ip = tok
            else:
                time.sleep(0.05)
        if first_ip:
            print(f"[discover] no [ip] tag, but saw plausible IP in log: {first_ip}")
        return first_ip
    finally:
        s.close()


def discover_ip_via_mdns(name=DEVICE_NAME, timeout_s=2.0):
    """Try to resolve <name>.local via mDNS. Returns the IP or None.
    mDNS resolution is OS-dependent: on Windows it works if Bonjour
    Print Services is installed; on macOS it works out of the box.
    """
    host = f"{name}.local"
    print(f"[discover] trying mDNS: {host}")
    try:
        # Use a short socket.getaddrinfo timeout via threading
        result = [None]
        def resolve():
            try:
                result[0] = socket.gethostbyname(host)
            except Exception:
                pass
        t = threading.Thread(target=resolve, daemon=True)
        t.start()
        t.join(timeout_s)
        if result[0]:
            print(f"[discover] mDNS resolved: {result[0]}")
        return result[0]
    except Exception as e:
        print(f"[discover] mDNS failed: {e}")
        return None


def discover_ip_via_subnet(timeout_per_ip=1.0):
    """Scan the local /24 for /autopanel. Returns the IP or None.
    Same approach as `web find`. Slow (~30s for 254 hosts with the
    default 1s timeout) but reliable as a last resort.
    """
    print("[discover] scanning local /24 for /autopanel...")
    try:
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
        base = ".".join(local_ip.split(".")[:3])
    except Exception as e:
        print(f"[discover] couldn't determine local subnet: {e}")
        return None
    for last in range(1, 255):
        ip = f"{base}.{last}"
        try:
            req = urllib.request.urlopen(f"http://{ip}/autopanel", timeout=timeout_per_ip)
            body = req.read().decode("utf-8", errors="replace")
            if req.status == 200 and "ha_autopanel" in body:
                print(f"[discover] /24 scan HIT: {ip}")
                return ip
        except (urllib.error.URLError, socket.timeout, ConnectionRefusedError):
            pass
        if last % 32 == 0:
            print(f"[discover] scanned {base}.1..{base}.{last} ...")
    return None


def discover_ip(port=None, timeout_s=30.0):
    """Combined: logs -> mDNS -> /24. Returns the IP or None.
    Tries the most informative source first (serial log) and only
    falls back to the slower sources if needed.
    """
    if port:
        ip = discover_ip_from_logs(port, timeout_s=timeout_s)
        if ip:
            return ip
    ip = discover_ip_via_mdns()
    if ip:
        return ip
    return discover_ip_via_subnet()


# ---------------------------------------------------------------------------
# Web checks
# ---------------------------------------------------------------------------

def http_get(url, timeout=5.0):
    """GET a URL, return (status, body) or raise on network error."""
    req = urllib.request.urlopen(url, timeout=timeout)
    body = req.read()
    return req.status, body


def check_root(ip, timeout=5.0):
    """GET http://<ip>/ - the device's root web server status page."""
    url = f"http://{ip}/"
    status, body = http_get(url, timeout)
    text = body.decode("utf-8", errors="replace")
    return {"url": url, "status": status, "ok": status == 200, "len": len(text)}


def check_setup_form(ip, timeout=5.0):
    """GET http://<ip>/autopanel - the device's setup form."""
    url = f"http://{ip}/autopanel"
    status, body = http_get(url, timeout)
    text = body.decode("utf-8", errors="replace")
    has_marker = "ha_autopanel" in text
    has_form = "<form" in text or "api_url" in text
    return {
        "url": url,
        "status": status,
        "ok": status == 200 and has_marker and has_form,
        "len": len(text),
        "has_marker": has_marker,
        "has_form": has_form,
    }


def check_customizations(ip, timeout=5.0):
    """GET http://<ip>/autopanel/customizations - the v2 endpoint.
    Must return 200 + valid JSON with at least the customizations
    fields the C++ side writes (hidden_rooms, hidden_entities,
    room_order, entity_order).
    """
    url = f"http://{ip}/autopanel/customizations"
    status, body = http_get(url, timeout)
    text = body.decode("utf-8", errors="replace")
    parsed = None
    parse_err = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        parse_err = str(e)
    has_keys = (
        parsed is not None
        and isinstance(parsed, dict)
        and "hidden_rooms" in parsed
        and "hidden_entities" in parsed
    )
    return {
        "url": url,
        "status": status,
        "ok": status == 200 and has_keys,
        "len": len(text),
        "json_ok": parsed is not None,
        "has_expected_keys": has_keys,
        "parse_err": parse_err,
        "body_preview": text[:200],
    }


def run_status_checks(ip, port=None):
    """Run all status checks against the given IP (and optionally the
    serial port for the home-name check). Returns a list of
    (name, ok, detail) tuples.
    """
    results = []
    print(f"\n[verify] === status checks against {ip} ===\n")
    checks = [
        ("root           (/)",                      lambda: check_root(ip)),
        ("setup form     (/autopanel)",            lambda: check_setup_form(ip)),
        ("customizations (/autopanel/customizations)", lambda: check_customizations(ip)),
    ]
    if port:
        # Home-name check needs the serial port (we send 'h' and read
        # the [home] log line). If we have the port, add the check.
        checks.append(("home name      (serial 'h' + [home] log)",
                       lambda: check_home_name(port)))
    for name, fn in checks:
        try:
            t0 = time.time()
            r = fn()
            dt_ms = int((time.time() - t0) * 1000)
            tag = "PASS" if r["ok"] else "FAIL"
            print(f"  [{tag}] {name}  {dt_ms}ms")
            for k, v in r.items():
                if k in ("ok",):
                    continue
                print(f"           {k}: {v}")
            results.append((name, r["ok"], r))
        except Exception as e:
            print(f"  [FAIL] {name}  EXCEPTION: {e}")
            results.append((name, False, {"error": str(e)}))
    return results


def check_home_name(port, timeout_s=5.0):
    """Send the 'h' serial command to force a home-name refresh, then
    read the log until we see the [home] tag or the timeout elapses.
    Returns the home name (string) and a pass/fail flag.
    """
    # Pattern: "[home] <friendly_name>"
    home_pat = re.compile(r"\[home\]\s+([^\r\n]+)")
    cmd_pat = re.compile(r"\[cmd\]\s+received:\s+'h'")
    print(f"  [check] opening {port}, sending 'h' for home name refresh")
    s = open_serial(port)
    try:
        drain_boot_noise(s, settle_s=1.0, echo=False)
        s.write(b"h\n")
        s.flush()
        deadline = time.time() + timeout_s
        got_cmd = False
        home_name = None
        while time.time() < deadline:
            if s.in_waiting:
                line = s.readline()
                text = decode_line(line)
                if not text:
                    continue
                # Echo the line under the check so the user can see
                # the device's response in real time.
                print(f"           [log] {text[:200]}")
                if cmd_pat.search(text):
                    got_cmd = True
                # Skip the "[home] fetching ..." line - the C++ side
                # logs both the in-flight announcement ("[home] fetching
                # ...") and the result ("[home] <name>") with the [home]
                # tag. We only want the result.
                if "fetching" in text:
                    continue
                m = home_pat.search(text)
                if m:
                    home_name = m.group(1).strip()
                    break
            else:
                time.sleep(0.05)
        if home_name is None:
            return {
                "ok": False,
                "command_received": got_cmd,
                "home_name": None,
                "reason": "no [home] log line seen within %.1fs" % timeout_s,
            }
        return {
            "ok": True,
            "command_received": got_cmd,
            "home_name": home_name,
        }
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def cmd_send(args):
    """Existing single-char send + readback behaviour. Preserved as-is
    so existing scripts that call it keep working.
    """
    if len(args) < 2:
        print(__doc__)
        sys.exit(1)
    port = args[0]
    cmd = args[1]
    wait_s = 5.0
    if "--wait" in args:
        idx = args.index("--wait")
        wait_s = float(args[idx + 1])

    print(f"[send_cmd] opening {port} at {DEFAULT_BAUD}, will read for {wait_s}s after send")
    s = open_serial(port)
    try:
        drain_boot_noise(s, echo=True)
        print(f"[send_cmd] sending: {cmd!r}")
        s.write(f"{cmd}\n".encode())
        s.flush()
        deadline = time.time() + wait_s
        lines = 0
        while time.time() < deadline:
            if s.in_waiting:
                line = s.readline()
                text = decode_line(line)
                if text:
                    print(f"[recv] {text[:600]}")
                    lines += 1
            else:
                time.sleep(0.05)
        print(f"[send_cmd] received {lines} new lines, done")
    finally:
        s.close()


def cmd_web(args):
    """Existing web subcommand: get/save/find/status."""
    if not args or args[0] == "find":
        ip = discover_ip_via_subnet()
        if ip:
            print(f"[web] /24 scan result: {ip}")
        return

    if args[0] == "get" and len(args) >= 2:
        ip = args[1]
        url = f"http://{ip}/autopanel"
        print(f"[web] GET {url}")
        try:
            status, body = http_get(url)
            print(f"[web] status: {status}")
            print(body.decode("utf-8", errors="replace"))
        except urllib.error.URLError as e:
            print(f"[web] failed: {e}")
        return

    if args[0] == "save" and len(args) >= 4:
        ip, api_url, api_token = args[1], args[2], args[3]
        url = f"http://{ip}/autopanel/save"
        print(f"[web] POST {url} api_url={api_url}")
        data = urllib.parse.urlencode({"api_url": api_url, "api_token": api_token}).encode()
        try:
            req = urllib.request.urlopen(url, data=data, timeout=5)
            print(f"[web] status: {req.status}")
            print(req.read().decode("utf-8", errors="replace"))
        except urllib.error.URLError as e:
            print(f"[web] failed: {e}")
        return

    if args[0] == "status" and len(args) >= 2:
        ip = args[1]
        results = run_status_checks(ip)
        bad = [r for r in results if not r[1]]
        sys.exit(0 if not bad else 1)

    print("  use 'web get <ip>', 'web save <ip> <url> <token>', 'web find', or 'web status <ip>'")


def cmd_discover(args):
    port = DEFAULT_PORT
    timeout_s = 30.0
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--port" and i + 1 < len(args):
            port = args[i + 1]
            i += 2
        elif a == "--timeout" and i + 1 < len(args):
            timeout_s = float(args[i + 1])
            i += 2
        else:
            i += 1
    ip = discover_ip(port=port, timeout_s=timeout_s)
    if ip:
        print(f"[discover] IP: {ip}")
        sys.exit(0)
    else:
        print("[discover] FAILED - no IP found", file=sys.stderr)
        sys.exit(1)


def cmd_verify(args):
    port = DEFAULT_PORT
    ip = None
    timeout_s = 30.0
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--port" and i + 1 < len(args):
            port = args[i + 1]
            i += 2
        elif a == "--ip" and i + 1 < len(args):
            ip = args[i + 1]
            i += 2
        elif a == "--timeout" and i + 1 < len(args):
            timeout_s = float(args[i + 1])
            i += 2
        else:
            i += 1

    if not ip:
        print(f"[verify] discovering IP via {port} / mDNS / /24 scan ...")
        ip = discover_ip(port=port, timeout_s=timeout_s)
        if not ip:
            print("[verify] FAILED - no IP", file=sys.stderr)
            sys.exit(1)
    print(f"[verify] using IP: {ip}")

    results = run_status_checks(ip, port=port)
    print(f"\n[verify] summary:")
    for name, ok, _ in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    bad = [r for r in results if not r[1]]
    if bad:
        print(f"\n[verify] FAILED: {len(bad)} check(s) failed", file=sys.stderr)
        sys.exit(1)
    print(f"\n[verify] OK: all {len(results)} checks passed")
    sys.exit(0)


def cmd_tail(args):
    port = DEFAULT_PORT
    duration_s = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--port" and i + 1 < len(args):
            port = args[i + 1]
            i += 2
        elif a == "--duration" and i + 1 < len(args):
            duration_s = float(args[i + 1])
            i += 2
        else:
            i += 1
    print(f"[tail] opening {port} at {DEFAULT_BAUD} (Ctrl-C to stop)")
    s = open_serial(port)
    try:
        start = time.time()
        try:
            while True:
                if s.in_waiting:
                    line = s.readline()
                    text = decode_line(line)
                    if text:
                        print(text, flush=True)
                else:
                    time.sleep(0.02)
                if duration_s and (time.time() - start) > duration_s:
                    break
        except KeyboardInterrupt:
            print("\n[tail] interrupted")
    finally:
        s.close()


def cmd_monitor(args):
    """Tail logs in foreground, run web checks in a background thread
    every --interval seconds. Prints a one-line result per check.
    """
    port = DEFAULT_PORT
    ip = None
    interval_s = 30.0
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--port" and i + 1 < len(args):
            port = args[i + 1]
            i += 2
        elif a == "--ip" and i + 1 < len(args):
            ip = args[i + 1]
            i += 2
        elif a == "--interval" and i + 1 < len(args):
            interval_s = float(args[i + 1])
            i += 2
        else:
            i += 1

    # Discover IP first (or use override)
    if not ip:
        print(f"[monitor] discovering IP via {port} / mDNS / /24 scan ...")
        ip = discover_ip(port=port, timeout_s=30.0)
        if not ip:
            print("[monitor] FAILED - no IP, exiting", file=sys.stderr)
            sys.exit(1)
    print(f"[monitor] using IP: {ip}, interval {interval_s}s (Ctrl-C to stop)")

    # Background thread: periodic web checks
    stop = threading.Event()
    def checker():
        # Initial check after a short delay
        time.sleep(min(5.0, interval_s))
        while not stop.is_set():
            try:
                t0 = time.time()
                results = run_status_checks(ip)
                bad = sum(1 for _, ok, _ in results if not ok)
                tag = "PASS" if not bad else f"FAIL ({bad})"
                print(f"[monitor] {tag}  {int((time.time()-t0)*1000)}ms  {time.strftime('%H:%M:%S')}")
            except Exception as e:
                print(f"[monitor] check error: {e}")
            stop.wait(interval_s)
    t = threading.Thread(target=checker, daemon=True)
    t.start()

    # Foreground: tail logs
    s = open_serial(port)
    try:
        try:
            while not stop.is_set():
                if s.in_waiting:
                    line = s.readline()
                    text = decode_line(line)
                    if text:
                        ts = time.strftime("%H:%M:%S")
                        print(f"[{ts}] {text}", flush=True)
                else:
                    time.sleep(0.02)
        except KeyboardInterrupt:
            print("\n[monitor] interrupted")
    finally:
        stop.set()
        s.close()


# ---------------------------------------------------------------------------
# Screenshot + simulated input (click / scroll)
# ---------------------------------------------------------------------------

DEFAULT_DEVICE_IP = "192.168.2.74"


def _http_get_bmp(ip, path, timeout=20):
    """GET a BMP file from the device, return the bytes. Used by
    `screenshot` (full screen) and any future per-widget captures.
    """
    import urllib.request
    req = urllib.request.urlopen(f"http://{ip}{path}", timeout=timeout)
    return req.read()


def _bmp_to_png(bmp_bytes, png_path):
    """Convert a 16-bit BI_BITFIELDS BMP (the format the device
    emits) to a PNG the host's image viewer (and the Read tool) can
    handle. Uses Pillow if available.
    """
    import io
    from PIL import Image
    img = Image.open(io.BytesIO(bmp_bytes))
    img.save(png_path, "PNG")
    return png_path


def cmd_screenshot(args):
    """Fetch the current screen from the device's BMP endpoint and
    save it as a PNG (the Read tool can't view raw BMPs, but a
    few-line Pillow conversion gives us a portable file).

    Examples:
      python send_cmd.py screenshot
          # default: 192.168.2.74, save to /tmp/screen.png
      python send_cmd.py screenshot --ip 192.168.2.74 --output ./now.png
      python send_cmd.py screenshot --bmp /tmp/raw.bmp
          # keep the BMP for fast re-encoding
    """
    import urllib.request
    import io
    ip = DEFAULT_DEVICE_IP
    output = "/tmp/screen.png"
    bmp_path = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--ip" and i + 1 < len(args):
            ip = args[i + 1]; i += 2
        elif a == "--output" and i + 1 < len(args):
            output = args[i + 1]; i += 2
        elif a == "--bmp" and i + 1 < len(args):
            bmp_path = args[i + 1]; i += 2
        else:
            i += 1
    t0 = time.time()
    try:
        bmp = _http_get_bmp(ip, "/autopanel/screenshot.bmp")
    except Exception as e:
        print(f"screenshot: HTTP GET failed: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"screenshot: {len(bmp)} bytes BMP in {time.time()-t0:.1f}s")
    if bmp_path:
        with open(bmp_path, "wb") as f:
            f.write(bmp)
        print(f"  wrote BMP to {bmp_path}")
    # Convert to PNG (small enough to Read inline, no cropping needed)
    _bmp_to_png(bmp, output)
    print(f"  wrote PNG to {output}")


def cmd_click(args):
    """Simulate a touch click on the device at (x, y) by sending a
    serial command to the C++ side that drives the LVGL input
    device. This is the harness path for testing GUI flows that
    aren't reachable by the existing state-trigger commands (e.g.
    tapping the Edit button to enter edit mode).

    Implementation: the C++ side has a 'C' serial command that
    takes "x y" on the next line, hit-tests via lv_indev_search_obj
    on the active screen, and dispatches LV_EVENT_CLICKED to the
    topmost object under the point. See HaAutoPanel::simulate_click_
    in the .cpp.

    Recognized flags (any order):
      --port COMx     (default DEFAULT_PORT)
      --wait N        (no-op for click; accepted for symmetry with
                       the legacy send path. The click block already
                       reads for 2s and exits.)
    Positional: <x> <y> at the end.

    Example:
      python send_cmd.py click --port COM47 500 300
    """
    if len(args) < 2:
        print("usage: click --port COMx <x> <y>", file=sys.stderr)
        sys.exit(1)
    # Strip --wait (no-op) and any other future flags so the trailing
    # positional <x> <y> is always args[-2] / args[-1].
    positional = [a for a in args if not a.startswith("--")]
    if len(positional) < 2:
        print("usage: click --port COMx <x> <y>", file=sys.stderr)
        sys.exit(1)
    port = DEFAULT_PORT
    if "--port" in args:
        port = args[args.index("--port") + 1]
    x = int(positional[-2])
    y = int(positional[-1])
    s = open_serial(port)
    try:
        drain_boot_noise(s, settle_s=1.0, echo=False)
        # Two writes: the 'C' command, then a separate "x y" line.
        # Match the C++ side's parser which reads one line at a time
        # via uart_read_bytes().
        s.write(b"C\n")
        s.write(f"{x} {y}\n".encode())
        s.flush()
        # Read response briefly
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if s.in_waiting:
                line = s.readline()
                print(f"[click] {decode_line(line).rstrip()}")
            else:
                time.sleep(0.05)
    finally:
        s.close()


def cmd_scroll(args):
    """Simulate a touch scroll/drag on the device. Sends an 'S'
    command with "x1 y1 x2 y2" on the next line, which the C++
    side hit-tests via lv_indev_search_obj on the active screen
    and applies lv_obj_scroll_by() with the delta on the hit
    object. This is the test-harness equivalent of a drag.

    Example:
      python send_cmd.py scroll --port COM47 500 200 500 500
          # drag from (500, 200) to (500, 500) - vertical scroll down
    """
    if len(args) < 5:
        print("usage: scroll --port COMx <x1> <y1> <x2> <y2>",
              file=sys.stderr)
        sys.exit(1)
    port = args[args.index("--port") + 1] if "--port" in args else DEFAULT_PORT
    coords = [int(a) for a in args[-4:]]
    s = open_serial(port)
    try:
        drain_boot_noise(s, settle_s=1.0, echo=False)
        s.write(b"S\n")
        s.write(f"{coords[0]} {coords[1]} {coords[2]} {coords[3]}\n".encode())
        s.flush()
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if s.in_waiting:
                line = s.readline()
                print(f"[scroll] {decode_line(line).rstrip()}")
            else:
                time.sleep(0.05)
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    sub = args[0]
    rest = args[1:]

    # If the first arg looks like a serial port (COMx or /dev/tty*), and
    # the second is a single char (or starts with --wait), it's the
    # legacy "send a command" path. Keep that working.
    looks_like_port = (re.match(r"^COM\d+$", sub) or sub.startswith("/dev/tty"))
    looks_like_cmd = rest and (len(rest[0]) == 1 or rest[0] in ("--wait",))

    if looks_like_port and looks_like_cmd:
        cmd_send([sub] + rest)
        return

    if sub == "web":
        cmd_web(rest)
    elif sub == "discover":
        cmd_discover(rest)
    elif sub == "verify":
        cmd_verify(rest)
    elif sub == "tail":
        cmd_tail(rest)
    elif sub == "monitor":
        cmd_monitor(rest)
    elif sub == "screenshot":
        cmd_screenshot(rest)
    elif sub == "click":
        cmd_click(rest)
    elif sub == "scroll":
        cmd_scroll(rest)
    elif sub in ("-h", "--help", "help"):
        print(__doc__)
    else:
        print(f"unknown subcommand: {sub!r}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
