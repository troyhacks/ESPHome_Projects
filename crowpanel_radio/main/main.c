/**
 * Crowpanel Internet Radio
 * ESP32-P4 + ESP-IDF + ESP-GMF Audio Pipeline
 *
 * Pipeline: io_http → aud_dec → aud_rate_cvt → aud_ch_cvt → aud_bit_cvt → io_i2s
 *
 * Hardware:
 *   - EK79007 MIPI DSI Display (1024x600)
 *   - GT911 Capacitive Touch (I2C: GPIO45/46)
 *   - I2S Audio Out: GPIO21(LRCLK), 22(BCLK), 23(SDATA)
 *   - Amplifier enable: GPIO30
 *
 * Initialization Phases:
 *   1. Driver Init  - Grab SDIO memory while heap is pristine
 *   2. UI Init     - Turn on screen, show "Connecting..."
 *   3. Connect     - Non-blocking WiFi connect, UI updates async
 */

#include <math.h>
#include <string.h>
#include "wifi_credentials.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_system.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_log.h"
#include "esp_err.h"
#include "esp_task_wdt.h"
#include "hal/lpwdt_ll.h"
#include "soc/lp_wdt_struct.h"
#include "nvs_flash.h"
#include "esp_lcd_panel_ops.h"
#include "esp_hosted.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_touch_gt911.h"
#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "driver/gpio.h"
#include "lvgl.h"

static const char *TAG = "RADIO";

/* ============================================
 * Radio Stations
 * ============================================ */

typedef struct {
    const char *name;
    const char *url;
} radio_station_t;

static const radio_station_t stations[] = {
    {"Lounge Ibiza", "https://0nlineradio.radioho.st/lounge-ibiza-chillout-lounge?ref=rb26"},
    {"Jazz FM",      "https://jazz-wr01.ice.infomaniak.ch/jazz-wr01-128.mp3"},
    {"SomaFM Groove","https://ice1.somafm.com/groovesalad-128-mp3"},
    {"KCRW Los Angeles", "https://streams.kcrw.com/kcrw_mp3"},
};
static int current_station = 0;
static int num_stations = sizeof(stations) / sizeof(stations[0]);

/* ============================================
 * Audio State
 * ============================================ */

static int g_volume = 80;

/* Async audio control — LVGL callbacks set flags, audio_control_task acts.
 * This prevents GMF blocking calls (DNS resolve, socket open, stream start)
 * from blocking lv_timer_handler() and causing SDIO starvation. */
typedef enum {
    AUDIO_REQ_NONE = 0,
    AUDIO_REQ_PLAY,
    AUDIO_REQ_STOP,
    AUDIO_REQ_SWITCH_STATION,
} audio_req_t;

static volatile audio_req_t s_audio_req = AUDIO_REQ_NONE;
static volatile int s_req_station_idx = 0;

/* ============================================
 * WiFi - Split init for memory sequencing
 * ============================================ */

#define WIFI_CONNECTED_BIT BIT0
static EventGroupHandle_t s_wifi_event_group;

static void update_ui_wifi_status(const char *status)
{
    // This will be called from wifi_event_handler to update LVGL UI
    // Status: "Connecting...", "Connected", "Disconnected", etc.
    extern void set_wifi_status_label(const char *status);
    set_wifi_status_label(status);
}

static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                                int32_t event_id, void *event_data)
{
    (void)arg;
    (void)event_data;
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        ESP_LOGI(TAG, "WiFi driver started, initiating connect...");
        update_ui_wifi_status("Connecting...");
        /* Don't call esp_wifi_connect() here - esp_wifi_start() already
         * triggers auto-connect internally. Calling again causes double-connect. */
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        ESP_LOGW(TAG, "WiFi disconnected, reconnecting...");
        update_ui_wifi_status("Disconnected");
        esp_wifi_connect();
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ESP_LOGI(TAG, "WiFi connected!");
        update_ui_wifi_status("Connected");
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

/**
 * Phase 1: Initialize WiFi driver and grab SDIO memory.
 * This MUST be called before any other memory allocations
 * to ensure the ESP-Hosted SDIO buffers get contiguous DMA memory.
 */
static void wifi_driver_init(void)
{
    s_wifi_event_group = xEventGroupCreate();

    /* nvs_flash_init() is already called in app_main() - do not call again here */

    ESP_ERROR_CHECK(esp_event_loop_create_default());

    ESP_ERROR_CHECK(esp_hosted_init());
    ESP_ERROR_CHECK(esp_hosted_connect_to_slave());

    esp_hosted_coprocessor_fwver_t c6_fw_version;
    ESP_ERROR_CHECK_WITHOUT_ABORT(esp_hosted_get_coprocessor_fwversion(&c6_fw_version));
    
    if (c6_fw_version.major1 >= 2 && c6_fw_version.major1 <= 1000) {
        ESP_LOGI(TAG, "ESP-Hosted C6 Firmware is version %d.%d.%d", c6_fw_version.major1, c6_fw_version.minor1, c6_fw_version.patch1);
    } else {
        ESP_LOGI(TAG, "ESP-Hosted C6 Firmware is older than version 2.15.12");
    }

    ESP_ERROR_CHECK(esp_netif_init());
    esp_netif_t *sta_netif = esp_netif_create_default_wifi_sta();
    assert(sta_netif);

    wifi_init_config_t wifi_initiation = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&wifi_initiation));

    ESP_ERROR_CHECK_WITHOUT_ABORT(esp_wifi_restore());

    vTaskDelay(pdMS_TO_TICKS(1000));

    esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL);
    esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event_handler, NULL);

    wifi_config_t wifi_config = {0};
    strncpy((char *)wifi_config.sta.ssid, WIFI_SSID, sizeof(wifi_config.sta.ssid));
    strncpy((char *)wifi_config.sta.password, WIFI_PASSWORD, sizeof(wifi_config.sta.password));

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));

    /* Start the WiFi driver - this is what actually allocates SDIO buffers.
     * Do NOT call esp_wifi_connect() here - that's Phase 3. */
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_LOGI(TAG, "WiFi driver initialized, SDIO memory allocated");
}

