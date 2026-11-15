@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist "logs" mkdir "logs"

where uv >nul 2>nul
if errorlevel 1 (
  call :log "未找到 uv。请先安装：https://docs.astral.sh/uv/getting-started/installation/"
  pause
  exit /b 1
)

if not exist ".venv" (
  call :log "首次启动：创建环境并安装依赖，请稍候..."
  uv sync
  if errorlevel 1 (
    call :log "依赖安装失败，请检查网络。"
    pause
    exit /b 1
  )
)

uv run --quiet python -m yafip.cli %*
if errorlevel 1 (
  call :log "执行失败，详见 logs\start_yafip.log 与 logs\yafip.log"
  pause
  exit /b 1
)
exit /b 0

:log
echo [yafip] %~1
for /f "tokens=1,2" %%a in ("%date% %time%") do >>"logs\start_yafip.log" echo %%a %%b %~1
exit /b 0
