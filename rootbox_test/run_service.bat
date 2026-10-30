@echo off
rem 根盒质检 · 监听服务启动器（上线形态）
rem 小车落图 → 自动质检 → 反馈；首次先用 --shadow 旁路观察。
rem 产线结构：<落图目录>\<批次>\<编号>\1.png + 2.png → <成品目录>\<批次>\<编号>.png
rem
rem 本文件不含任何机器相关的绝对路径。目标机配置有两种办法（可混用）：
rem   1) 复制 service_env.example.bat 为 service_env.bat，填好路径（推荐，本文件不用改）
rem   2) 启动时用命令行传：run_service.bat --watch <落图目录> --out <输出目录> --stitch-dir <成品目录>
rem 计划任务示例（开机自启，路径按目标机填）：
rem   schtasks /create /tn "RootboxQC" /sc onstart /ru SYSTEM /tr "\"%~f0\" --watch <落图目录> --out <输出目录> --stitch-dir <成品目录> --health-port 8090"
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1

rem 现场配置（不进仓库）：如果同目录有 service_env.bat 就先加载
if exist "%~dp0service_env.bat" call "%~dp0service_env.bat"

rem Python 解释器：优先环境变量 ROOTBOX_QC_PYTHON，其次 PATH 里的 python
if not defined ROOTBOX_QC_PYTHON set "ROOTBOX_QC_PYTHON=python"

rem 生产必须配：落图目录 + 拼接成品根目录。缺任一项，服务启动自检会 fail-closed 退出（退出码 3）并说明原因。
if not defined ROOTBOX_QC_WATCH_DIR echo [提示] 未设置 ROOTBOX_QC_WATCH_DIR，请用 --watch 指定落图目录，或在 service_env.bat 里配置
if not defined ROOTBOX_QC_STITCH_DIR echo [提示] 未设置 ROOTBOX_QC_STITCH_DIR，请用 --stitch-dir 指定成品根目录，或在 service_env.bat 里配置

"%ROOTBOX_QC_PYTHON%" run_service.py %*
exit /b %ERRORLEVEL%
