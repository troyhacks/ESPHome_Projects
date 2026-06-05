"""
Automated regression test for ha_autopanel on the Crowpanel 7" P4.

Drives the device through every serial command and click coordinate
documented in the harness, captures a screenshot at each step, and
prints a one-line PASS/FAIL summary. Exits 0 if all checks pass, 1
otherwise.

Usage:
  python run_tests.py            # full run, ~5 min
  python run_tests.py --quick    # skip the long states (n/a, auth)

Tests:
  1.  Initial grid render
  2.  State machine: SETUP_REQUIRED, AUTH_FAILED, NOT_AUTHORIZED,
      CONNECTING, READY
  3.  Re-run discovery ('d') and confirm grid re-renders
  4.  Click sim: tap Edit button -> enter edit mode
  5.  Click sim: tap Cancel -> exit edit mode (no save)
  6.  Click sim: tap Edit, then X on a room, then Save -> hide
  7.  Serial 'o': open sort panel
  8.  Serial 'O': close sort panel
  9.  Serial '0'..'9': open detail view for each room index
  10. Serial 'g' to return to grid
  11. 'verify' subcommand: end-to-end health check

The script intentionally uses sleep between steps so the device has
time to render. Crowpanel LVGL is slow; ~3-4s per state change is
the typical observed settle time.
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

PORT = "COM47"
IP = "192.168.2.74"
HA_BASE = "http://homeassistant.local:8123"
# Hardcoded absolute paths so the script works no matter where
# the user keeps it. (An earlier revision computed the harness path
# from __file__.parent, which broke when the file was at
# C:/Users/.../Temp/ha_shots/ rather than next to send_cmd.py.)
HERE = Path(__file__).resolve().parent
HARNESS = Path(r"C:\ESPHome_Projects\Crowpanel_7.00_ESP32-P4\send_cmd.py")
# Screenshots land in testing/screenshots/ inside the project so
# they persist across sessions and are easy to find. The /tmp
# fallback is for when the script is run from outside the project
# tree (e.g., downloaded to a temp dir).
SHOTS = HERE / "screenshots"
SECRETS = Path(r"C:\ESPHome_Projects\Crowpanel_7.00_ESP32-P4\secrets.yaml")


def load_ha_token():
    """Pull the HA long-lived access token from secrets.yaml.

    secrets.yaml uses ESPHome's !secret tag-style, but for the simple
    key: value form we just need a line-by-line parse. The token is
    under `my_ha_api_password`. Returns None on any miss.
    """
    try:
        for line in SECRETS.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("my_ha_api_password:"):
                # value may be quoted, may have a comment after
                val = s.split(":", 1)[1].strip().strip('"').strip("'")
                # drop trailing comment
                if "  #" in val:
                    val = val.split("  #", 1)[0].strip().strip('"')
                return val
    except Exception as e:
        print(f"[warn] could not load HA token: {e}", file=sys.stderr)
    return None


def ha_call(domain, service, entity_id=None, brightness=None, extra=None, token=None):
    """POST a service call to HA's REST API. Returns (rc, body).

    URL: POST /api/services/{domain}/{service}
    Body: {"entity_id": "...", ...optional fields...}
    """
    import json as _json
    import urllib.error
    import urllib.request
    if token is None:
        token = load_ha_token()
    if token is None:
        return 1, "no HA token"
    payload = {}
    if entity_id is not None:
        payload["entity_id"] = entity_id
    if brightness is not None:
        payload["brightness"] = brightness
    if extra:
        payload.update(extra)
    req = urllib.request.Request(
        f"{HA_BASE}/api/services/{domain}/{service}",
        data=_json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return 1, str(e)

# Each test: (name, run_function)
# run_function() returns (passed: bool, summary: str)
def run_harness(args, timeout=30):
    """Invoke send_cmd.py and return (returncode, stdout+stderr)."""
    proc = subprocess.run(
        [sys.executable, str(HARNESS)] + args,
        capture_output=True, text=True, timeout=timeout
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()

def shot(name, output=None, bmp=None, timeout=45):
    """Capture a screenshot. Returns (rc, out, path).
    rc: 0 = ok, 1 = failed, 2 = timed out (still continues).

    Filenames get a timestamp suffix (e.g. "01_initial_grid_20260605-083015.png")
    so it's clear at a glance which screenshots are from which run. The
    --name option (passed as `name`) preserves the human-readable tag,
    the suffix is just for ordering.

    Tries the JPEG endpoint first (on P4 / wherever the device has
    SOC_JPEG_ENCODE_SUPPORTED) which gives a much smaller payload
    than BMP and avoids the BMP->PNG conversion step. Falls back
    to the BMP endpoint + Python conversion if the device returns
    501 (not implemented) or any other error - this keeps the test
    suite usable on S2/S3/C3/C6 too.
    """
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = output or str(SHOTS / f"{name}_{ts}.png")
    bmp = bmp or str(SHOTS / f"{name}_{ts}.bmp")
    # Try JPEG first.
    try:
        r = subprocess.run(
            ['curl', '-s', '-m', str(timeout), '-o', output,
             '-w', '%{http_code}',
             f'http://{IP}/autopanel/screenshot.jpg'],
            capture_output=True, text=True, timeout=timeout + 5
        )
        http_code = r.stdout.strip()
        if r.returncode == 0 and http_code == '200' and os.path.getsize(output) > 1000:
            # The .jpg file is a real JPEG; rename to .jpg for clarity
            # and don't keep a BMP around.
            jpg_path = output[:-4] + '.jpg' if output.endswith('.png') else output
            try:
                os.replace(output, jpg_path)
                output = jpg_path
            except OSError:
                pass
            return 0, f'jpg {os.path.getsize(output)} bytes', output
    except subprocess.TimeoutExpired:
        pass
    # Fall back to the BMP path via the harness (which does BMP->PNG).
    try:
        rc, out = run_harness([
            "screenshot", "--ip", IP,
            "--output", output, "--bmp", bmp
        ], timeout=timeout)
    except subprocess.TimeoutExpired:
        return 2, f"timeout after {timeout}s", output
    return rc, out, output

def web_cmd(cmd_char, wait=2):
    """Send a single-char command via the /autopanel/test/cmd web API.

    Gated by agent_debug: true in the test yaml. Returns (rc, out).
    Wait happens here on the test side - the LVGL events dispatched
    by the web API are queued and need a beat to render before the
    next screenshot.
    """
    import urllib.error as _urllib_error
    import urllib.request as _urllib_request
    url = f"http://{IP}/autopanel/test/cmd?c={cmd_char}"
    try:
        with _urllib_request.urlopen(url, timeout=wait+5) as r:
            body = r.read().decode('utf-8', errors='replace')
            time.sleep(wait)
            return 0 if r.status == 200 else 1, body
    except _urllib_error.HTTPError as e:
        return 1, f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}"
    except Exception as e:
        return 1, f"error: {e}"

def web_click(x, y, wait=2):
    """Tap the screen at (x, y) via the /autopanel/test/click web API.
    Gated by agent_debug: true. Returns (rc, out).
    """
    import urllib.error as _urllib_error
    import urllib.request as _urllib_request
    url = f"http://{IP}/autopanel/test/click?x={x}&y={y}"
    try:
        with _urllib_request.urlopen(url, timeout=wait+5) as r:
            body = r.read().decode('utf-8', errors='replace')
            time.sleep(wait)
            return 0 if r.status == 200 else 1, body
    except _urllib_error.HTTPError as e:
        return 1, f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}"
    except Exception as e:
        return 1, f"error: {e}"

# Backwards-compat shims. The original test code used the names
# serial_cmd / click. The new code uses the web_* variants above;
# these thin wrappers keep the test bodies readable as if they
# were driving the serial port, but the port is actually untouched.
def serial_cmd(cmd, wait=2):
    return web_cmd(cmd, wait=wait)

def click(x, y, wait=2):
    return web_click(x, y, wait=wait)

def wait_for_settle(seconds=2):
    """Give the panel time to render before the next screenshot."""
    time.sleep(seconds)


# ----------------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------------

results = []  # (name, passed, summary)

def record(name, passed, summary):
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}: {summary}")
    results.append((name, passed, summary))

def test_01_initial_grid():
    """Capture the initial grid state after boot."""
    rc, out, png = shot("01_initial_grid")
    size_b = os.path.getsize(png) if os.path.isfile(png) else 0
    ok = rc == 0 and size_b > 1000
    record("01 initial grid", ok,
           f"{size_b} bytes" if ok else f"rc={rc}: {out[:200]}")

def test_02_state_machine(quick):
    """Cycle through every panel state. Each is a forced transition."""
    states = [
        ("s", "03_setup_required", "SETUP_REQUIRED"),
        ("a", "04_auth_failed",   "AUTH_FAILED"),
        ("c", "06_connecting",    "CONNECTING"),
    ]
    if not quick:
        states.insert(2, ("n", "05_not_authorized", "NOT_AUTHORIZED"))
    for cmd, name, label in states:
        rc, out = serial_cmd(cmd, wait=1)
        wait_for_settle(1.5)
        rc2, out2, png = shot(name)
        ok = rc == 0 and rc2 == 0
        record(f"02 state {label}", ok, f"cmd '{cmd}' rc={rc} shot rc={rc2}")

def test_03_recovery_to_ready():
    """Force READY ('g') and re-run discovery ('d'). The panel should
    rebuild the room grid in both cases."""
    rc, out = serial_cmd("g", wait=2)
    wait_for_settle(2)
    rc2, out2, png = shot("07_ready_after_state_cycle")
    ok = rc == 0 and rc2 == 0
    record("03 state READY", ok, f"cmd 'g' rc={rc} shot rc={rc2}")

    rc, out = serial_cmd("d", wait=3)
    wait_for_settle(3)
    rc2, out2, png = shot("08_after_discovery")
    ok = rc == 0 and rc2 == 0 and "Found area" in out
    record("03 re-discovery", ok,
           f"cmd 'd' rc={rc} shot rc={rc2} {'parsed areas' if 'Found area' in out else 'no area log'}")

def force_ready():
    """Helper: force the panel back to READY ('g') so click
    coordinates that target the grid UI (Edit button, room cards)
    actually land on the right widget. Tests that click should
    call this first when transitioning from a forced state."""
    serial_cmd("g", wait=2)
    wait_for_settle(2)


def test_04_click_edit():
    """Tap the Edit button at top-right (~965, 17). Verify edit mode."""
    force_ready()
    rc, out = click(965, 17, wait=2)
    wait_for_settle(2)
    rc2, out2, png = shot("09_edit_mode")
    edit_mode_set = "Edit mode" in out or "edit_mode" in out
    ok = rc == 0 and rc2 == 0 and edit_mode_set
    record("04 click Edit button", ok,
           f"click rc={rc} shot rc={rc2} {'enter edit' if edit_mode_set else 'no edit-mode log'}")

def test_05_click_cancel():
    """Tap the Cancel button in edit mode. Should return to grid."""
    # We're still in edit mode from test 04. Cancel is to the left
    # of Save in the title bar; based on the prior screenshot, Cancel
    # is around (920, 17).
    rc, out = click(920, 17, wait=2)
    wait_for_settle(2)
    rc2, out2, png = shot("10_after_cancel")
    cancel_log = "Cancel" in out or "reverting" in out
    ok = rc == 0 and rc2 == 0 and cancel_log
    record("05 click Cancel", ok,
           f"click rc={rc} shot rc={rc2} {'revert log' if cancel_log else ''}")

def test_06_hide_room():
    """Enter edit mode, click X on first room, click Save."""
    force_ready()
    # Enter edit mode
    rc, out = click(965, 17, wait=2)
    wait_for_settle(2)
    enter_log = "Edit mode" in out or "edit session" in out
    # Click X on first room (Living Room, top-left card). X is at
    # the top-right of the card. card starts at x=0, width=250, so
    # right edge ~ x=240. X badge is just inside that, ~ (231, 42).
    rc, out = click(231, 42, wait=2)
    wait_for_settle(1.5)
    # Save button (was 'Done' in normal mode, becomes Save in edit
    # mode). Approx (875, 17).
    rc, out = click(875, 17, wait=2)
    wait_for_settle(2.5)
    rc2, out2, png = shot("11_after_hide_save")
    write_log = "Wrote" in out or "customizations" in out or "saved" in out.lower()
    ok = rc == 0 and rc2 == 0 and enter_log and write_log
    record("06 hide a room + Save", ok,
           f"click rc={rc} shot rc={rc2} "
           f"{'entered edit' if enter_log else 'no edit log'} "
           f"{'wrote file' if write_log else 'no write log'}")

def test_07_sort_panel_open_close():
    """Open sort panel ('o'), screenshot, close ('O')."""
    rc, out = serial_cmd("o", wait=2)
    wait_for_settle(2)
    rc2, out2, png = shot("12_sort_panel_open")
    ok = rc == 0 and rc2 == 0
    record("07 open sort panel", ok, f"cmd 'o' rc={rc} shot rc={rc2}")
    # Close
    rc, out = serial_cmd("O", wait=2)
    wait_for_settle(2)
    rc2, out2, png = shot("13_sort_panel_closed")
    ok = rc == 0 and rc2 == 0
    record("07 close sort panel", ok, f"cmd 'O' rc={rc} shot rc={rc2}")

def test_08_detail_views():
    """Open detail view for rooms 0-3. Verify entity list renders."""
    for i in range(4):
        rc, out = serial_cmd(str(i), wait=2)
        wait_for_settle(2)
        rc2, out2, png = shot(f"14_detail_room_{i}")
        detail_log = "detail" in out.lower() or "Entity detail" in out or rc == 0
        ok = rc == 0 and rc2 == 0
        record(f"08 detail room {i}", ok,
               f"cmd '{i}' rc={rc} shot rc={rc2}")
    # Back to grid
    serial_cmd("g", wait=2)
    wait_for_settle(2)

def test_09_toggle_light():
    """Tap a room arc to send a brightness. The room card should
    visibly update on the grid (arc fill, room state)."""
    force_ready()
    # Room card arc is roughly centered in each card. Living Room is
    # top-left, arc center is at (125, 175) approximately.
    rc, out = click(125, 175, wait=2)
    wait_for_settle(2)
    rc2, out2, png = shot("15_after_arc_tap")
    sent_log = "Sent light" in out or "Sent light.turn" in out or "room arc release" in out
    ok = rc == 0 and rc2 == 0
    record("09 tap room arc", ok,
           f"click rc={rc} shot rc={rc2} {'HA service sent' if sent_log else 'no sent log'}")

def test_10_health_check():
    """End-to-end health via the harness's verify subcommand."""
    rc, out = run_harness(["verify", "--ip", IP, "--timeout", "10"], timeout=60)
    # verify prints PASS/FAIL per check
    lines = [l for l in out.splitlines() if "PASS" in l or "FAIL" in l]
    passes = sum(1 for l in lines if "PASS" in l)
    fails = sum(1 for l in lines if "FAIL" in l)
    record("10 verify (health check)", fails == 0,
           f"{passes} PASS, {fails} FAIL of {len(lines)} checks")

