# Dynamic Entity Discovery - Crowpanel 7" ESP32-P4 HMI Project

## Overview

This project implements a real-time Home Assistant entity browser and controller on the Crowpanel 7" ESP32-P4 HMI display using LVGL 9. It dynamically discovers areas and entities from HA and creates an interactive UI with room cards and entity controls.

## Project Structure

### External Component

- **Location**: `C:/Users/troys/ESPHome_Projects/esphome-dynamic-entity-discovery/components/dynamic_entity_discovery/`
- **Files**:
  - `__init__.py` - ESPHome component configuration and code generation
  - `dynamic_entity_discovery.h` - C++ header with structs and class declaration
  - `dynamic_entity_discovery.cpp` - Main implementation

### Test Configuration

- **Location**: `C:/ESPHome_Projects/Crowpanel_7.00_ESP32-P4/test_dynamic_component.yaml`

## Switching External Component Source

### Local Development (Current)

```yaml
external_components:
  - source:
      type: local
      path: C:/Users/troys/ESPHome_Projects/esphome-dynamic-entity-discovery/components
    components: [dynamic_entity_discovery]
```

**Use when**: Developing/debugging the component locally

**To force re-clone**: Delete the hashed folder at:

```text
.esphome/external_components/<hash>/components/dynamic_entity_discovery/
```

### GitHub Remote

```yaml
external_components:
  - source:
      type: git
      url: https://github.com/yourusername/esphome_dynamic_entity_discovery
      ref: main
    components: [dynamic_entity_discovery]
```

**Use when**: Using a published version on a different machine

**Note**: After switching sources, clean the build:

```bash
rm -rf .esphome/build/test-dynamic-discovery
esphome compile test_dynamic_component.yaml
```

### Key Files for External Component

```text
esphome-dynamic-entity-discovery/
└── components/
    └── dynamic_entity_discovery/
        ├── __init__.py           # ESPHome config schema & code gen
        ├── dynamic_entity_discovery.h  # Header with structs
        └── dynamic_entity_discovery.cpp # Implementation
```

## Key Implementation Details

### 1. HA API Integration

- Uses ESPHome's `http_request` component for HA API calls
- Requires a Long-Lived Access Token (LLAT) from HA (Settings → Profile → Long-Lived Access Tokens)
- API calls:
  - Template API (`/api/template`) to fetch areas with entity IDs
  - States API (`/api/states`) to fetch full entity states

### 2. Area/Entity Discovery Flow

```text
trigger_discovery()
  └── fetch_areas_()         # POST /api/template with Jinja template
      └── Returns: [{area_id, name, entities: [entity_ids...]}]
  └── fetch_entities_()     # GET /api/states
      └── Match entity_id to area via lookup table
  └── filter_and_build_room_cards_()
  └── create_ui_from_room_cards_()
```

### 3. HA Template for Areas

```jinja
{"template": "{% set ns = namespace(rooms=[]) %}{% for a in areas() %}{% set ns.rooms = ns.rooms + [{\"area_id\": a, \"name\": area_name(a), \"entities\": area_entities(a)}] %}{% endfor %}{{ ns.rooms | tojson }}"}
```

### 4. LVGL 9 Compatibility Notes

- All widgets use `lv_obj_create()` family (lv_arc_create, lv_label_create, etc.)
- Arc APIs: `lv_arc_set_min_value()`, `lv_arc_set_max_value()` (separate calls)
- Arc bg angles: `lv_arc_set_bg_start_angle()`, `lv_arc_set_bg_end_angle()` (separate calls)
- `lv_event_get_target()` returns `void*` - cast to `lv_obj_t*`
- `lv_obj_get_user_data()` returns `void*` - cast as needed

### 5. Memory Management

- `ArcCallbackData` struct allocated on heap for arc value change callbacks
- Freed via `LV_EVENT_DELETE` handler on the arc
- No dangling pointers - struct owns its data

### 6. Touch/Click Handling

