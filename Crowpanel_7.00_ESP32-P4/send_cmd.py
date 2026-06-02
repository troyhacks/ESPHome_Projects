"""
send_cmd.py - Serial command interface for the ha_autopanel test harness.

The ha_autopanel component reads single ASCII chars from UART0 (the same
UART the logger uses for log output) and dispatches them as commands.
This script sends one command and reads back the device's response.

Usage:
  python send_cmd.py <port> <command> [--wait <seconds>]

Examples:
  python send_cmd.py COM47 r         # re-probe authorization
  python send_cmd.py COM47 s         # force SETUP_REQUIRED state
  python send_cmd.py COM47 a         # force AUTH_FAILED state
  python send_cmd.py COM47 n         # force NOT_AUTHORIZED state
  python send_cmd.py COM47 c         # force CONNECTING state
  python send_cmd.py COM47 g         # force READY (re-render room grid)
  python send_cmd.py COM47 d         # re-run full discovery
  python send_cmd.py COM47 0         # open detail view for room 0
  python send_cmd.py COM47 n --wait 8  # wait 8s for response

The component's full command set:
  p, r     Re-probe authorization (same as Retry button)
  s        Set state to SETUP_REQUIRED
  a        Set state to AUTH_FAILED
  n        Set state to NOT_AUTHORIZED
  c        Set state to CONNECTING
  g        Set state to READY (re-render the room grid)
  d        Re-run full discovery
  0..9     Open detail view for room N (0 = first card)

This lets the host drive every state of the panel without a physical
touch - especially useful for verifying the SETUP_REQUIRED /
AUTH_FAILED / NOT_AUTHORIZED / CONNECTING screens when the
device's actual HA state wouldn't otherwise produce them.

For web form testing, the script can also hit the device's HTTP
endpoint directly:
  python send_cmd.py <port> web
"""
import sys
import time
import serial


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = args[0]
    # Special: the "web" subcommand hits the device's HTTP API directly.
    if cmd == "web":
        web_test(args[1:])
        return
    # Otherwise the first arg is the serial port and the second is the
    # single-char command to send.
    port = cmd
    cmd = args[1]
    wait_s = 5.0
    if "--wait" in args:
        idx = args.index("--wait")
        wait_s = float(args[idx + 1])

    # The Crowpanel connects to a CH340K at 115200 baud by default.
    print(f"[send_cmd] opening {port} at 115200, will read for {wait_s}s after send")
    s = serial.Serial(port, 115200, timeout=1)
    s.reset_input_buffer()
    # Wait for the device to be ready (boot logs to settle)
    time.sleep(2)
    # Drain any boot log noise so we only print lines from the cmd onward
    drained = 0
    while s.in_waiting:
        line = s.readline()
        drained += 1
        if drained <= 3:
            try:
                print(f"[recv-pre] {line.decode(errors='replace').rstrip()[:200]}")
            except Exception:
                pass
    print(f"[send_cmd] drained {drained} lines, sending: {cmd!r}")
    s.write(f"{cmd}\n".encode())
    s.flush()
    # Read response for wait_s seconds
    deadline = time.time() + wait_s
    lines = 0
    while time.time() < deadline:
        if s.in_waiting:
            line = s.readline()
            try:
                text = line.decode(errors='replace').rstrip()
            except Exception:
                text = repr(line)
            if text:
                # ANSI colour codes from ESPHome's logger. Strip them for
                # readability and so the output is grep-friendly.
                clean = text.replace("\x1b[0;32m", "").replace("\x1b[0;33m", "").replace("\x1b[0m", "")
                try:
                    print(f"[recv] {clean[:600]}")
                except UnicodeEncodeError:
                    print(f"[recv] {clean[:600].encode('ascii', 'replace').decode('ascii')}")
                lines += 1
        else:
            time.sleep(0.05)
    print(f"[send_cmd] received {lines} new lines, done")
    s.close()


def web_test(args):
    """Hit the device's HTTP endpoint for the /autopanel web form.

    Usage:
      python send_cmd.py web get <ip>             # fetch the setup form
      python send_cmd.py web save <ip> <url> <token>  # POST a new config
      python send_cmd.py web find                 # scan the local subnet
    """
    import urllib.request
    import urllib.parse
    import urllib.error

    if not args or args[0] == "find":
        # Scan the local /24 for /autopanel
        # Pick the same /24 as the host
        import socket
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)
        # local_ip is something like 192.168.2.67; use the /24
        base = ".".join(local_ip.split(".")[:3])
        print(f"[web] scanning {base}.0/24 for /autopanel...")
        for last in range(1, 255):
            ip = f"{base}.{last}"
            try:
                req = urllib.request.urlopen(f"http://{ip}/autopanel", timeout=1)
                if req.status == 200 and "ha_autopanel" in req.read().decode("utf-8", errors="replace"):
                    print(f"[web] HIT: {ip}")
            except (urllib.error.URLError, socket.timeout, ConnectionRefusedError):
                pass
        return

    if args[0] == "get" and len(args) >= 2:
        ip = args[1]
        url = f"http://{ip}/autopanel"
        print(f"[web] GET {url}")
        try:
            req = urllib.request.urlopen(url, timeout=5)
            print(f"[web] status: {req.status}")
            body = req.read().decode("utf-8", errors="replace")
            print(body)
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
            body = req.read().decode("utf-8", errors="replace")
            print(body)
        except urllib.error.URLError as e:
            print(f"[web] failed: {e}")
        return

    print(f"[web] unknown subcommand: {args}")
    print("  use 'get <ip>', 'save <ip> <url> <token>', or 'find'")


if __name__ == "__main__":
    main()
