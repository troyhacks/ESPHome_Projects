"""bft - Build, Flash, Test for the Crowpanel ha_autopanel.

Streamlines the full cycle:
  1. Kill any lingering esphome / send_cmd / pyserial processes that
     still hold the serial port.
  2. Build the firmware (esphome compile). Pure build, no port use.
  3. Flash (esphome upload --device COM47). Briefly uses the port for
     esptool, then releases it. The device reboots.
  4. Wait for the device's web API to come up.
  5. Open a read-only pyserial log session in a background thread.
     This is the active log monitor: it streams every line to a
     timestamped log file AND watches for crash markers. If it sees
     "Guru Meditation", "*** CRASH DETECTED", "Stack protection",
     "Instruction address misaligned", "Brownout", or a panic backtrace
     it sets a thread-safe crash flag and continues logging.
  6. Run the automated test suite. Tests drive the panel via the
     /autopanel/test/* web API endpoints (gated by `agent_debug: true`
     in the test yaml) so the serial port is free for the log session.
     The test runner polls the crash flag between steps and aborts
     the run if a crash is detected.
  7. Stop the log session cleanly.
  8. Print a pass/fail/crash summary. Exit 0 on pass, 1 on test
     failure or crash.

Usage:
  python testing/bft.py             # full cycle
  python testing/bft.py --skip-build # use existing build
  python testing/bft.py --no-test    # build + flash + log only

Designed to be runnable from a Claude Bash tool, a human terminal,
or a CI script - no interactive prompts.
"""
import argparse
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
ESP32_IP = "192.168.2.74"
DEVICE_PORT = "COM47"
YAML = "test_dynamic_component.yaml"
HARNESS = HERE / "send_cmd.py"
TESTS = HERE / "run_tests.py"
LOGS = HERE / "logs"
LOGS.mkdir(exist_ok=True)

# Patterns that indicate a firmware crash. Matched against every line
# of the active log session. The list is intentionally narrow - we
# only want to flag actual device faults, not application logs that
# happen to contain the word "crash" or "error" in normal use.
# (For example, the panel's auth-probe logs "error=An action which
# does not return responses..." on every boot; that's not a crash.)
CRASH_PATTERNS = [
    re.compile(r"Guru Meditation", re.IGNORECASE),
    re.compile(r"\*\*\*\s*CRASH\s*DETECTED", re.IGNORECASE),
    re.compile(r"Stack protection", re.IGNORECASE),
    re.compile(r"Instruction address misaligned", re.IGNORECASE),
    re.compile(r"abort\(\) was called", re.IGNORECASE),
    re.compile(r"esp_backtrace_print", re.IGNORECASE),
    re.compile(r"Brownout", re.IGNORECASE),
    re.compile(r"panic'ed", re.IGNORECASE),
]

# How aggressively the log session tries to recover from a dropped
# serial port. The S3 in particular closes its USB-SERIAL-JTAG CDC
# endpoint when the MCU resets, which looks like a SerialException
# to pyserial. P4's UART is more forgiving but the reconnect path
# is the same. 20 attempts * 1s = 20s window covers a normal
# ESP32 reboot (3-5s) with margin for bootloader re-enumeration.
RECONNECT_ATTEMPTS = 20
RECONNECT_DELAY_S = 1.0


def log(stage, msg):
    """Single-line, timestamped progress log."""
    print(f"  [{time.strftime('%H:%M:%S')}] {stage}: {msg}", flush=True)