- Room cards use transparent overlay button (`label_btn`) for click navigation
- Arc and ON/OFF button have their own click handlers (don't bubble to card)

### 7. Widget Enablement

When using external_components with custom LVGL C++ code, must declare widgets in yaml:

```yaml
lvgl:
  widgets:
    - arc: {}
    - label: {}
    - obj: {...}
```

## Configuration

### secrets.yaml (required)

```yaml
my_ha_api_password: "your_long_lived_access_token"
wifi_ssid: "your_ssid"
wifi_password: "your_password"
encryption_key: "32_byte_hex_key_for_api_encryption"
```

### test_dynamic_component.yaml Key Sections

```yaml
http_request:
  id: ha_http

dynamic_entity_discovery:
  id: dynamic_discovery
  ha_api_url: "http://homeassistant.local:8123"
  ha_api_password: !secret my_ha_api_password
  http_request_ref: ha_http  # References http_request component
  include_all: true
  domains:
    - light
    - switch
    - sensor
    - binary_sensor

wifi:
  on_connect:
    then:
      - delay: 1s
      - lambda: 'id(dynamic_discovery).trigger_discovery();'
```

## Key Fixes Applied

### 1. Pointer Truncation Bug (Critical)

- **Problem**: `uintptr_t combined = (entity_index << 16) | (pointer & 0xFFFF)` truncates 32-bit ESP32 pointers to 16 bits
- **Fix**: Heap-allocated `ArcCallbackData` struct with proper pointer

### 2. LVGL 9 API Migration

- `lv_arc_set_range(min, max)` → `lv_arc_set_min_value()` + `lv_arc_set_max_value()`
- `lv_arc_set_bg_angles(start, end)` → `lv_arc_set_bg_start_angle()` + `lv_arc_set_bg_end_angle()`

### 3. JSON Parsing

- ArduinoJson: use `obj["key"].isNull()` not `containsKey()` (deprecated)
- `parse_json(string)` returns `JsonDocument`

### 4. http_request Component Wiring

- ESPHome config: use `http_request_ref` (not `http_request`) as config key
- Python `cv.Optional("http_request_ref"): cv.use_id(http_request.HttpRequestComponent)`

### 5. WiFi Trigger Order

- Use `wifi.on_connect` to trigger discovery, not `api.on_client_connected`
- API connects before WiFi is ready, causing "Not connected to network" errors

### 6. Watchdog Feeding

- HTTP reads must feed watchdog frequently with `App.feed_wdt()` and `yield()` in loops
- Read loop structure:

```cpp
while (condition) {
    App.feed_wdt();
    yield();
    int read = container->read(buf, sizeof(buf));
    // handle read result
}
```

## Building

### Compile

```bash
cd C:/ESPHome_Projects/Crowpanel_7.00_ESP32-P4
esphome compile test_dynamic_component.yaml
```

### Flash

Firmware located at:

```bash
.esphome/build/test-dynamic-discovery/.pioenvs/test-dynamic-discovery/firmware.factory.bin
```

## Safety Commit

In case of major issues, rollback to:

```bash
git checkout 4f2dead1826694b685d91b4e948fe82890bbf40b
```

## TODO / Future Work

1. Implement actual HA service calls for brightness/toggle
2. Live state updates via HA websocket or polling
3. Area filtering with include/exclude lists
4. Entity state display for sensors
5. Better error recovery on network failures
6. Consider using PSRAM for large JSON buffers

## Hardware

- **Board**: Crowpanel 7" ESP32-P4 HMI (Waveshare ESP32-P4-WIFI6-TOUCH-LCD-7B)
- **Display**: MIPI DSI 1024x600
- **Touch**: GT911 capacitive touch controller
- **Framework**: ESP-IDF with PSRAM enabled

## Lessons Learned

### External Component Development

1. `add_lv_use()` in `__init__.py` runs too late for lv_conf.h generation
2. Must declare widgets explicitly in yaml's `widgets:` section
3. Re-clone external_components by deleting hashed folder in `.esphome/external_components/`

### LVGL 9 Notes

1. All widget create functions still exist (lv_arc_create, etc.) - not removed
2. Event callbacks must be plain function pointers (no lambdas with captures)
3. user_data mechanism works with heap-allocated structs

### ESP32-P4 Notes

1. 32-bit pointers - never truncate to 16 bits
2. Watchdog timeout is ~5 seconds for loopTask
3. WiFi connection can take several seconds