/**
 * Phase 3: Connect to AP (non-blocking).
 * Called after UI is initialized so user sees "Connecting..." status.
 */
static void wifi_connect(void)
{
    ESP_LOGI(TAG, "Initiating WiFi connection to %s...", WIFI_SSID);
    /* esp_wifi_start was already called in wifi_driver_init(),
     * we just need to trigger the connect which is handled by the event handler */
    esp_wifi_connect();
}

/* ============================================
 * Display - EK79007 MIPI DSI (via esp32_display_panel)
 * ============================================ */

#include "esp_lcd_mipi_dsi.h"
#include "esp_lcd_ek79007.h"
#include "esp_ldo_regulator.h"

static esp_lcd_panel_handle_t panel_handle = NULL;

/* Flush callback - LVGL calls this after rendering an area.
 * EK79007 DPI panel continuously scans the framebuffer with no DMA
 * completion interrupt, so we immediately signal flush done after
 * the copy. This prevents LVGL's rendering thread from blocking on
 * wait_for_flushing. */
static void disp_flush(lv_display_t *disp, const lv_area_t *area, uint8_t *color_p)
{
    (void)disp;
    (void)area;
    (void)color_p;
    /* EK79007 DPI panel auto-scans the framebuffer continuously.
     * The rendered data is already in the framebuffer we set with
     * lv_display_set_buffers(). No manual copy needed.
     * Immediately signal flush done so LVGL proceeds with next frame. */
    lv_display_flush_ready(disp);
}
static lv_display_t *lvgl_disp = NULL;
static esp_ldo_channel_handle_t s_mipi_phy_ldo = NULL;

/**
 * EK79007 Display Initialization
 *
 * Hardware connections on CrowPanel ESP32-P4:
 *   - MIPI DSI lanes: native DSI interface
 *   - Reset GPIO: GPIO27
 *   - Backlight: GPIO31 (controlled separately)
 *   - MIPI PHY power: LDO channel 3 @ 2500mV
 */