def kill_lingering_esphome():
    """Kill any python processes still holding the serial port or
    watching the serial log. Without this, the next esphome run
    fails with PermissionError(13, 'Access is denied.') on the
    serial port.
    """
    try:
        import psutil  # type: ignore
    except ImportError:
        # psutil may not be installed. Fall back to a less-precise
        # approach via subprocess.
        result = subprocess.run(
            ['powershell.exe', '-Command',
             "Get-Process python -ErrorAction SilentlyContinue | "
             "Where-Object { $_.CommandLine -like '*esphome*' -or "
             "$_.CommandLine -like '*send_cmd*' } | Stop-Process -Force"],
            capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log('cleanup', f'psutil missing, powershell fallback failed: {result.stderr[:200]}')
        return

    killed = 0
    for p in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if p.info['name'] and p.info['name'].lower() in ('python.exe', 'python3.exe'):
                cmd = ' '.join(p.info['cmdline'] or [])
                if 'esphome' in cmd or 'send_cmd' in cmd or 'bft' in cmd:
                    p.kill()
                    killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if killed:
        log('cleanup', f'killed {killed} lingering esphome/send_cmd process(es)')
    else:
        log('cleanup', 'no lingering processes')


def build_firmware():
    """Run esphome compile. Returns (ok, log_path)."""
    log('build', f'compiling {YAML}...')
    log_path = LOGS / f"build_{time.strftime('%Y%m%d-%H%M%S')}.log"
    log_path.parent.mkdir(exist_ok=True)
    proc = subprocess.run(
        ['python', '-m', 'esphome', 'compile', YAML],
        cwd=str(PROJECT_ROOT),
        capture_output=True, text=True
    )
    log_path.write_text(proc.stdout + proc.stderr, encoding='utf-8')
    if proc.returncode != 0:
        log('build', f'FAILED (rc={proc.returncode}). See {log_path}')
        return False, log_path
    bin_path = (PROJECT_ROOT / ".esphome" / "build" /
                "test-dynamic-discovery" / ".pioenvs" /
                "test-dynamic-discovery" / "firmware.factory.bin")
    if bin_path.exists():
        size_kb = bin_path.stat().st_size / 1024
        log('build', f'OK ({size_kb:.0f} KB). Log: {log_path}')
    else:
        log('build', f'OK. Log: {log_path}')
    return True, log_path


def upload_firmware():
    """Run esphome upload. esptool briefly takes the port, then
    releases it after flashing. Returns (ok, log_path)."""
    log('upload', f'esphome upload {YAML} --device {DEVICE_PORT}')
    log_path = LOGS / f"upload_{time.strftime('%Y%m%d-%H%M%S')}.log"
    log_path.parent.mkdir(exist_ok=True)
    proc = subprocess.run(
        ['python', '-m', 'esphome', 'upload', YAML, '--device', DEVICE_PORT],
        cwd=str(PROJECT_ROOT),
        capture_output=True, text=True
    )
    log_path.write_text(proc.stdout + proc.stderr, encoding='utf-8')
    if proc.returncode != 0:
        log('upload', f'FAILED (rc={proc.returncode}). See {log_path}')
        return False, log_path
    log('upload', f'OK. Log: {log_path}')
    return True, log_path


def wait_for_web_api(ip, timeout=90):
    """Poll the device's web API until it's up. Returns True on success."""
    log('boot', f'waiting for web API at {ip}...')
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = subprocess.run(
                ['curl', '-s', '-m', '2', f'http://{ip}/'],
                capture_output=True, text=True, timeout=5
            )
            if r.returncode == 0 and '<html' in r.stdout.lower():
                log('boot', f'web API up at {ip}')
                # Give LVGL a couple more seconds to render the first
                # paint so the next screenshot isn't a white frame.
                time.sleep(2)
                return True
        except Exception:
            pass
        time.sleep(2)
    log('boot', f'web API did NOT come up within {timeout}s')
    return False


class LogSession:
    """Background read-only pyserial log session.

    Opens the serial port for reading only, then runs a daemon thread
    that decodes each line, writes it to a timestamped log file, and
    greps for crash markers. Crash detection is exposed via the
    `crashed` property (thread-safe) and the `crash_lines` list which
    captures the matching lines for the final report.
    """

    def __init__(self, port, baud=115200, log_path=None):
        import serial  # local import so the rest of bft works without it
        self.port = port
        self.baud = baud
        self.log_path = Path(log_path) if log_path else (
            LOGS / f"device_{time.strftime('%Y%m%d-%H%M%S')}.log")
        self.log_path.parent.mkdir(exist_ok=True)
        self._stop = threading.Event()
        self._crashed = threading.Event()
        self._crash_lock = threading.Lock()
        self._crash_lines = []
        self._thread = None
        self._serial = None
        self._serial_mod = serial  # keep ref so we can close later

    @property
    def crashed(self):
        return self._crashed.is_set()

    @property
    def crash_lines(self):
        with self._crash_lock:
            return list(self._crash_lines)

    def start(self):
        log('log', f'opening {self.port} @ {self.baud} for read-only log capture')
        self._serial = self._serial_mod.Serial(
            self.port, self.baud, timeout=0.5)
        # Discard any boot noise already in the buffer (e.g. from the
        # upload step just before this). The first read returns whatever
        # is buffered, and on P4 that can be the esptool status lines.
        # Wait briefly for the device to actually reboot and produce
        # its own banner, then start capturing.
        time.sleep(1.0)
        try:
            self._serial.reset_input_buffer()
        except Exception:
            pass
        self._thread = threading.Thread(
            target=self._run, name="log-session", daemon=True)
        self._thread.start()
        log('log', f'streaming to {self.log_path}')

    def _run(self):
        # Daemon loop. Each iteration: read one line, decode, check
        # for crash markers, append to log file. pyserial's timeout=0.5
        # makes readline() return b'' on idle, which is the natural
        # place to check self._stop.
        #
        # AUTO-RECONNECT: on S3 especially, a crash can close the
        # serial port (the USB-SERIAL-JTAG peripheral drops the CDC
        # endpoint when the MCU resets). pyserial.readline() then
        # raises SerialException or returns garbage. We try to
        # reopen the port in a tight loop and resume capture. This
        # means the log file is contiguous across crashes and
        # reboots - the test harness sees the full lifecycle.
        f = self.log_path.open('w', encoding='utf-8', newline='')
        try:
            while not self._stop.is_set():
                try:
                    raw = self._serial.readline()
                except Exception as e:
                    # Port closed or device disconnected. Try to
                    # reopen up to RECONNECT_ATTEMPTS times with
                    # RECONNECT_DELAY_S between tries. If the device
                    # is rebooting (which can take 3-5s on P4) the
                    # first few attempts will fail.
                    f.write(f"[bft] log session read error: {e} - attempting reconnect\n")
                    f.flush()
                    self._serial = None
                    for attempt in range(RECONNECT_ATTEMPTS):
                        if self._stop.is_set():
                            break
                        time.sleep(RECONNECT_DELAY_S)
                        try:
                            self._serial = self._serial_mod.Serial(
                                self.port, self.baud, timeout=0.5)
                            f.write(f"[bft] reconnected after {attempt+1} attempt(s)\n")
                            f.flush()
                            log('log', f'reconnected to {self.port} after crash/reset')
                            break
                        except Exception as re_e:
                            f.write(f"[bft] reconnect attempt {attempt+1} failed: {re_e}\n")
                            f.flush()
                    else:
                        f.write(f"[bft] all {RECONNECT_ATTEMPTS} reconnect attempts failed; giving up\n")
                        f.flush()
                        break
                    continue
                if not raw:
                    continue
                # ESP-IDF / ESPHome use ASCII for the level tag and a
                # millisecond timestamp; both decode as UTF-8.
                try:
                    line = raw.decode('utf-8', errors='replace').rstrip('\r\n')
                except Exception:
                    line = repr(raw)
                # Always prepend a wall-clock timestamp for the
                # host-side log so the agent can correlate against
                # local test events.
                ts = datetime.now().strftime('%H:%M:%S.%f')[:-3]
                f.write(f"{ts} {line}\n")
                f.flush()
                # Crash detection. The pattern match is per-line so
                # we don't trigger on incidental mentions in stack
                # backtraces that name a different module.
                for pat in CRASH_PATTERNS:
                    if pat.search(line):
                        with self._crash_lock:
                            self._crash_lines.append(line)
                        # Latch the event the first time. The line is
                        # already in crash_lines; no need to keep
                        # appending duplicate lines for the same crash
                        # loop.
                        if not self._crashed.is_set():
                            log('log', f'CRASH DETECTED: {line[:200]}')
                        self._crashed.set()
                        break
        finally:
            try:
                f.close()
            except Exception:
                pass

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
        log('log', f'closed {self.port}, log saved to {self.log_path}')


def run_tests(log_session):
    """Run the test suite. Aborts early if the log session flags a
    crash. Returns (ok: bool, summary: str)."""
    log('test', f'running {TESTS.name} --quick')
    test_log = LOGS / f"test_{time.strftime('%Y%m%d-%H%M%S')}.log"
    # v1.17: flip the AUTO-TEST banner on so the user can see the
    # test harness is driving the panel. Banners turn on before
    # the test process starts and turn off after it exits, so the
    # user gets visual confirmation that bft.py is in control.
    # If the device's HTTP server is in a bad state (the
    # 1.9s-LVGL / 1.x-httpd-hang bug), the curl here will
    # timeout silently - the banner won't show, but the tests
    # still run. The test_banner_active flag is in-memory so it
    # also resets to false on reboot.
    subprocess.run(
        ['curl', '-sS', '-m', '5', '-o', '/dev/null',
         f'http://{ESP32_IP}/autopanel/test/banner?on=1'],
        capture_output=True, text=True, timeout=8
    )
    try:
        proc = subprocess.run(
            ['python', str(TESTS), '--quick'],
            cwd=str(HERE),
            capture_output=True, text=True
        )
        test_log.write_text(proc.stdout + proc.stderr, encoding='utf-8')
        # If a crash happened during the run, surface it as a failure
        # even if the test process itself exited cleanly.
        if log_session.crashed:
            crash_lines = log_session.crash_lines
            return False, f"CRASH during test run: {crash_lines[0][:200]}"
        log('test', f'exit code {proc.returncode}, log: {test_log}')
        if proc.stdout:
            for line in proc.stdout.strip().splitlines()[-20:]:
                print(f"    {line}")
        return proc.returncode == 0, 'ok' if proc.returncode == 0 else 'test failures'
    finally:
        # Always turn the banner back off, even on exception, so
        # a crashed test process doesn't leave the user with a
        # permanent "AUTO-TEST" pill on their panel.
        subprocess.run(
            ['curl', '-sS', '-m', '5', '-o', '/dev/null',
             f'http://{ESP32_IP}/autopanel/test/banner?on=0'],
            capture_output=True, text=True, timeout=8
        )


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('--skip-build', action='store_true',
                   help='Use the existing .esphome build (skip esphome compile).')
    p.add_argument('--no-test', action='store_true',
                   help='Build + flash + log only; do not run the test suite.')
    p.add_argument('--skip-upload', action='store_true',
                   help='Skip flashing (assumes device is already running the latest).')
    p.add_argument('--ip', default=ESP32_IP,
                   help=f'Device IP (default: {ESP32_IP})')
    args = p.parse_args()

    print(f"=== bft: Crowpanel build-flash-test ===")
    print(f"    project: {PROJECT_ROOT}")
    print(f"    device:  {args.ip}:80 (port {DEVICE_PORT})")
    print()

    # Step 1: clean slate
    log('start', 'killing any lingering esphome / send_cmd processes')
    kill_lingering_esphome()
    print()

    # Step 2: build
    if not args.skip_build:
        build_ok, build_log = build_firmware()
        print()
        if not build_ok:
            log('FAIL', 'build failed - aborting')
            return 1
    else:
        log('build', 'skipped (--skip-build)')
    print()

    # Step 3: upload
    if not args.skip_upload:
        upload_ok, upload_log = upload_firmware()
        print()
        if not upload_ok:
            log('FAIL', 'upload failed - aborting')
            return 1
    else:
        log('upload', 'skipped (--skip-upload)')
    print()

    # Step 4: wait for boot
    if not wait_for_web_api(args.ip, timeout=90):
        log('FAIL', 'device did not become reachable after upload - aborting')
        return 1
    print()

    # Step 5: start the active log session. This is the heart of the
    # new flow: pyserial reads from the port in a background thread
    # while the test runner uses the web API. The session is
    # read-only, so it does not conflict with the web server.
    log_session = LogSession(DEVICE_PORT, baud=115200)
    log_session.start()
    print()

    try:
        # Step 6: run tests (if requested). The test runner is
        # web-API only now, so it does not need the serial port.
        if not args.no_test:
            tests_ok, test_summary = run_tests(log_session)
            print()
        else:
            tests_ok, test_summary = True, 'skipped (--no-test)'

        # Step 7: report
        print()
        if log_session.crashed:
            log('FAIL', f'CRASH detected during run:')
            for line in log_session.crash_lines[:5]:
                log('FAIL', f'  {line[:200]}')
            log('FAIL', f'full device log: {log_session.log_path}')
            return 1
        if not tests_ok:
            log('FAIL', f'test suite failed: {test_summary}')
            return 1
        log('done', f'all green. log: {log_session.log_path}')
        return 0
    finally:
        # Step 8: always close the log session, even on failure
        log_session.stop()


if __name__ == "__main__":
    sys.exit(main())