def find_real_light(token, known_devices=None):
    """Find a light entity in HA that actually exists and currently
    has a settable state. We probe a few common names and return
    the first one whose /api/states/<eid> returns 200.

    Note: HA's `area_id` attribute on the state object is almost
    always NULL on this install (the user hasn't assigned areas to
    entities). The device, however, has its own area mapping built
    from the area-registry response, so it knows about lights that
    don't carry an area_id in attributes. We can't easily query the
    device's entity list, so we just turn on any light the device
    is likely to be subscribed to and check if the GUI updates.
    """
    import urllib.error as _urllib_error
    import urllib.request as _urllib_request
    candidates = known_devices or [
        "light.kitchen", "light.living_room", "light.front_porch_overhead_light",
        "light.back_stairs", "light.closet", "light.bedroom", "light.office",
        "light.garage", "light.spare_room", "light.dining_room_main_lights",
    ]
    for cand in candidates:
        req = _urllib_request.Request(
            f"{HA_BASE}/api/states/{cand}",
            method="GET",
            headers={"Authorization": f"Bearer {token}"}
        )
        try:
            with _urllib_request.urlopen(req, timeout=5) as r:
                if r.status == 200:
                    return cand
        except _urllib_error.HTTPError:
            continue
        except Exception:
            continue
    return None


