@echo off
REM v1.22u: unset env vars that the parent bash leaked
REM into this cmd.exe. Two known leaks:
REM 1. MSYS* vars: idf_tools.py refuses to run when it
REM    sees MSYSTEM=MINGW64.
REM 2. GIT_CONFIG_KEY_0=safe.bareRepository + GIT_CONFIG_VALUE_0=explicit
REM    forces git to refuse bare-repo use (which the IDF
REM    component manager needs to cache component git repos).
set MSYSTEM=
set MSYS=
set MSYS2_PATH=
set MSYS_NO_PATHCONV=
set MSYS2_ARG_CONV_EXCL=
set GIT_CONFIG_KEY_0=
set GIT_CONFIG_VALUE_0=
set GIT_CONFIG_COUNT=
cd /d "C:\ESPHome_Projects\Crowpanel_7.00_ESP32-P4"
esphome compile test_dynamic_component.yaml > testing\logs\build_v1.22u_bat.log 2>&1
echo exit=%ERRORLEVEL%
