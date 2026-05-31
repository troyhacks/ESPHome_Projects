# Crowpanel Internet Radio

Standalone ESP-IDF + ESP-GMF Internet Radio for the Elecrow Crowpanel 7" ESP32-P4.

## Hardware

| Component | Details |
|-----------|---------|
| Display   | EK79007 MIPI DSI, 1024x600, RGB565 |
| Touch     | GT911 Capacitive (I2C: GPIO45/46) |
| Audio     | I2S out: GPIO21(LRCLK), 22(BCLK), 23(SDATA) |
| Amplifier | GPIO30 (active-high enable) |
| Backlight | GPIO31 (PWM) |
| WiFi      | ESP32-P4 native WiFi 6 |

## Project Structure

```
crowpanel_radio/
├── CMakeLists.txt
├── project_description.json
├── sdkconfig.defaults
├── README.md
└── main/
    ├── CMakeLists.txt
    ├── Kconfig
    ├── idf_component.yml
    ├── main.c
    └── gmf_i2s_out/
        ├── esp_gmf_io_i2s_out.h
        ├── esp_gmf_io_i2s_out.c
        └── CMakeLists.txt
```

## Building

```bash
cd crowpanel_radio

idf.py reconfigure
idf.py menuconfig
# → Component config → Crowpanel Radio → WiFi SSID/Password

idf.py build
idf.py -p /dev/ttyACM0 flash monitor
```

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                    main.c                             │
│  WiFi  │  Display+Touch  │  LVGL UI  │  Audio Init  │
├──────────────────────────────────────────────────────┤
│  esp32_display_panel  │  esp_gmf_io  │  LVGL 9.x   │
│  (EK79007 + GT911)  │  (HTTP stream pipeline)        │
├──────────────────────────────────────────────────────┤
│               ESP-IDF / FreeRTOS                      │
└──────────────────────────────────────────────────────┘
```

## TODO - Audio Pipeline

The current code initializes I2S audio but does not yet implement the full GMF streaming pipeline. To complete the radio:

1. **Add GMF packages**: Clone `https://github.com/espressif/esp-gmf` as local components
2. **Create pipeline**: `io_http` → `aud_dec` → `aud_rate_cvt` → `aud_ch_cvt` → `aud_bit_cvt` → `gmf_i2s_out`
3. **Wire stream URL**: Set via `esp_gmf_pipeline_set_in_uri()`

## Dependencies

- `espressif/esp32_display_panel` - Display + touch drivers (CrowPanel support)
- `espressif/esp_lcd_ek79007` - EK79007 MIPI DSI panel driver
- `espressif/esp_lcd_touch_gt911` - GT911 touch driver
- `lvgl/lvgl` - LVGL graphics library
- `espressif/gmf_*` - ESP-GMF audio pipeline components

## Resources

- [Elecrow CrowPanel Wiki](https://www.elecrow.com/wiki/CrowPanel_Advanced_7inch_ESP32-P4_HMI_AI_Display_1024x600_IPS_Touch_Screen_with_WiFi6_Compatible_with_ArduinoLVGL.html)
- [Elecrow GitHub](https://github.com/Elecrow-RD/CrowPanel-Advanced-7inch-ESP32-P4-HMI-AI-Display-1024x600-IPS-Touch-Screen)
- [ESP-GMF](https://github.com/espressif/esp-gmf)
- [ESP32 Display Panel Component](https://components.espressif.com/components/espressif/esp32_display_panel)