static void display_init(void)
{
    ESP_LOGI(TAG, "Initializing EK79007 MIPI DSI display (1024x600)");

    /* 0. Acquire LDO channel for MIPI PHY power */
    esp_ldo_channel_config_t ldo_cfg = {
        .chan_id = 3,
        .voltage_mv = 2500,
    };
    ESP_ERROR_CHECK(esp_ldo_acquire_channel(&ldo_cfg, &s_mipi_phy_ldo));
    ESP_LOGI(TAG, "MIPI PHY LDO acquired (channel 3, 2500mV)");

        /* 3. Define DPI configuration for 1024x600 @ 60Hz */
    esp_lcd_dpi_panel_config_t dpi_config = EK79007_1024_600_PANEL_60HZ_CONFIG_CF(LCD_COLOR_FMT_RGB565);
    ESP_LOGI(TAG, "dpi_config before override:");
    ESP_LOGI(TAG, "  dpi_clock_freq_mhz   = %.2f MHz", (double)dpi_config.dpi_clock_freq_mhz);
    ESP_LOGI(TAG, "  num_fbs             = %u", dpi_config.num_fbs);
    ESP_LOGI(TAG, "  video_timing:");
    ESP_LOGI(TAG, "    h_size            = %u", dpi_config.video_timing.h_size);
    ESP_LOGI(TAG, "    v_size            = %u", dpi_config.video_timing.v_size);
    ESP_LOGI(TAG, "    hsync_pulse_width = %u", dpi_config.video_timing.hsync_pulse_width);
    ESP_LOGI(TAG, "    hsync_back_porch  = %u", dpi_config.video_timing.hsync_back_porch);
    ESP_LOGI(TAG, "    hsync_front_porch = %u", dpi_config.video_timing.hsync_front_porch);
    ESP_LOGI(TAG, "    vsync_pulse_width = %u", dpi_config.video_timing.vsync_pulse_width);
    ESP_LOGI(TAG, "    vsync_back_porch  = %u", dpi_config.video_timing.vsync_back_porch);
    ESP_LOGI(TAG, "    vsync_front_porch = %u", dpi_config.video_timing.vsync_front_porch);

    dpi_config.dpi_clock_freq_mhz               = 60.00;
    dpi_config.num_fbs                          = 2;

    dpi_config.video_timing.h_size              = 1024;
    dpi_config.video_timing.hsync_front_porch   = 406;  /* 1600 - 1024 - 10 - 160 = 406 */
    dpi_config.video_timing.hsync_pulse_width   = 10;
    dpi_config.video_timing.hsync_back_porch    = 160;

    dpi_config.video_timing.v_size              = 600;
    dpi_config.video_timing.vsync_front_porch   = 1;    /* VFP: minimal (v_total=625 → exactly 60fps) */
    dpi_config.video_timing.vsync_pulse_width   = 1;
    dpi_config.video_timing.vsync_back_porch    = 23;   /* VBP: 23 lines for panel to latch frame before next vsync */

    ESP_LOGI(TAG, "dpi_config after override:");
    ESP_LOGI(TAG, "  dpi_clock_freq_mhz   = %.2f MHz", (double)dpi_config.dpi_clock_freq_mhz);
    ESP_LOGI(TAG, "  num_fbs             = %u", dpi_config.num_fbs);
    ESP_LOGI(TAG, "  video_timing:");
    ESP_LOGI(TAG, "    h_size            = %u", dpi_config.video_timing.h_size);
    ESP_LOGI(TAG, "    v_size            = %u", dpi_config.video_timing.v_size);
    ESP_LOGI(TAG, "    hsync_pulse_width = %u", dpi_config.video_timing.hsync_pulse_width);
    ESP_LOGI(TAG, "    hsync_back_porch  = %u", dpi_config.video_timing.hsync_back_porch);
    ESP_LOGI(TAG, "    hsync_front_porch = %u", dpi_config.video_timing.hsync_front_porch);
    ESP_LOGI(TAG, "    vsync_pulse_width = %u", dpi_config.video_timing.vsync_pulse_width);
    ESP_LOGI(TAG, "    vsync_back_porch  = %u", dpi_config.video_timing.vsync_back_porch);
    ESP_LOGI(TAG, "    vsync_front_porch = %u", dpi_config.video_timing.vsync_front_porch);

    /* Full timing validation */
    uint32_t h_total = dpi_config.video_timing.h_size
                     + dpi_config.video_timing.hsync_pulse_width
                     + dpi_config.video_timing.hsync_back_porch
                     + dpi_config.video_timing.hsync_front_porch;
    uint32_t v_total = dpi_config.video_timing.v_size
                     + dpi_config.video_timing.vsync_pulse_width
                     + dpi_config.video_timing.vsync_back_porch
                     + dpi_config.video_timing.vsync_front_porch;
    float line_time_us   = (float)h_total / dpi_config.dpi_clock_freq_mhz;
    float frame_time_ms  = (float)(h_total * v_total) / dpi_config.dpi_clock_freq_mhz / 1000.0f;
    float fps            = 1000.0f / frame_time_ms;
    float h_blank        = h_total - dpi_config.video_timing.h_size;
    float v_blank        = v_total - dpi_config.video_timing.v_size;
    float hsync_pct      = (float)dpi_config.video_timing.hsync_pulse_width / h_total * 100.0f;
    float hblank_pct     = h_blank / h_total * 100.0f;
    float vblank_pct     = (float)v_blank / v_total * 100.0f;
    float pixel_rate_mhz = dpi_config.dpi_clock_freq_mhz;
    float data_rate_mbps  = pixel_rate_mhz * 16.0f;  /* RGB565 = 16 bpp */
    float lane_rate_mbps  = data_rate_mbps / 2.0f;   /* 2 lanes */
    ESP_LOGI(TAG, "  === Timing Validation ===");
    ESP_LOGI(TAG, "  h_total         = %u  (%u + %u + %u + %u)",
             h_total, dpi_config.video_timing.h_size,
             dpi_config.video_timing.hsync_pulse_width,
             dpi_config.video_timing.hsync_back_porch,
             dpi_config.video_timing.hsync_front_porch);
    ESP_LOGI(TAG, "  v_total         = %u  (%u + %u + %u + %u)",
             v_total, dpi_config.video_timing.v_size,
             dpi_config.video_timing.vsync_pulse_width,
             dpi_config.video_timing.vsync_back_porch,
             dpi_config.video_timing.vsync_front_porch);
    ESP_LOGI(TAG, "  line_time       = %.3f us  (%.3f ns)", line_time_us, line_time_us * 1000.0f);
    ESP_LOGI(TAG, "  frame_time      = %.3f ms  → %.2f fps%s",
             frame_time_ms, fps, (fabsf(fps - 60.0f) < 0.1f) ? " [EXACT 60Hz]" : "");
    ESP_LOGI(TAG, "  hsync           = %.1f%% of line", hsync_pct);
    ESP_LOGI(TAG, "  h blank         = %u px (%.1f%% of line)", (unsigned)h_blank, hblank_pct);
    ESP_LOGI(TAG, "  v blank         = %u lines (%.1f%% of frame)", (unsigned)v_blank, vblank_pct);
    ESP_LOGI(TAG, "  pixel_rate      = %.2f Mpx/s", pixel_rate_mhz);
    ESP_LOGI(TAG, "  data_rate       = %.1f Mbps  (RGB565 @ %.2f Mpx/s)", data_rate_mbps, pixel_rate_mhz);
    ESP_LOGI(TAG, "  lane_rate       = %.1f Mbps  (%.1f Mbps/lane × 2 lanes)",
             lane_rate_mbps, lane_rate_mbps);

    /* 1. Create DSI bus */
    /* Calculate lane bit rate from pixel clock:
     * RGB565 = 16 bits per pixel, 2 lanes
     * lane_bit_rate_mbps = dpi_clock_freq_mhz * 16 / 2 = dpi_clock_freq_mhz * 8 */
    uint32_t lane_bit_rate = (uint32_t)(dpi_config.dpi_clock_freq_mhz * 8);
    esp_lcd_dsi_bus_handle_t dsi_bus = NULL;
    esp_lcd_dsi_bus_config_t bus_config = {
        .bus_id = 0,
        .num_data_lanes = 2,           // EK79007 uses 2-lane DSI
        .phy_clk_src = MIPI_DSI_PHY_CLK_SRC_DEFAULT,
        .lane_bit_rate_mbps = lane_bit_rate,
    };
    ESP_LOGI(TAG, "DSI bus: clock=%.2f MHz, lanes=2, lane_bit_rate=%u Mbps",
             (double)dpi_config.dpi_clock_freq_mhz, lane_bit_rate);
    ESP_ERROR_CHECK(esp_lcd_new_dsi_bus(&bus_config, &dsi_bus));
    ESP_LOGI(TAG, "DSI bus created");

    /* 2. Create DBI panel IO (for sending commands) */
    esp_lcd_panel_io_handle_t panel_io = NULL;
    esp_lcd_dbi_io_config_t io_config = {
        .virtual_channel = 0,
        .lcd_cmd_bits = 8,
        .lcd_param_bits = 8,
    };
    ESP_ERROR_CHECK(esp_lcd_new_panel_io_dbi(dsi_bus, &io_config, &panel_io));
    ESP_LOGI(TAG, "DBI panel IO created");

    /* 4. Build vendor config for EK79007 */
    ek79007_vendor_config_t vendor_config = {
        .init_cmds = NULL,  // Use default init commands from driver
        .init_cmds_size = 0,
        .mipi_config = {
            .dsi_bus = dsi_bus,
            .dpi_config = &dpi_config,
            .lane_num = 2,
        },
    };

    /* 5. Panel device config */
    esp_lcd_panel_dev_config_t panel_dev_config = {
        .reset_gpio_num = 27,         // CrowPanel EK79007 reset GPIO
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = 16,
        .vendor_config = (void *)&vendor_config,
    };

    /* 6. Create EK79007 panel */
    ESP_ERROR_CHECK(esp_lcd_new_panel_ek79007(panel_io, &panel_dev_config, &panel_handle));
    ESP_LOGI(TAG, "EK79007 panel created");

    /* 7. Initialize the panel (reset + send init commands) */
    ESP_ERROR_CHECK(esp_lcd_panel_init(panel_handle));

    /* 8. Get framebuffer address for LVGL.
     * Use 1 framebuffer — double-buffered FULL mode causes the panel to scan
     * a buffer that LVGL has swapped away, producing garbage on screen. */
    void *fb1 = NULL;
    ESP_ERROR_CHECK(esp_lcd_dpi_panel_get_frame_buffer(panel_handle, 1, &fb1));
    ESP_LOGI(TAG, "Framebuffer: %p", fb1);

    /* 9. Create LVGL display with the framebuffer.
     * EK79007 DPI panel continuously scans PSRAM framebuffer - no DMA interrupt.
     * disp_flush() immediately signals lv_display_flush_ready() so LVGL proceeds.
     * Single buffer + FULL render mode: LVGL renders entire screen each frame. */
    lvgl_disp = lv_display_create(1024, 600);
    lv_display_set_buffers(lvgl_disp, fb1, NULL, 1024 * 600 * 2, LV_DISPLAY_RENDER_MODE_FULL);
    lv_display_set_color_format(lvgl_disp, LV_COLOR_FORMAT_RGB565);

    /* 10. Register flush callback - immediately signals flush done since
     * the EK79007 DPI panel continuously scans with no DMA completion interrupt. */
    lv_display_set_flush_cb(lvgl_disp, disp_flush);

    ESP_LOGI(TAG, "EK79007 display initialized");
}