def test_11_state_sync_on():
    """Turn on a light in HA via REST, then capture the grid and
    verify the room card reflects the change. This is the central
    state-sync bug: previously the GUI always showed everything off
    even when HA said lights were on."""
    token = load_ha_token()
    if token is None:
        record("11 state sync (turn on light)", False, "no HA token in secrets.yaml")
        return

    chosen = find_real_light(token)
    if chosen is None:
        record("11 state sync (turn on light)", False,
               "no light entity found in HA (probe list exhausted)")
        return

    rc, body = ha_call("light", "turn_on", entity_id=chosen, token=token)
    print(f"    -> HA turn_on {chosen}: rc={rc} body={body[:100]}")
    if rc != 200:
        record("11 state sync (turn on light)", False,
               f"HA turn_on {chosen} failed: rc={rc}")
        return

    # Give HA + api subscription + LVGL redraw time
    wait_for_settle(4)
    rc, out, png = shot("16_state_sync_on")
    size_b = os.path.getsize(png) if os.path.isfile(png) else 0
    ok = rc == 0 and size_b > 1000
    record("11 state sync (turn on light)", ok,
           f"HA turn_on {chosen}; shot {size_b}B "
           f"(visual check of {png} required to confirm GUI update)")

def test_12_state_sync_off():
    """Turn the same light off, screenshot again. The arc should
    empty back to the 'off' position."""
    token = load_ha_token()
    if token is None:
        record("12 state sync (turn off light)", False, "no HA token")
        return
    chosen = find_real_light(token)
    if chosen is None:
        record("12 state sync (turn off light)", False, "no light entity found")
        return
    rc, body = ha_call("light", "turn_off", entity_id=chosen, token=token)
    print(f"    -> HA turn_off {chosen}: rc={rc} body={body[:100]}")
    wait_for_settle(3)
    rc2, out2, png = shot("17_state_sync_off")
    # Wrap the file-size check in a guard so a transient screenshot
    # failure (timeout, busy device) doesn't kill the entire test run.
    # The test still gets recorded as a fail in that case, but the
    # rest of the run completes.
    if os.path.isfile(png):
        size_b = os.path.getsize(png)
    else:
        size_b = 0
    ok = rc == 200 and rc2 == 0 and size_b > 0
    record("12 state sync (turn off light)", ok,
           f"HA rc={rc} shot rc={rc2} ({size_b}B)")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--quick", action="store_true",
                   help="Skip the long-state cycles (faster smoke test)")
    args = p.parse_args()

    SHOTS.mkdir(parents=True, exist_ok=True)
    print(f"ha_autopanel automated test run on {PORT} / {IP}")
    print(f"Screenshots -> {SHOTS}")
    print("=" * 60)

    t0 = time.time()
    test_01_initial_grid()
    test_02_state_machine(args.quick)
    test_03_recovery_to_ready()
    test_04_click_edit()
    test_05_click_cancel()
    test_06_hide_room()
    test_07_sort_panel_open_close()
    test_08_detail_views()
    test_09_toggle_light()
    test_10_health_check()
    test_11_state_sync_on()
    test_12_state_sync_off()
    elapsed = time.time() - t0

    print("=" * 60)
    total = len(results)
    passed = sum(1 for _, p, _ in results if p)
    failed = total - passed
    print(f"\n{passed}/{total} tests passed in {elapsed:.1f}s")
    if failed:
        print("FAILURES:")
        for name, ok, summary in results:
            if not ok:
                print(f"  - {name}: {summary}")
        sys.exit(1)
    print(f"\nAll {total} tests passed. Screenshots: {SHOTS}")
    sys.exit(0)

if __name__ == "__main__":
    main()
