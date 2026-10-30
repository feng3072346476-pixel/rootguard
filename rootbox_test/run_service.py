"""根盒质检 · 目录监听服务入口（上线形态）。

小车把一对图落到监听目录，本服务自动配对 → verify_pair（单图 → 成对 → 拼接 → 成品复检）
→ 把结论写到反馈目录（可选 HTTP 回调）。

启动（先 shadow 旁路观察，确认无误再切 live）：

    set ROOTBOX_QC_STITCH_DIR=D:\\yy1\\qc\\stitch
    python run_service.py --watch D:\\yy1\\qc_in --out D:\\yy1\\qc_out --health-port 8090 --shadow

切 live 去掉 --shadow。详细约定见 README「用法四：目录监听服务」。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rootbox_qc.service import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
