@echo off
set MSYSTEM=
set MSYS=
set MSYS2_PATH=
set MSYS_NO_PATHCONV=
set MSYS2_ARG_CONV_EXCL=
set GIT_CONFIG_KEY_0=
set GIT_CONFIG_VALUE_0=
set GIT_CONFIG_COUNT=
cd /d "C:\ESPHome_Projects\Crowpanel_7.00_ESP32-P4"
esphome upload test_dynamic_component.yaml --device COM47 > testing\logs\upload_v1.22u_bat.log 2>&1
echo exit=%ERRORLEVEL%