static void backlight_init(void)
{
    gpio_config_t gpio_cfg = {
        .pin_bit_mask = 1ULL << 31,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = false,
        .pull_down_en = false,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&gpio_cfg);
    gpio_set_level(31, 1);
    ESP_LOGI(TAG, "Backlight on (GPIO31)");
}

/* ============================================
 * Touch - GT911 via I2C
 * ============================================ */

static esp_lcd_touch_handle_t touch_handle = NULL;
static i2c_master_bus_handle_t i2c_bus = NULL;

static void touch_read_cb(lv_indev_t *indev, lv_indev_data_t *data)
{
    (void)indev;
    static esp_lcd_touch_point_data_t point;
    uint8_t count = 0;

    if (touch_handle == NULL) {
        data->state = LV_INDEV_STATE_RELEASED;
        return;
    }

    /* GT911 read — no artificial debounce, LVGL drives the polling rate.
     * esp_lcd_touch_get_data() is non-blocking; returns ESP_OK with count=0 if no touch. */
    esp_err_t err = esp_lcd_touch_get_data(touch_handle, &point, &count, 1);
    if (err != ESP_OK || count == 0) {
        data->state = LV_INDEV_STATE_RELEASED;
        return;
    }

    data->point.x = point.x;
    data->point.y = point.y;
    data->state = (point.strength > 0) ? LV_INDEV_STATE_PRESSED : LV_INDEV_STATE_RELEASED;
}

static void touch_init(void)
{
    ESP_LOGI(TAG, "Initialize I2C for touch (GPIO45=SDA, GPIO46=SCL)");
    i2c_master_bus_config_t i2c_cfg = {
        .i2c_port = 0,
        .sda_io_num = 45,
        .scl_io_num = 46,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    ESP_ERROR_CHECK(i2c_new_master_bus(&i2c_cfg, &i2c_bus));

    esp_lcd_panel_io_i2c_config_t io_cfg = {
        .dev_addr = ESP_LCD_TOUCH_IO_I2C_GT911_ADDRESS,
        .control_phase_bytes = 1,
        .lcd_cmd_bits = 16,
        .flags.disable_control_phase = 1,
        .scl_speed_hz = 400000,
    };

    esp_lcd_panel_io_handle_t tp_io;
    ESP_ERROR_CHECK(esp_lcd_new_panel_io_i2c(i2c_bus, &io_cfg, &tp_io));

    esp_lcd_touch_config_t tp_cfg = {
        .x_max = 1024,
        .y_max = 600,
        .rst_gpio_num = 40,
        .int_gpio_num = 42,
        .levels = { .reset = 0, .interrupt = 0 },
        .flags = { .swap_xy = false, .mirror_x = false, .mirror_y = false },
    };

    esp_err_t err = esp_lcd_touch_new_i2c_gt911(tp_io, &tp_cfg, &touch_handle);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "GT911 primary addr failed, trying backup");
        io_cfg.dev_addr = ESP_LCD_TOUCH_IO_I2C_GT911_ADDRESS_BACKUP;
        ESP_ERROR_CHECK(esp_lcd_new_panel_io_i2c(i2c_bus, &io_cfg, &tp_io));
        ESP_ERROR_CHECK(esp_lcd_touch_new_i2c_gt911(tp_io, &tp_cfg, &touch_handle));
    }
    ESP_LOGI(TAG, "Touch initialized: GT911");
}

/* ============================================
 * LVGL
 * ============================================ */

