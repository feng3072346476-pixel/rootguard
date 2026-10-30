@echo off
rem 根盒质检 · 监听服务启动器（上线形态）
rem 小车落图 → 自动质检 → 反馈；首次先用 --shadow 旁路观察。
rem 产线结构：<watch>\data1030\0001\1.png + 2.png → <save>\data1030\0001.png
rem 用法：run_service.bat --watch D:\qc\in --out D:\qc\out --stitch-dir D:\qc\save --health-port 8090 --shadow
rem 计划任务示例（开机自启）：
rem   schtasks /create /tn "RootboxQC" /sc onstart /ru SYSTEM /tr "\"%~f0\" --watch D:\qc\in --out D:\qc\out --stitch-dir D:\qc\save --health-port 8090"
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1

rem 按机器实际环境改这行；也可留 python（前提是已在 PATH 上）
set "PY=D:\Anaconda3\python.exe"
if not exist "%PY%" set "PY=python"

rem 生产必须配：拼接成品根目录（成品的父目录，下面按批次分）。不配会 fail-closed 拒绝启动。
if not defined ROOTBOX_QC_STITCH_DIR set "ROOTBOX_QC_STITCH_DIR=D:\qc\save"

"%PY%" run_service.py %*
exit /b %ERRORLEVEL%
