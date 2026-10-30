@echo off
rem 导出坏图启动器：双击直接运行，也可带参覆盖：
rem   export_bad_photos.bat -i <样本根目录> -o <坏图输出目录> --dry-run
rem 本文件与 export_bad_photos.py、rootbox_qc\ 放在同一目录即可独立使用。
chcp 65001 >nul
cd /d "%~dp0"

rem Python 解释器：优先环境变量 ROOTBOX_QC_PYTHON，其次 PATH 里的 python
if not defined ROOTBOX_QC_PYTHON set "ROOTBOX_QC_PYTHON=python"

"%ROOTBOX_QC_PYTHON%" export_bad_photos.py %*
echo.
pause