static void lvgl_init(void)
{
    ESP_LOGI(TAG, "Initialize LVGL");
    /* Display is created in display_init() - nothing to do here */
}

/* ============================================
 * Audio Hardware - I2S + Amplifier
 * ============================================ */

static i2s_chan_handle_t g_i2s_tx = NULL;
#define AMP_GPIO 30

static void audio_amp_init(void)
{
    gpio_config_t gpio_cfg = {
        .pin_bit_mask = 1ULL << AMP_GPIO,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = false,
        .pull_down_en = false,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&gpio_cfg);
    gpio_set_level(AMP_GPIO, 1);
    ESP_LOGI(TAG, "Amplifier enabled (GPIO%d)", AMP_GPIO);
}

static i2s_chan_handle_t audio_i2s_init(void)
{
    i2s_chan_config_t chan_cfg = {
        .id = I2S_NUM_0,
        .role = I2S_ROLE_MASTER,
        .dma_desc_num = 8,
        .dma_frame_num = 256,
        .auto_clear = true,
    };
    i2s_chan_handle_t tx;
    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, &tx, NULL));

    i2s_std_config_t std_cfg = {
        .clk_cfg = {
            .sample_rate_hz = 44100,
            .clk_src = I2C_CLK_SRC_DEFAULT,
            .mclk_multiple = I2S_MCLK_MULTIPLE_256,
        },
        .slot_cfg = {
            .data_bit_width = I2S_DATA_BIT_WIDTH_16BIT,
            .slot_bit_width = I2S_SLOT_BIT_WIDTH_AUTO,
            .slot_mode = I2S_SLOT_MODE_STEREO,
            .slot_mask = I2S_STD_SLOT_BOTH,
            .ws_width = 16,
            .ws_pol = false,
            .bit_shift = true,
            .left_align = true,
            .big_endian = false,
            .bit_order_lsb = false,
        },
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = 22,
            .ws = 21,
            .dout = 23,
            .din = I2S_GPIO_UNUSED,
        },
    };

    ESP_ERROR_CHECK(i2s_channel_init_std_mode(tx, &std_cfg));
    ESP_ERROR_CHECK(i2s_channel_enable(tx));
    ESP_LOGI(TAG, "I2S initialized (BCLK=22, WS=21, DOUT=23)");
    return tx;
}

/* ============================================
 * ESP-GMF Audio Pipeline
 *
 * Uses the pure GMF pipeline architecture:
 *   io_http → aud_dec → aud_rate_cvt → aud_ch_cvt → aud_bit_cvt → io_i2s
 *
 * We register our custom gmf_i2s_out as "io_i2s" in the pool.
 * ============================================ */

#include "esp_gmf_pool.h"
#include "esp_gmf_pipeline.h"
#include "esp_gmf_task.h"
#include "esp_gmf_element.h"
#include "esp_gmf_audio_dec.h"
#include "esp_gmf_io_http.h"
#include "esp_gmf_io_i2s_out.h"
#include "gmf_loader_setup_defaults.h"

static esp_gmf_pipeline_handle_t g_pipeline = NULL;
static esp_gmf_task_handle_t g_gmf_task = NULL;
static esp_gmf_io_handle_t g_i2s_io = NULL;
static esp_gmf_pool_handle_t g_pool = NULL;
static bool g_gmf_initialized = false;

/* Forward declarations */
static void stop_gmf_pipeline(void);

static esp_gmf_err_t gmf_event_handler(esp_gmf_event_pkt_t *event, void *ctx)
{
    (void)ctx;
    ESP_LOGI(TAG, "GMF event: el=%s type=%x sub=%d",
             OBJ_GET_TAG(event->from), event->type, event->sub);

    if (event->sub == ESP_GMF_EVENT_STATE_ERROR) {
        ESP_LOGE(TAG, "Pipeline error!");
    } else if (event->sub == ESP_GMF_EVENT_STATE_STOPPED) {
        ESP_LOGI(TAG, "Pipeline stopped");
    } else if (event->sub == ESP_GMF_EVENT_STATE_FINISHED) {
        ESP_LOGI(TAG, "Pipeline finished");
    }
    return ESP_GMF_ERR_OK;
}

static void start_gmf_pipeline(const char *url)
{
    if (g_pipeline) {
        stop_gmf_pipeline();
        vTaskDelay(pdMS_TO_TICKS(500));
    }

    ESP_LOGI(TAG, "Starting GMF pipeline: %s", url);

    /* Create GMF pool and load default components (only once) */
    if (g_pool == NULL) {
        esp_gmf_pool_init(&g_pool);

        /* Load default GMF components:
         * - io_http: HTTP stream reader (registered as "io_http")
         * - aud_dec: audio decoder (registered as "aud_dec")
         * - aud_rate_cvt, aud_ch_cvt, aud_bit_cvt: audio format converters
         * - io_codec_dev: codec device output (registered as "io_codec_dev")
         */
        gmf_loader_setup_io_default(g_pool);
        gmf_loader_setup_audio_codec_default(g_pool);
        gmf_loader_setup_audio_effects_default(g_pool);
        g_gmf_initialized = true;
    }

    /* Create pipeline: io_http → [chain] → io_i2s
     * The io_i2s is our custom registered I/O element
     */
    const char *chain_names[] = {
        "aud_dec", "aud_rate_cvt", "aud_ch_cvt", "aud_bit_cvt"
    };

    esp_gmf_pipeline_handle_t pipe = NULL;
    int ret = esp_gmf_pool_new_pipeline(
        g_pool,
        "io_http",
        chain_names, sizeof(chain_names) / sizeof(char *),
        "io_i2s",
        &pipe);

    if (ret != ESP_GMF_ERR_OK) {
        ESP_LOGE(TAG, "Failed to create pipeline: %d", ret);
        return;
    }
    g_pipeline = pipe;

    /* Set stream URL - decoder auto-detects format from stream */
    esp_gmf_pipeline_set_in_uri(pipe, url);

    /* Create and bind GMF task */
    esp_gmf_task_cfg_t task_cfg = DEFAULT_ESP_GMF_TASK_CONFIG();
    task_cfg.name = "gmf_radio";
    task_cfg.thread.stack = 8192;
    task_cfg.thread.prio = 3;
    task_cfg.thread.core = 0xF;  // 0xF = any core

    ret = esp_gmf_task_init(&task_cfg, &g_gmf_task);
    if (ret != ESP_GMF_ERR_OK) {
        ESP_LOGE(TAG, "Failed to create GMF task: %d", ret);
        return;
    }

    esp_gmf_pipeline_bind_task(pipe, g_gmf_task);
    esp_gmf_pipeline_set_event(pipe, gmf_event_handler, NULL);

    /* Start the pipeline */
    esp_gmf_pipeline_run(pipe);
    ESP_LOGI(TAG, "GMF pipeline running");
}

