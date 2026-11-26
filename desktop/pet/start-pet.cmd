@echo off
rem 启动蛋仔桌宠（复用 desktop/electron 的 Electron 二进制，零额外安装）。
setlocal
rem DSH/某些终端会设置 ELECTRON_RUN_AS_NODE=1，会让 electron 退化成纯 node，必须清除。
set "ELECTRON_RUN_AS_NODE="
set "ELECTRON=%~dp0..\electron\node_modules\electron\dist\electron.exe"
if not exist "%ELECTRON%" (
  echo [eggy] Electron binary not found: "%ELECTRON%"
  echo [eggy] Run "npm install" in desktop\electron first.
  exit /b 1
)
start "" "%ELECTRON%" "%~dp0." --enable-logging
endlocal