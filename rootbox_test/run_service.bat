@echo off
rem 根盒质检 · 监听服务启动器（上线形态）
rem 小车落图 → 自动质检 → 反馈；首次先用 --shadow 旁路观察。
rem 用法：run_service.bat --watch D:\yy1\qc_in --out D:\yy1\qc_out --health-port 8090 --shadow
rem 计划任务示例（开机自启）：
rem   schtasks /create /tn "RootboxQC" /sc onstart /ru SYSTEM /tr "\"%~f0\" --watch D:\yy1\qc_in --out D:\yy1\qc_out --health-port 8090"
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1

rem 按机器实际环境改这行；也可留 python（前提是已在 PATH 上）
set "PY=D:\Anaconda3\python.exe"
if not exist "%PY%" set "PY=python"

rem 生产必须配：拼接输出目录（不配会 fail-closed 拒绝启动）
if not defined ROOTBOX_QC_STITCH_DIR set "ROOTBOX_QC_STITCH_DIR=D:\yy1\qc\stitch"

"%PY%" run_service.py %*
exit /b %ERRORLEVEL%