static void stop_gmf_pipeline(void)
{
    if (g_pipeline) {
        esp_gmf_pipeline_stop(g_pipeline);
        esp_gmf_pipeline_destroy(g_pipeline);
        g_pipeline = NULL;
    }
    if (g_gmf_task) {
        esp_gmf_task_deinit(g_gmf_task);
        g_gmf_task = NULL;
    }
    ESP_LOGI(TAG, "GMF pipeline stopped");
}

static void register_i2s_output(i2s_chan_handle_t i2s_chan)
{
    /* Initialize our custom GMF I2S output element */
    i2s_out_io_cfg_t i2s_cfg = {
        .name = "io_i2s",
        .i2s_chan = i2s_chan,
        .volume = g_volume,
    };

    esp_gmf_err_t ret = esp_gmf_io_i2s_out_init(&i2s_cfg, &g_i2s_io);
    if (ret != ESP_GMF_ERR_OK) {
        ESP_LOGE(TAG, "Failed to create I2S GMF element: %d", ret);
        return;
    }

    /* Register with GMF pool so pipeline can find it as "io_i2s" */
    ret = esp_gmf_pool_register_io(g_pool, g_i2s_io, "io_i2s");
    if (ret != ESP_GMF_ERR_OK) {
        ESP_LOGE(TAG, "Failed to register I2S IO: %d", ret);
        return;
    }

    ESP_LOGI(TAG, "I2S output registered as 'io_i2s'");
}

/* ============================================
 * LVGL Radio UI
 * ============================================ */

static lv_obj_t *station_label = NULL;
static lv_obj_t *status_label = NULL;
static lv_obj_t *wifi_status_label = NULL;
static bool ui_is_playing = false;

void set_wifi_status_label(const char *status)
{
    if (wifi_status_label) {
        lv_label_set_text(wifi_status_label, status);
        lv_obj_invalidate(wifi_status_label);
    }
}

static void btn_prev_cb(lv_event_t *e)
{
    (void)e;
    current_station = (current_station - 1 + num_stations) % num_stations;
    if (station_label) lv_label_set_text(station_label, stations[current_station].name);
    s_req_station_idx = current_station;
    if (ui_is_playing) s_audio_req = AUDIO_REQ_SWITCH_STATION;
}

static void btn_play_cb(lv_event_t *e)
{
    (void)e;
    if (ui_is_playing) {
        s_audio_req = AUDIO_REQ_STOP;
    } else {
        s_audio_req = AUDIO_REQ_PLAY;
    }
}

static void btn_station_cb(lv_event_t *e)
{
    int idx = (int)(intptr_t)lv_event_get_user_data(e);
    current_station = idx;
    if (station_label) lv_label_set_text(station_label, stations[idx].name);
    s_req_station_idx = idx;
    if (ui_is_playing) s_audio_req = AUDIO_REQ_SWITCH_STATION;
}

static void btn_next_cb(lv_event_t *e)
{
    (void)e;
    current_station = (current_station + 1) % num_stations;
    if (station_label) lv_label_set_text(station_label, stations[current_station].name);
    s_req_station_idx = current_station;
    if (ui_is_playing) s_audio_req = AUDIO_REQ_SWITCH_STATION;
}

static void vol_changed_cb(lv_event_t *e)
{
    lv_obj_t *slider = lv_event_get_target(e);
    g_volume = lv_slider_get_value(slider);
    if (g_i2s_io) {
        esp_gmf_io_i2s_out_set_volume(g_i2s_io, g_volume);
    }
    ESP_LOGI(TAG, "Volume: %d%%", g_volume);
}

