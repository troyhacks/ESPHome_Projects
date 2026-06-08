# Crowpanel 7" ESP32-P4 — ha_autopanel

This directory is the active development environment for the
**ha_autopanel** external component.

## Files

- `Crowpanel_7.00_ESP32-P4.yaml` — the **old static config** (lightweight reference,
  not active). Uses hardcoded per-room cards in the old YAML style.
- `test_dynamic_component.yaml` — the **active config** for the new
  ha_autopanel component. The component code lives in
  `../esphome_dynamic_entity_discovery/components/ha_autopanel/`.
- `partitions.csv` — custom partition table (added when LittleFS lands;
  reserves 1MB for the `storage` partition).
- `send_cmd.py` — serial command interface for headless testing of the
  panel. See "Test workflow" below.
- `PROJECT_KNOWLEDGE.md` — historical context, decisions, and bug fixes.

## Build & flash

```bash
# Compile (incremental - ~30s)
esphome compile test_dynamic_component.yaml

# Upload
esphome upload test_dynamic_component.yaml --device COM47

# Tail logs
esphome logs test_dynamic_component.yaml --device COM47
```

## Test workflow

The ha_autopanel component reads single ASCII chars from UART0
(the same UART the logger uses for log output) and dispatches them
as commands. `send_cmd.py` is a Python wrapper that opens the serial
port, sends a command, and reads the response.

```bash
# Test the SETUP_REQUIRED screen
python send_cmd.py COM47 s

# Test AUTH_FAILED
python send_cmd.py COM47 a

# Test NOT_AUTHORIZED
python send_cmd.py COM47 n

# Test the auth probe (Retry button equivalent)
python send_cmd.py COM47 r

# Open the detail view for room 0
python send_cmd.py COM47 0

# Re-run discovery
python send_cmd.py COM47 d
```

### Full command set

| Command | Action |
| --- | --- |
| `p`, `r` | Re-probe authorization (same as Retry button) |
| `s` | Set state to SETUP_REQUIRED |
| `a` | Set state to AUTH_FAILED |
| `n` | Set state to NOT_AUTHORIZED |
| `c` | Set state to CONNECTING |
| `g` | Set state to READY (re-render the room grid) |
| `d` | Re-run full discovery |
| `0`..`9` | Open detail view for room N (0 = first card) |

The state machine on the device is:

- **BOOTING** — "Starting..." (briefly at power-on)
- **SETUP_REQUIRED** — "Setup required" + IP/AP info
- **AUTH_FAILED** — "Auth failed" + IP/AP info
- **NOT_AUTHORIZED** — "Not authorized" + HA URL + remediation steps + Retry
- **CONNECTING** — "Connecting..." (briefly during auth probe)
- **READY** — Room grid

`send_cmd.py` lets you force any state for visual verification. The
auto-probe also fires on every `api.on_client_connected` and may
overwrite manual state changes.

## The "no-code" vision

In the long run, this directory should contain only:

- `test_dynamic_component.yaml` — bare minimum to declare the
  ha_autopanel exists and where to find it
- `partitions.csv` — flash layout (including LittleFS)
- `send_cmd.py` — test interface
- `README.md` — this file

Everything else — HA URL, token, which areas to show, which entities
to hide, display overrides, default brightness — is configured via
the on-device web UI at `http://<device-ip>/autopanel` and stored in
LittleFS at `/storage/autopanel.cfg`. No recompile needed to set up
a new panel.

## v1.24 highlights (WebSocket-native state sync)

The state-sync path was rewritten in v1.24 to use Home
Assistant's raw WebSocket API (`ws://<ha>/api/websocket`)
instead of the esphome-native-API
`subscribe_home_assistant_state`. The old path triggered
a `PC 0x480dxxxx` abort ~300 ms after Panel READY (200+
`std::function` allocations in the httpd worker context
under heap pressure). The new path:

- Opens a raw WebSocket to HA on boot
- Sends HA's native `get_states` + `subscribe_events`
  messages for one-time bulk + live updates
- Uses a 1 MB pre-allocated PSRAM pool for the 229 KB
  `get_states` JSON parse (eliminates heap-lock contention
  with the SDIO RX task that was triggering
  `sdio_drv.c:1260 copy_payload` asserts)
- No more `std::function` allocations on the hot path

After ~2 hours of continuous run on the panel: 249+
`state_changed` events received, no aborts, no SDIO
asserts. See
`PROJECT_KNOWLEDGE.md` (and the v1.24 commit messages in
`../esphome_dynamic_entity_discovery`) for the full
changelog.

### Known v1.24 issues (v1.25 follow-up)

- **Cold-boot NOT_AUTHORIZED on first boot.** Fix landed
  in v1.24 commit 7: `AUTH_PROBE_TIMEOUT_MS` 5s → 15s,
  plus 3 auto-retries at 5 s intervals. The Retry
  button still works as a manual escape hatch.
- **httpd worker stack overflow** on the bft.py stress
  test (full 12-test suite: clicks, drag, screen
  changes, screenshots). Add
  `CONFIG_HTTPD_DEFAULT_TASK_STACK_SIZE: "16384"` to
  `test_dynamic_component.yaml` sdkconfig_options to fix.
  Not applied yet because the v1.24 sdkmconfig tuning
  was lost in a build-env wipe; needs the user to
  re-apply.
- **Dead code from v1.22w-v1.22v** (subscription machinery,
  bulk fetch, polls) is still in the source. The functions
  are unreachable; the members/structs/constants are
  unused. Plan to delete in a v1.24+ cleanup pass.
