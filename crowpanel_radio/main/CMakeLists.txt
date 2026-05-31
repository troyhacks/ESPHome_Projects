idf_component_register(
    SRCS "main.c"
    INCLUDE_DIRS "."
    REQUIRES
        esp_lcd_ek79007
        esp_lcd_touch_gt911
        lvgl
        gmf_core
        gmf_io
        gmf_audio
        gmf_misc
        gmf_loader
        gmf_i2s_out
        esp_wifi
        nvs_flash
        esp_hosted
        esp_wifi_remote
)