static void create_radio_ui(void)
{
    lv_obj_t *scr = lv_scr_act();
    lv_obj_set_style_bg_color(scr, lv_color_hex(0x111827), LV_PART_MAIN);

    /* WiFi status - top right */
    wifi_status_label = lv_label_create(scr);
    lv_label_set_text(wifi_status_label, "WiFi: Init...");
    lv_obj_set_style_text_font(wifi_status_label, &lv_font_montserrat_14, 0);
    lv_obj_set_style_text_color(wifi_status_label, lv_color_hex(0x9CA3AF), 0);
    lv_obj_align(wifi_status_label, LV_ALIGN_TOP_RIGHT, -20, 20);

    /* Title */
    lv_obj_t *title = lv_label_create(scr);
    lv_label_set_text(title, "INTERNET RADIO");
    lv_obj_set_style_text_font(title, &lv_font_montserrat_14, 0);
    lv_obj_set_style_text_color(title, lv_color_hex(0xFFFFFF), 0);
    lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 20);

    /* Station name */
    station_label = lv_label_create(scr);
    lv_label_set_text(station_label, stations[current_station].name);
    lv_obj_set_style_text_font(station_label, &lv_font_montserrat_14, 0);
    lv_obj_set_style_text_color(station_label, lv_color_hex(0x38BDF8), 0);
    lv_obj_align(station_label, LV_ALIGN_TOP_MID, 0, 70);

    /* Status */
    status_label = lv_label_create(scr);
    lv_label_set_text(status_label, "Ready");
    lv_obj_set_style_text_color(status_label, lv_color_hex(0x9CA3AF), 0);
    lv_obj_align(status_label, LV_ALIGN_TOP_MID, 0, 105);

    /* Transport container */
    lv_obj_t *ctrl = lv_obj_create(scr);
    lv_obj_set_size(ctrl, 400, 100);
    lv_obj_align(ctrl, LV_ALIGN_CENTER, 0, -50);
    lv_obj_set_style_bg_color(ctrl, lv_color_hex(0x1a1a2e), 0);
    lv_obj_set_style_radius(ctrl, 16, 0);
    lv_obj_set_flex_flow(ctrl, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(ctrl, LV_FLEX_ALIGN_SPACE_EVENLY, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);

    /* Prev */
    lv_obj_t *btn_prev = lv_btn_create(ctrl);
    lv_obj_set_size(btn_prev, 80, 70);
    lv_obj_t *lbl_prev = lv_label_create(btn_prev);
    lv_label_set_text(lbl_prev, "|<");
    lv_obj_center(lbl_prev);
    lv_obj_add_event_cb(btn_prev, btn_prev_cb, LV_EVENT_CLICKED, NULL);

    /* Play */
    lv_obj_t *btn_play = lv_btn_create(ctrl);
    lv_obj_set_size(btn_play, 100, 70);
    lv_obj_t *lbl_play = lv_label_create(btn_play);
    lv_label_set_text(lbl_play, "PLAY");
    lv_obj_center(lbl_play);
    lv_obj_add_event_cb(btn_play, btn_play_cb, LV_EVENT_CLICKED, NULL);

    /* Next */
    lv_obj_t *btn_next = lv_btn_create(ctrl);
    lv_obj_set_size(btn_next, 80, 70);
    lv_obj_t *lbl_next = lv_label_create(btn_next);
    lv_label_set_text(lbl_next, ">|");
    lv_obj_center(lbl_next);
    lv_obj_add_event_cb(btn_next, btn_next_cb, LV_EVENT_CLICKED, NULL);

    /* Volume */
    lv_obj_t *vol_cont = lv_obj_create(scr);
    lv_obj_set_size(vol_cont, 400, 100);
    lv_obj_align(vol_cont, LV_ALIGN_CENTER, 0, 60);
    lv_obj_set_style_bg_color(vol_cont, lv_color_hex(0x1a1a2e), 0);
    lv_obj_set_style_radius(vol_cont, 12, 0);

    lv_obj_t *vol_lbl = lv_label_create(vol_cont);
    lv_label_set_text(vol_lbl, "Volume");
    lv_obj_set_style_text_color(vol_lbl, lv_color_hex(0x9CA3AF), 0);
    lv_obj_align(vol_lbl, LV_ALIGN_TOP_MID, 0, 12);

    lv_obj_t *vol_slider = lv_slider_create(vol_cont);
    lv_slider_set_range(vol_slider, 0, 100);
    lv_slider_set_value(vol_slider, g_volume, LV_ANIM_OFF);
    lv_obj_set_width(vol_slider, 350);
    lv_obj_align(vol_slider, LV_ALIGN_BOTTOM_MID, 0, -12);
    lv_obj_add_event_cb(vol_slider, vol_changed_cb, LV_EVENT_VALUE_CHANGED, NULL);
    lv_obj_move_foreground(vol_lbl);

    /* Station list */
    lv_obj_t *st_cont = lv_obj_create(scr);
    lv_obj_set_size(st_cont, 1024, 180);
    lv_obj_align(st_cont, LV_ALIGN_BOTTOM_MID, 0, 0);
    lv_obj_set_style_bg_color(st_cont, lv_color_hex(0x0F172A), 0);
    lv_obj_remove_flag(st_cont, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *st_lbl = lv_label_create(st_cont);
    lv_label_set_text(st_lbl, "Stations");
    lv_obj_set_style_text_color(st_lbl, lv_color_hex(0x9CA3AF), 0);
    lv_obj_align(st_lbl, LV_ALIGN_TOP_LEFT, 20, 10);

    for (int i = 0; i < num_stations; i++) {
        lv_obj_t *s_btn = lv_btn_create(st_cont);
        lv_obj_set_size(s_btn, 220, 50);
        lv_obj_set_pos(s_btn, 20 + (i % 4) * 240, 40 + (i / 4) * 60);
        lv_obj_set_style_bg_color(s_btn, lv_color_hex(0x1a1a2e), 0);
        lv_obj_t *s_lbl = lv_label_create(s_btn);
        lv_label_set_text(s_lbl, stations[i].name);
        lv_obj_set_style_text_color(s_lbl, lv_color_hex(0x38BDF8), 0);
        lv_obj_set_style_text_font(s_lbl, &lv_font_montserrat_14, 0);
        lv_obj_center(s_lbl);
        lv_obj_set_user_data(s_btn, (void *)(intptr_t)i);
        lv_obj_add_event_cb(s_btn, btn_station_cb, LV_EVENT_CLICKED, (void *)(intptr_t)i);
    }

    ESP_LOGI(TAG, "Radio UI created");
}

/* ============================================
 * LVGL Tasks
 * ============================================ */

static void lvgl_tick_task(void *arg)
{
    (void)arg;
    while (1) {
        lv_tick_inc(5);
        vTaskDelay(pdMS_TO_TICKS(5));
    }
}

static void lvgl_task(void *arg)
{
    (void)arg;
    while (1) {
        lv_timer_handler();
        vTaskDelay(pdMS_TO_TICKS(5));
    }
}

/* ============================================
 * LP WDT Feeder Task
 * Prevents LP WDT from firing during heavy WiFi traffic.
 * The LP WDT is serviced by the LP CPU, but the main CPU can also
 * feed it directly via registers to prevent timeouts during busy periods.
 * ============================================ */

static void lp_wdt_feed_task(void *arg)
{
    (void)arg;
    TickType_t last_wake = xTaskGetTickCount();
    uint32_t count = 0;
    while (1) {
        vTaskDelayUntil(&last_wake, pdMS_TO_TICKS(2000));
        lpwdt_ll_feed(&LP_WDT);
        ESP_LOGD(TAG, "LP WDT fed [%lu]", (unsigned long)++count);
    }
}

/* ============================================
 * Audio Control Task
 * Offloads blocking GMF calls (DNS resolve, socket open,
 * stream start) from the LVGL thread so lv_timer_handler()
 * never blocks. This prevents SDIO starvation and the resulting
 * C6 co-processor timeout that would reset the P4.
 * ============================================ */

static void audio_control_task(void *arg)
{
    (void)arg;
    while (1) {
        audio_req_t req = s_audio_req;
        if (req != AUDIO_REQ_NONE) {
            s_audio_req = AUDIO_REQ_NONE;

            if (req == AUDIO_REQ_PLAY) {
                ESP_LOGI(TAG, "AUDIO: starting playback");
                start_gmf_pipeline(stations[current_station].url);
                ui_is_playing = true;
                if (status_label) lv_label_set_text(status_label, "Streaming...");
            } else if (req == AUDIO_REQ_STOP) {
                ESP_LOGI(TAG, "AUDIO: stopping playback");
                stop_gmf_pipeline();
                ui_is_playing = false;
                if (status_label) lv_label_set_text(status_label, "Stopped");
            } else if (req == AUDIO_REQ_SWITCH_STATION) {
                ESP_LOGI(TAG, "AUDIO: switching to station %d", s_req_station_idx);
                start_gmf_pipeline(stations[s_req_station_idx].url);
            }
        }
        vTaskDelay(pdMS_TO_TICKS(50));
    }
}

/* ============================================
 * Main
 * ============================================ */

void app_main(void)
{
    ESP_LOGI(TAG, "=== Crowpanel Internet Radio ===");
    ESP_LOGI(TAG, "ESP32-P4 + ESP-IDF + ESP-GMF");
    ESP_LOGI(TAG, "IDF: %s", esp_get_idf_version());

    /* Increase task WDT timeout before init sequences.
     * Heavy init (display DMA, SDIO) can exceed the default 5s timeout. */
    esp_task_wdt_config_t wdt_config = {
        .timeout_ms = 15000,
        .idle_core_mask = BIT(0) | BIT(1),
    };
    esp_err_t wdt_err = esp_task_wdt_reconfigure(&wdt_config);
    if (wdt_err != ESP_OK) {
        ESP_LOGW(TAG, "esp_task_wdt_reconfigure failed: %s", esp_err_to_name(wdt_err));
    }

    /* Start LP WDT feeder to prevent LP WDT from firing during heavy init/WiFi.
     * Feeds the LP WDT every 2s from the main CPU as a backup to the LP CPU's own feeding. */
    xTaskCreatePinnedToCore(&lp_wdt_feed_task, "lp_wdt", 2048, NULL, 2, NULL, 0);

    ESP_ERROR_CHECK(nvs_flash_init());

    /* Brief settle delay to let the system stabilize and feed WDT */
    vTaskDelay(pdMS_TO_TICKS(50));

    /* ========================================
     * PHASE 1: Driver Init (Grab SDIO Memory)
     * Must be first to ensure contiguous DMA buffers
     * ======================================== */
    ESP_LOGI(TAG, "Phase 1: Driver init (SDIO memory)...");
    wifi_driver_init();

    /* ========================================
     * PHASE 2: UI Init (Turn on screen)
     * ======================================== */
    ESP_LOGI(TAG, "Phase 2: UI init...");

    /* LVGL must be initialized before display (for memory allocator) */
    lv_init();

    display_init();
    backlight_init();

    /* Touch */
    touch_init();

    /* Register touch as LVGL input device */
    lv_indev_t *touch_indev = lv_indev_create();
    lv_indev_set_type(touch_indev, LV_INDEV_TYPE_POINTER);
    lv_indev_set_read_cb(touch_indev, touch_read_cb);
    lv_indev_set_user_data(touch_indev, touch_handle);

    /* Now create UI now that display and LVGL are ready */
    lvgl_init();
    create_radio_ui();
    xTaskCreatePinnedToCore(&lvgl_tick_task, "lvgl_tick", 4096, NULL, 3, NULL, 1);
    xTaskCreatePinnedToCore(&lvgl_task, "lvgl", 8192, NULL, 3, NULL, 1);
    /* Async audio control — offloads blocking GMF calls from LVGL thread */
    xTaskCreatePinnedToCore(&audio_control_task, "audio_ctrl", 4096, NULL, 4, NULL, 1);

    /* Audio hardware */
    g_i2s_tx = audio_i2s_init();
    audio_amp_init();

    /* Create GMF pool and register custom I2S output */
    esp_gmf_pool_init(&g_pool);
    register_i2s_output(g_i2s_tx);

    /* ========================================
     * PHASE 3: Connect (Non-blocking)
     * UI updates async via wifi_event_handler
     * ======================================== */
    ESP_LOGI(TAG, "Phase 3: Starting WiFi connection...");
    wifi_connect();

    ESP_LOGI(TAG, "=== Radio ready! ===");
    ESP_LOGI(TAG, "Waiting for WiFi... Press PLAY when connected.");

    /* Don't auto-start streaming - wait for WiFi first */
    /* The user can press PLAY once WiFi is connected */

    while (1) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
