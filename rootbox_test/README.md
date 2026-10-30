# 根盒图像质检（rootbox_qc）— 生产版

夜间根盒拍摄图像的自动质量检查：拍摄后立即校验，不合格触发重拍。
检测 5 类问题：**损坏、模糊、拍摄不全、错位、重复拍摄**（错位含单张偏移与两张
灯照间错位；重复拍摄=相机未移动/文件被重复拷贝，两张灯照完全相同，独立类别）；
另内置兼容的**倾斜**检查（`tilt_check_enabled` 开关，默认关闭——bigbox 线相机固定、
根盒不移动，物理上无触发条件，仅相机可移动/箱体人工摆放的现场启用）。

## 2026-10-29 现行配置（先读）

- **产线刀口 C = 2200 / 2780 / 3300**（原图 px @5472 宽），已写入
  `config.py:THRESHOLDS["blur_strip_cuts"]`；`layout=None` 默认走 C。
  旧批次用 `layout="A"`（2000/2700/3300）或 `layout="B"`（1950/2500/3050）
  显式切换**整套**刀口（模糊存活列 + 单张缝位 + 固定缝窗一起变），不要只改
  `blur_strip_cuts`。`layout` 只认 A/B/C，不做按文件名自动路由。
- **三态**：报告新增 `status = pass | fail | undetermined`。判不了（分辨率不符、
  刀口/画幅不匹配、角色不明、长边结构不可用）记 `undetermined`，仍置
  `recapture_needed=True`；到 `max_attempts` 仍判不了则 `escalate_manual=True`。
  `passed` 仅表示 `status=="pass"`，消费方不要再把 `passed=False` 一律当确定缺陷。
- **线上成对必须 raw**：`verify_pair(path_a, path_b, ...)` 只要给了 `path_b`，
  内部强制 `input_kind="raw"`；`verify_single` 线上也应显式传
  `input_kind="raw"`。`auto` 只用于离线混合目录，且单图猜出的模态不会被
  成对阶段复用（`input_kind` 不一致会重算）。
- **分辨率守卫**：默认只接受 5472×3648（竖拍自动旋转后同尺寸）。其他尺寸不缩放
  刀口、不猜模态，直接 `undetermined` + 重拍。换相机/换分辨率必须同时改
  `expected_width/expected_height` 并重新标定刀口。
- **竖拍修复**：`load_image` 旋转后，损坏检查的"解码尺寸 vs 文件头"核对接受
  转置尺寸，不再把竖拍原图误报"疑似文件截断"。
- **生产流程**：两张单图各自检查 -> 任一不过立即返回（不拼接）-> 两张都过后
  成对比对 -> 调 `paths.stitch_script` 拼接 -> 对成品按 `mosaic` 再跑
  损坏/模糊/不全/单张错位。生产**必须**设 `ROOTBOX_QC_STITCH_DIR`（或
  `cfg["paths"]["stitch_output_dir"]`），否则默认只完成前两步，
  `stitched_status=None`，不是完整流程。
- **重拍后报告**：同一 pair 的多次 attempt 交给同一个 `RecaptureTracker.observe()`，
  报告新增 `retry` 块（重拍次数、首末状态、首拍原因、每次尝试明细）；
  `FileNotifier` 在 `attempt>1` 或转人工时写 `<pair_id>_retry.json`。
- **不全判据（2026-10-29 改为上下对称 + 成品顶部覆盖，按 066 蓝框）**：
  上、下两条金属长边都要在画面内。上 30% 内最强水平边挺直度 `<0.22`、或
  位置 `>0.20` 判不全（顶部没拍到/被推离）；下 30% 内最强水平边位置
  `<0.868` 判不全（底梁偏上/被推离）；底部挺直度 `<0.35` 且目标底梁带
  `[0.85,0.96]` 强度 `<0.28` 判底部结构缺失。066 类“上侧正常、底部暗但
  仍有弱结构”记 `dark_bottom_exempt` 放行。
- **成品图顶部覆盖（只对 mosaic 生效）**：顶部土壤/板面起点 `top_cover_y`
  `<0.072` 判不全，语义是顶部金属横梁过窄/被裁（727=0.0616；正常
  508=0.0792、782=0.0821、113=0.1437、389=0.1378）。原图 raw 不套用；
  旧“右侧金属外框”判据（727=0.293、113=0.066）已被用户否定并 revert，不要恢复。
- **模糊存活列阈值 48.5 + 成品图纹理门**：存活列 `alive var < 48.5` 且
  灯管像素占比 `<0.005` 时判据不适用、跳过；有灯管反光按 48.5 判。阈值取在
  用户确认偏糊的 092/1（47.8）、485/1（48.3）之上，正常 915/1（49.3~49.5）
  之下。成品平图（`input_kind="mosaic"`）另加 6×8 分块 Laplacian 方差 p10
  `<40` 判模糊，覆盖没开灯管、单图锐度抓不到的 013（21.5）/019（25.2）；
  正常平图最低 57.9（868）。夜里没开灯的 7 张正常图
  （113/124/142/369/389/724/727）仍走 LED 门放行。
- **dataed in-sample 复核**（251 张：正常 145 / 不全 106；052/1 已由用户
  确认不全，052/2 同批同模式一并按不全计，727 已由用户确认不全）：
  `check_completeness` 为正常 145/145 pass、不全 106/106 fail；完整
  `verify_single` 为正常 144/145（唯一 fail 是 066，既有 `alignment_single`
  判错位，用户已认可）、不全 106/106。113/389 是正常图，现已 pass。
  **这是 in-sample 标定，不是盲测**；换批次必须用新样本重跑。阈值余量很窄：
  目标底梁带 `0.28` 与不全 301/2（0.275）、正常 099（0.294）只差
  0.003~0.014；顶部覆盖 `0.072` 与正常 508（0.0792）只差 0.0072；
  052/2 的标签仍需现场确认。

> 原代码备份：`_backup_20260904/`（未动，可对照/回滚）
> 旧版改造说明：修复 LED 检测失效、接线未生效阈值、新增边框角点检查、
> 错位改用特征匹配、新增线上单张接口、HTTP 通知器标准库实现。

## 项目结构

```
rootbox_test/
├── main.py                # 离线批量入口（ROOTBOX_QC_INPUT_DIR 或 config.paths.input_dir）
├── run_service.py         # 线上监听服务入口（落图目录 → 质检 → 反馈）
├── run_service.bat        # 其双击/计划任务启动器（Windows）
├── rootbox_qc/            # 代码包
│   ├── __init__.py        # 对外 API
│   ├── config.py          # 全部配置：路径 + 阈值 + 配对规则 + 通知方式
│   ├── runner.py          # 离线：扫描 + 配对 + 检测 + 终端汇总
│   ├── verify.py          # 线上：verify_single 单图 / verify_pair 单图→成对→拼接→成品复检
│   ├── service.py         # 线上监听壳：目录轮询 + 启动自检 + 反馈 + 健康/指标 + shadow
│   ├── checks.py          # 5 类检测实现（+ 可开关的倾斜）
│   ├── frame.py           # 特征检测：亮金属结构 + LED 光带（自适应阈值）
│   └── notifier.py        # 通知：file / console / http / multi
├── scripts/
│   ├── smoke_test.py          # 合成图自检（改完代码先跑它）
│   ├── opt_test.py            # 五类信号/架构专项验证（合成图）
│   ├── regression_5472.py     # 真实画幅回归：分辨率守卫/竖拍旋转/C 刀口
│   ├── service_test.py        # 监听服务回归：真图 × 真拼接脚本 × 真 HTTP
│   ├── export_bad_photos.py   # 按问题类型导出坏图（原图+拼接图）
│   └── export_bad_photos.bat  # 其双击启动器（Windows，独立部署用）
└── results/               # 报告 + 重拍队列 + 日志
```

依赖（已锁定投产实测版本，见 `requirements.txt`）：

```bash
pip install -r requirements.txt
```

## 用法一：离线批量（人工审片）

```python
# 设置 ROOTBOX_QC_INPUT_DIR，或改 config.py 的 paths.input_dir；或：
from rootbox_qc import run

reports = run(r"D:\yy1")                              # 终端打印 + results/ 报告 + 重拍队列
reports = run(r"D:\yy1", thresholds={"tilt_angle_max_deg": 5.0})   # 临时覆盖阈值
reports = run(r"D:\yy1", write_files=False)           # 只看终端
```

支持目录结构（自动识别）：平铺 `n_1.jpg+n_2.jpg` / 单层 `001\1.png+2.png` /
双层 `类别\001\1.png+2.png` / 根目录无图自动找含图子目录。

> 离线批量默认只做单图+成对检查。若环境已设 `ROOTBOX_QC_STITCH_DIR`，每个通过的
> 对都会自动拼接并复检（更慢、占磁盘）；只做人工审片时传 `stitch_output_dir=""`
> 明确关闭。

## 用法二：线上（拍摄即检，生产用法）

机器拍完一张立即校验，不合格当场重拍该张；两张都过才拼接，拼接成品再检：

```python
import os
os.environ["ROOTBOX_QC_STITCH_DIR"] = r"D:\data\qc_stitched"       # 生产必须
os.environ["ROOTBOX_QC_STITCH_SCRIPT"] = r"d:\tupianpj\图片拼接2.py"

from rootbox_qc import verify_single, verify_pair
from rootbox_qc.config import load_config

cfg = load_config()          # 或用 run 时同一套配置
rep_a = verify_single(r"D:\data\001\1.png", cfg, attempt=1, input_kind="raw")
if rep_a.recapture_needed:   # fail 和 undetermined 都为 True
    notify_robot_to_retake(rep_a.recapture_targets, rep_a.recapture_reasons)
    # 重拍后 attempt+1 再检；达到 max_attempts(3) 转人工
    # rep_a.status == "undetermined" 时，告警文案应说明"判不了/需人工确认"，不要当作确定缺陷
else:
    rep_b = verify_single(r"D:\data\001\2.png", cfg, attempt=1, input_kind="raw")
    if rep_b.recapture_needed:
        notify_robot_to_retake(rep_b.recapture_targets, rep_b.recapture_reasons)
    else:
        rep = verify_pair(r"D:\data\001\1.png", r"D:\data\001\2.png", cfg,
                          attempt=1, input_kind="raw", res_a=rep_a, res_b=rep_b)
        if rep.recapture_needed:
            notify_robot_to_retake(rep.recapture_targets, rep.recapture_reasons)
        else:
            # 两张单图、成对、拼接成品都通过；未配 stitch_output_dir 时这里会是 None
            if rep.stitched_status != "pass":
                raise RuntimeError("拼接成品未通过复检，禁止继续拍摄")
            continue_next_shot()
```

`QCReport.status`：`pass` / `fail` / `undetermined`。JSON 报告同时保留
`recapture_needed`、`reasons`、`targets` 兼容字段，并新增 `status`。
成对报告另有 `stitched_status`（`pass` / `fail` / `undetermined` / `None`）、
`stitched_checks`、`stitch_error`、`stitched_path`。所有报告还含 `retry`
重拍汇总（见下）。

触发重拍的出口：`notifier.py` 的 HttpNotifier（POST JSON 到小车 REST，标准库实现，
失败重试但不影响判检）或轮询 `results/recapture_queue.jsonl`（每行一条，
字段：pair_id / reasons / targets / attempt / timestamp，超 5MB 自动轮转）。

### 重拍后报告（retry）

同一个 pair 每次重拍后，把 `QCReport` 交给同一个 `RecaptureTracker` 登记；
报告里的 `retry` 块会保留首拍原因、每次尝试的状态和最终结果：

```python
from rootbox_qc.verify import RecaptureTracker

tracker = RecaptureTracker(max_attempts=3)   # 同 pair 多轮必须复用同一个 tracker
pair_id = "001"                               # 同一物理根盒/批次，重拍前后保持不变
attempt = tracker.next_attempt(pair_id)       # 首次为 1；重拍后为 2、3...
rep = verify_single(path, cfg, pair_id=pair_id, attempt=attempt, input_kind="raw")
tracker.observe(rep)
print(rep.retry_report())                     # 也可直接读 rep.to_dict()["retry"]
```

`retry` 字段：

| 字段 | 含义 |
|------|------|
| `retake_count` | 重拍次数 = `attempt - 1`（首拍为 0） |
| `attempts_total` / `max_attempts` | 已记录次数 / 熔断上限 |
| `first_status` / `final_status` | 首个已记录状态 / 本次最终状态（`history_complete=true` 时首个即首拍） |
| `resolved` / `retake_succeeded` | 最终是否通过 / 是否“重拍后通过” |
| `escalated` | 是否已达上限转人工/停机 |
| `first_reasons` / `final_reasons` | 首拍原因 / 本次原因 |
| `failure_categories` | 历次命中的故障类别（损坏/模糊/不全/错位/重复拍摄/其它） |
| `attempts[]` | 每次尝试的 attempt、scope(single/pair)、status、reasons、targets、categories、images、stitched_status、timestamp |
| `history_complete` | 是否从 attempt=1 连续记录；未复用 tracker 或从磁盘恢复时可能为 false |
| `systemic_alert` | 连续同类故障的系统性告警（若有） |
| `first_timestamp` / `final_timestamp` | 首拍 / 本次时间 |

文件出口（`FileNotifier`）：每次仍写 `<pair_id>.json`；`attempt>1` 另写
`<pair_id>_attempt<N>.json`；`attempt>1` 或已转人工时再写
`<pair_id>_retry.json`（上述 retry 汇总 + `final_report` 指向最终报告文件）。
`retry` 块在每次报告里都存在，批量只跑一次时表现为 `retake_count=0`、
`attempts_total=1`。

> 注意：`attempt` 必须由调用方在重拍后递增；同一个 tracker 要跨轮复用，
> 否则新 tracker 没有首拍历史，`history_complete` 会是 false。
> `pair_id` 不要用默认的 `1` / `2` 文件主干充当重拍 ID（会把 A/B 单图历史混在一起）；
> 同一物理根盒用同一个 ID。若 A、B 两张单图各自独立重拍，用 `<pair_id>_a` /
> `<pair_id>_b` 分开登记，成对复检再用 `<pair_id>`。

```python
NOTIFIER = {"type": "http", "url": "http://<小车IP>:8080/qc/recapture", "timeout": 5, "retries": 2}
# 或 file 留档 + http 触发同时：
NOTIFIER = {"type": "multi", "targets": [
    {"type": "file"},
    {"type": "http", "url": "http://<小车IP>:8080/qc/recapture"},
]}
```

## 检测信号一览

| 类型 | 信号 | 默认阈值（config.py） |
|------|------|-----------|
| 损坏 | 文件大小 / 文件头魔数 / 解码失败 / 近纯色 / 解码尺寸与文件头核对（截断） | `<100KB`；体积归一 `≥5000B/MP`；`gray_std≥15.0`（熵指标只记录不判定） |
| 模糊 | 亮区 ROI 拉普拉斯方差（单张兜底）＋ 拼接存活列 Laplacian 方差（`blur_strip_alive_var_min`，角色感知主判据，现行 C 刀口 2200/2780/3300）＋ **存活列 LED 门**（`blur_strip_alive_led_ratio_min`：无灯管反光时存活列方差不可用）＋ **成品图整体纹理**（`blur_mosaic_tile_lap_p10_min`，仅 `mosaic`）＋ **成对锐度不对称**（`blur_pair_*`，2026-09-24 新增，抓单张阈值抓不到的"一张拖影一张正常"）＋ LED 边缘弥散宽度（`blur_edge_check_enabled`，停用） | 存活列 var `≥48.5`；LED 占比 `<0.005` 且 var `<48.5` → 跳过存活列判据；成品图 6×8 分块 Laplacian p10 `≥40`；成对：糊图亮区 var `≤25.5` 且两张比值 `≥2.2`；单张 ROI var `≥20`；边缘 10~90% 宽度 `≤12px`（启用时生效）；刀口与画幅不匹配或角色不明 → `undetermined` |
| 倾斜 | 亮金属外接矩形角度（亮区占比异常时跳过判定；`tilt_check_enabled` 开关） | `≤8.714°`（bigbox 线相机固定、根盒不移动，物理上无触发条件，已关闭；仅相机可移动/箱体人工摆放现场启用） |
| 不全 | **上下对称：上/下两条金属长边都要在画面内**（上 30% 最强水平边的挺直度+位置；下 30% 最强水平边位置；目标底梁带强度；066 类暗底豁免）＋ **成品图顶部覆盖**（mosaic 专用：顶部土壤/板面起点 top_cover_y）/ 亮像素占比 / 凸包面积 / 外接矩形角点越界（可开关）/ 边框归一化位置 frame_bbox（始终记录；配 completeness_expected_bounds 后按机位基准判定） | 上侧：挺直度 <0.22 或位置 >0.20 → 不全；下侧：底梁位置 <0.868 → 不全；底部：bot_zone_max<0.35 且底梁带 <0.28 → 不全；成品顶部覆盖：top_cover_y<0.072 → 不全（仅 mosaic）；占比 ≤30%；角距边 ≥2%；机位基准偏离 ≤2% |
| 错位 | **区域带相位位移**（`alignment_band_*`，2026-09-24 新增主判据：上/中/下三条带各跑相位相关取 \|dy\| 中位，只测箱体内容不被固定背景稀释）＋ 两图 ORB 特征匹配（平移/旋转/缩放/内点率）＋ 相位相关独立印证（防 RANSAC 退化解）；亮区质心兜底 | 区域带 `≤15px`（2026-09-24 由用户裁定从 40 降到 35；2026-09-29 再裁定降到 15——dataed 全量回归 430 单元 417 无变化、13 单元新增错位命中 15.1~28.4px，均已落地）；位移 `≤400px`；旋转 `≤2°`；缩放差 `≤5%`；内点率 `≥10%`；特征/相位位移差 `≤150px`；配 `alignment_expected_shift_px` 后按"偏离设计走位"判定 |
| 重复拍摄 | 文件字节完全相同（零误报强信号）/ **低频照明差**（主信号，"两次拍摄照明状态相同"的定义式，缩 200px 宽＋高斯模糊后作差，对箱体微移的边缘错位免疫）/ 特征内点率＋位移；任一命中即判 | 低频照明差 `≤10`（全池标定：同光照 ≤4.5、不同光照 ≥19.1）；内点率 `≥95%` 且位移 `≤5px`；独立类别，2026-09-09 从错位拆出，2026-09-24 主信号由原始 MAE 换为低频照明差 |

> 阈值说明：0.0571→0.30 与角点检查开关为 2026-09-07 现场重标定（`F:\2026_maize_root_ertao`，
> 白天反光亮区）；`D:\yy1` 夜间环境的旧值见 config.py 注释。换现场务必重新标定。
> 2026-09-08 bigbox 线基于已导出命中样本重算 + 视觉抽检，更新了模糊列宽（LED 固有宽度
> 23~60px→阈值 75；ROI 方差 30→15，正常暗画幅 16~30）、不全区段（夜拍金属不发亮、
> 凸包无区分度→禁用）、倾斜整类（34 命中行角度 9~43° 全部为亮斑/反光散布假象，
> 抽检 10 张箱体均水平；且本线相机固定、根盒不移动，倾斜物理上无法发生、
> 相机撞歪由错位检查兜底→`tilt_check_enabled=False`）、错位位移/内点率阈值
> （两次拍摄设计走位 145~198px、正常对内点率低至 0.12）；详见 config.py 内联注释。
> 2026-09-09 复核模糊终点：连续放宽检测上限 60→400 全量重测 + 视觉抽检，发现
> LED 固有宽度为双峰（40~60px 主流 + 90~118px 大 LED；2026-09-08 的"76~80px 命中带"
> 是被 80 上限截断/金属排除的固有宽度，23 个命中样本均为误报），而 130~400px 的全部是
> 金属边框连通域/反光带/机架亮度（检测候选被污染，与失焦无区分度）→ LED 宽度信号
> 改开关默认停用（`blur_led_check_enabled=False`），模糊判定由 ROI 方差独担。

## 拼接后复检（生产流程，2026-10-29）

`verify_pair(path_a, path_b)` 的执行顺序：

1. A、B 各自跑单图检查（损坏 → 分辨率守卫 → 模糊 → 不全 → 单张错位）。
2. 任一 `ImageResult.status != "pass"`：立即汇总返回；不做成对错位/重复/锐度
   比对，也不拼接。
3. 两张都过：成对错位、重复拍摄、锐度不对称、照度一致性检查。
4. 成对通过且配置了 `stitch_output_dir`：把内存中的 A/B 送入
   `stitch_from_qc(img_a, img_b, out_path)`，成品写 `<pair_id>.png`。
5. 成品按 `input_kind="mosaic"` 重跑损坏/模糊/不全/单张错位，结果写入
   `stitched_status` / `stitched_checks` / `stitched_path`。

失败语义：

- 成品 `fail`/`undetermined`：整对状态按 `fail > undetermined > pass` 归并，
  `recapture_needed=True`，`reasons` 带 `[拼接图]` 前缀，
  `recapture_targets` 指向两张原图（重拍两张再拼）。
- 成品检查进 `failed_checks` 时带 `scope="stitched"`、`paths=[]`，避免
  `export_bad_photos.py` 把原图重复登记成“拼接图失败”。
- 拼接脚本缺失/执行异常：记入 `stitch_error`；要求复检时
  `stitched_status="undetermined"` 并阻塞，不会静默放行。
- `verify_stitched=False`：保持旧行为，只暴露错误、不改变质检结论；离线只做
  单图/成对时可显式关闭。
- 单张失败或成对失败时不会拼接，也不会追加“未配置 stitch_output_dir”的伪原因。

配置（生产必须）：

```python
cfg = load_config()
cfg["paths"]["stitch_script"] = r"d:\tupianpj\图片拼接2.py"   # 需提供 stitch_from_qc
cfg["paths"]["stitch_output_dir"] = r"D:\data\qc_stitched"
```

也可在 import 前设 `ROOTBOX_QC_STITCH_DIR` / `ROOTBOX_QC_STITCH_SCRIPT`。
目录会自动创建，每个通过的对写一个 `<pair_id>.png`。`verify_pair` 默认
`verify_stitched=None`：只有配置了目录才复检；`path_b=None` 的单张不拼接
（`stitched_status=None`），离线扫散图不会因此被判失败。`run()` 未配置时启动会
warning，提示当前只做了单图+成对检查。

## 关于"模糊"（2026-10-30 统一角度复核 A/C）

本线"模糊"的定义是**机器没停稳**：拼接图一部分模糊一部分清晰；原图对可能一张模糊
一张清晰，也可能两张都模糊。成品图由两张原图按固定竖向刀口交叉取片拼成，**只有每张
原图"存活"的列进入成品**。刀口默认 **C = 2200/2780/3300**（`layout=None`）；
旧批次用 `layout="A"`（2000/2700/3300）或 `"B"`（1950/2500/3050）显式切换，
模糊存活列、单张缝位、固定缝窗会一起切换，不要只改 `blur_strip_cuts`。

**现行判据（`blur_strip_check_enabled=True`）**

- 角色感知存活列方差：1.png 取 stripB（`[c1,c2) ∪ [c3,W)`），2.png 取 stripA
  （`[0,c1) ∪ [c2,c3)`）；角色由文件名 1.x/2.x 判定，散装单图用条带亮度比指纹。
- 阈值 `blur_strip_alive_var_min=48.5`、`blur_strip_surviving_var_min=26.5`。
  48.5 取在用户确认偏糊的 092/1（47.8）、485/1（48.3）之上，正常 915/1
  （49.3/49.5）之下，余量只有 0.8~1.0。
- **2026-10-30 统一角度复核（dataed 正常 148 + 模糊 37 对）**：A（2000/2700/3300）
  与 C（2200/2780/3300）状态一致。按用户 m03107 口径，092/1 本身有点糊，
  算命中而非误报，故正常 147/148 pass；模糊 37 对中 1.png 33/37 命中、
  2.png 37/37 pass（2 号是清晰取片侧，符合设计）。剩余 4 张单图漏检
  （267/299/522/888 的 1.png alive 49~56、ROI 22~26）全部由 `blur_pair`
  抓到，成对 37/37 重拍。默认继续用 C。
- 成品图 013/019 没有灯管反光，LED 门本会跳过存活列；现由 6×8 分块
  Laplacian p10 判糊：013=21.5、019=25.2 < 40。正常平图最低 57.9
  （868；566=59.9），余量 17.9。
- 485/1（alive 48.3、ROI 26.2）此前单图与成对都放过，现由 48.5 在单图阶段命中。
- 历史 A 刀口实测：纯模糊 8/8 检出、正常 0/50 误报；ROI var 20 兜底散装成品；
  散装成品 010/025/027/031/032/2426 是已知缺口。
- **判不了不再静默 pass**：刀口与画幅不匹配、无法判定 1/2 号角色时，
  记 `status=undetermined`，按需重拍处理；到 `max_attempts` 仍判不了则转人工。

## 关于"错位"（2026-09-22 真值集重标定：配对 3/4 检出、0/16 误报；单张改用拼接缝落差 52/67 检出、0/34 误报）

用 `data/错位` 的 3 组标注原图对（1607/1873/2531）+ 8 组真实非错位原图对
（`data/模糊` 007~014，其 alignment 全部通过）独立复核了错位检测：

| 度量 | 非错位对照（8 对） | 错位标注（3 对） | 阈值 | 结论 |
|------|----------------|---------------|------|------|
| 旋转差 θ（ORB+RANSAC） | -0.32° ~ +0.33° | 2.58° / 2.95° / 9.99° | 2.0° | **主信号，7 倍裕度，3/3 检出、0/8 误报** |
| 特征位移 | 150~195px | 294 / 305 / 1689px | 400px | 只抓 1689px 级；294/305 故意交给 θ |
| 亮区质心距离 | 1397~1586px | 1090 / 1114 / 1792px | — | **反向**：错位组反而更低，质心被光照差异主导，无区分度（仅特征不可用时兜底，本例未触发） |

位移上限 400 是刻意放松的（400 只抓 1689px 级极端错位），294/305px 这类
"超出带宽但幅度不大"的错位由 θ 判据捕获——与 config.py 的标定注释一致。
注：09-08 记录的"设计走位 145~198px"实为模糊对照 007~014 的退化匹配位移
（真正常对实测 6~25px），已更正；400px 上限保留的依据是真值集 0/16 误报，
不是设计走位假设。

**2026-09-22 用新真值集 `D:\pic\data` 重标定（排除 total 未整理图）**：
全量实测错位 26 原图对 + 67 散装单图、正常 16 原图对 + 34 散装单图。

- **多缺陷并存（非污染，2026-09-22 更正）**：`错位/` 的 015~038 共 22 组
  原图对与 `不全/`、`模糊/` 目录**文件完全相同**（MD5 逐张核对），另有
  277/278 两张散图与 `不全/` 重复——这些样本同时具备不全+模糊+错位三种
  缺陷，是**真实的错位样本**，不是数据污染。但其成对位移 126~198px 与
  "仅模糊"对照 007~014（150~195px）完全重叠（根因：弱纹理/模糊图 ORB
  匹配退化，位移估计被抬高），位移判据无法分离 -> 配对标定仍在 4 组干净
  原图对（101/1607/1873/2531）上进行；这 22 组中仅 4 张有对应拼接成品，
  其中只有 027 的错位体现为缝落差 132~156px，其余为水平分量，本数据集内
  无自动判据稳定捕获，留待人工复核。
- **配对判据**：3/4 检出、0/16 误报。1607（1689px + 9.99°）、1873（294px
  + 2.58°）、2531（305px - 2.95°）全部命中，θ 仍是主信号；正常对位移
  6~25px、θ -0.24°~+0.13°，与错位下限 294px 之间间隔干净。阈值维持不变。
- **唯一漏检 101**：位移 41.6px、θ -0.38°、内点率 0.54、列剖面相关 0.0433，
  全部落在正常对分布内（正常对 colcor 0.0419~0.1241；质心位移两类均
  1000~1800px 完全重叠）。属于轻度错位，位置/旋转/内容一致性判据物理上
  无法分离，且与"轻度错位可容忍"的现场标准不冲突 -> 不为此放宽阈值。
- **单张错位判据改用"拼接缝两侧垂直落差"并启用**（`alignment_single_check_enabled=True`，
  `alignment_seam_vshift_max=20px`）：成品图 = 两张灯照左右拼接，分界固定在
  x≈1999/2000（5472px 宽，101/101 张命中）；取缝两侧窄带的行边缘强度剖面做
  平移相关，落差即错位的直接物理量：

  | 度量 | 正常 34 散图 | 错位 67 散图 | 阈值 | 结论 |
  |------|------------|------------|------|------|
  | 缝两侧垂直落差 \|vshift\| | 0~16px（med 0） | med 40 / p75 54 / max 152px | 20px | **52/67 检出、0/34 误报**（16px 可检出 55/67 但裕度为 0，不取） |

  非拼接图（单张灯照原图）检不到缝，自动跳过、不误报。旧判据"亮区质心 vs
  几何中心"已弃用：旧实现取 `frame["center"]`（= corners 均值）与几何中心
  恒等、offset_ratio 恒 0；修复后错位 med 0.485 / max 36.8 仍低于正常图
  med 0.492 / max 110.0（暗画幅散图质心被零星反光点主导）。局限：只覆盖
  垂直落差分量，水平错位仍需配对判据。
与 config.py 的标定注释一致；**配对阈值维持不变，单张判据按上条启用**。
唯一覆盖缺口：若未来出现"旋转 <2° 且位移 250~400px"的错位会漏判（对照实测
位移上限 195px，与错位下限 294px 之间有干净间隔，需要时可把
`alignment_max_shift_px` 降到 ~250 补上第二信号，前提是在更多正常对上确认）。

**复核中暴露的另一个问题（已于 2026-10-29 重标，见下文“拍摄不全”）：** 当时
长边位置阈值是在**成品拼接图**上标定的（正常图 y_top 0.000~0.044 / y_bot
0.933~0.997），而线上批量模式跑**原图**，原图的长边归一化位置系统性偏低
（实测对照原图 y_bot 0.912~0.944），导致 8/8 对照原图被"不全"误判（007
y_bot=0.912、014 y_bot=0.915 临界）。此外暗画幅图的长边检测会锁到画面中部
的低对比亮带（010/1 y_top=0.739、拼接暗图 013 y_top=0.801）。**这是当时的
结论；2026-10-29 已改为上下对称判据（顶梁存在/贴顶 + 底梁位置/强度），并在
dataed 正常/不全 251 张上重新复核；旧字段 0.396 作废。**

## 关于"拍摄不全"（2026-10-29 改为上下对称 + 成品顶部覆盖）

本线"不全"的定义（用户 m01123 / m01964 / m02293 / m02821）：桌面 066.png 的蓝框
代表合格图片应包含的内容，目标根盒允许轻微偏移（066 本身可接受）；**上侧金属长边
被推离画面顶部、或下侧金属长边被推离画面底部/明显偏上，任一边超过太多都判不全**。
成品平图还要求**顶部金属横梁有足够高度**：727 上下横梁都在，但顶部横梁过窄、板面
从 6% 就开始，用户确认应算不全；113/389 顶部横梁厚（板面起点 0.14 左右），用户
确认是正常图，不得按右侧无外框判不全。旧 2026-10-12 "左右/上方拍到相邻根盒可接受"
的口径已废止，不能再按它验收。

**信号（全部来自 checks.py:_long_edge_metric 的 512 宽行剖面，Sobel_y>40、7 行平滑）：**

- 上侧：top_peak_strength（上 30% 内最强水平边的挺直度）和 top_peak_y（其位置）。
  正常顶梁贴顶，strength 高、y 小；顶梁没拍到或相机下移时，强度掉到背景水平或
  位置落进画面中部。
- 下侧：bottom_peak_y（下 30% 内最强水平边的位置）。正常最低约 0.874；相机
  上移/底梁被裁时，最强边会提前到 0.868 以下（更靠画面中部）。
- 底部结构：bot_zone_max（85%~100% 最大挺直度）和 bottom_band_max（目标底梁带
  85%~96%）。两者都低，说明底梁结构不在画面里，而不是"底梁位置低"。
- 成品顶部覆盖（仅 `input_kind="mosaic"`）：`_top_cover_y` 在 512 宽 HSV 里取
  土壤色（H 5~45、S≥45、V≥25）行占比，连续 12 行 >0.45 的第一行即板面起点
  `top_cover_y`。727=0.0616（顶梁过窄），正常 508=0.0792、782=0.0821、
  113=0.1437、389=0.1378。原图 raw 不套用，避免把拼缝侧/未拼原图误杀。

**判据（按顺序，命中即不全）：**

0. 成品图 top_cover_y < 0.072 → 顶部金属横梁过窄/被裁（727 类，仅 mosaic）；
1. top_peak_strength < 0.22 → 上侧金属长边缺失/未拍到；
2. top_peak_y > 0.20 → 上侧金属长边被推离画面顶部；
3. bottom_peak_y < 0.868 → 底部金属长边被推离画面底部/明显偏上；
4. bot_zone_max < 0.35 且 bottom_band_max < 0.28 → 底部金属长边结构缺失；
   仅当 mean_gray < 65、bottom_band_max >= 0.15、bot_zone_max < 0.25 时记
   metric.long_edge.dark_bottom_exempt 放行（066 类暗底）。
5. 兜底：bot_zone_max < 0.15 → 不全；0.15~0.25 的非暗图只在缝台阶形态成立时
   豁免。

**关键样本（新代码实测）：**

| 样本 | top_strength / top_y | bottom_y | bot_zone / band | 结果 |
|---|---|---|---|---|
| 052/1（用户确认不全） | 0.168 / 0.026 | 0.827 | 0.400 / 0.400 | fail 上侧 |
| 052/2（同批，待确认） | 0.179 / 0.026 | 0.830 | 0.398 / 0.398 | fail 上侧 |
| 865（用户举例） | 0.252 / 0.050 | 0.862 | 0.390 / 0.390 | fail 底部位移 |
| 1183 | 0.189 / 0.015 | 0.886 | 0.395 / 0.395 | fail 上侧 |
| 301/2 | 0.424 / 0.082 | 0.927 | 0.275 / 0.275 | fail 底梁带 |
| 2241/2 | 0.587 / 0.179 | 0.985 | 0.323 / 0.096 | fail 底梁带 |
| 727（用户确认不全） | 0.316 / 0.050 | 0.941 | 0.466 / 0.466 | fail 顶部覆盖 0.0616 |
| 066（正常） | 0.361 / 0.041 | 0.985 | 0.208 / 0.174 | pass 暗底豁免 |
| 099/1（正常） | 0.429 / 0.029 | 0.927 | 0.294 / 0.294 | pass |

113（正常）top_cover_y=0.1437、389（正常）=0.1378，均 pass；727 的问题是顶部横梁
过窄，不是右侧外框（旧右侧判据把 113/389 误杀，已 revert）。

052/1 的顶部没有目标根盒顶梁，旧代码只因为 bot_zone_max=0.400 >= 0.396 放行；
0.396 是为这张误标正常图拟合出来的，已删除。865 的顶梁其实可见
（top_strength=0.252），它是底梁最强边提前到 0.862 被新判据拦下，不再是"底部
强度 0.392 险过 0.396"的偶然结果。

**dataed 全量（in-sample，非盲测；只跑正常/不全两类）**：251 张，标签为正常 145 /
不全 106（052/1 用户确认不全，052/2 同批同模式移入不全，727 用户确认不全；原始
dataed 目录未改）。`check_completeness` 为正常 145/145 pass、不全 106/106 fail；
完整 `verify_single` 为正常 144/145（唯一 fail 是 066，既有 alignment_single 判
错位，用户已认可）、不全 106/106 fail。106 张不全里多数同时命中损坏/模糊，属多缺陷。

**限制（必须写进验收）：** 0.22 / 0.20 / 0.868 / 0.28 / 0.072 都是在 dataed 这 251
张上选的 in-sample 阈值，不是盲测。目标底梁带 0.28 与不全 301/2（0.275）、正常 099
（0.294）只差 0.003~0.014；底梁位置 0.868 与不全 865（0.862）、正常最低 0.874 只差
0.006；顶部覆盖 0.072 与正常 508（0.0792）只差 0.0072，727（0.0616）余量 0.0104。
052/2 的标签仍需现场确认。换批次/换机位必须重新抽 20~30 对正常 + 20~30 张不全复核，
不能把 145/106 当作泛化精度。

> **以下 2026-09-18 / 09-22 / 10-12 为历史标定记录，现行口径以上文 2026-10-29 为准。**
>

>
> **2026-09-22 按原图真值集重标定（推翻上一版“原图会整批误报”的结论）。**
> 上一版用的 data-已分类 正常标签混入大量错标（“正常”图 y_top 高到 0.141），
> 据此得出“原图长边系统性偏低、8/8 误判”，并错误地把 top_pos_max 放宽到 0.15。
> 用户重新整理的真值集 D:\pic\data（正常 50 样本 / 不全 60 样本，原图对+散装单图）
> 全量实测后结论完全反转：
>
> - **真值正常图 y_top 上限就是 0.044**（与成品图一致）-> 原始 0.05 阈值是对的，
>   已把 top_pos_max 从 0.15 改回 0.05。上侧是最干净信号：检出 36/60，误报 0/50。
> - 下侧 y_bot<0.92 单独看会误报 2 张暗画幅图（066 y_bot=0.126、321=0.167
>   属伪读数：下端检不到强长边时 y_bot 退化到画面中上部）。改用**可信下限 0.5
>   守卫**（替代上一版无效的 bot_strength>=0.25 强度守卫——真值集上不全图下强度
>   同样低，强度守卫会把检出从 83% 打到 60%，完全没区分度）。
>
> 生产代码端到端实测（全量跑 check_completeness）：
> - 正常 **误报 1/50**（085，y_bot=0.925，视觉模型评价其盒子偏小偏上，
>   疑似人工标注偏宽松，待复核）；不全 **检出 56/60（93%）**。
> - 漏检 4 张（508/566/615/719）y_bot 0.968~0.988，与正常图下限（0.944 起）
>   完全重叠，位置判据物理上无法分离，与成品图标定时的边界结论一致。
> - 下侧阈值取在交换曲线最优点 0.935：0.92 版 83%/0 误报，0.935 版
>   93%/1 误报，0.940 版 95%/3 误报。曾评估成对一致性判据（两图 y_bot
>   差>0.03 判不全）能多抓 015/018，但同样命中 085/099，且与错位检测
>   重叠、与用户轻度错位可容忍的标准冲突 -> 不采用。
>
> **2026-10-12 用户口径放宽："必须拍全当前根盒；左右拍到相邻根盒可接受"**
> （dataed 拼接平图 1870/1885/1896 族：根盒完整、整体下移、上方多出土壤，
> 旧判据误报，新口径应判正常）。判据三处变更：
>
> 1. **移除上侧 `y_top>0.05` 触发**——1870 族 top 0.05~0.14 是上方多余
>    土壤所致，非画框偏移；新口径下上侧/左右拍到邻居均可接受。
> 2. **停用拼接双帧滞后信号**（`completeness_mosaic_lag_check_enabled=False`）
>    ——lag 幅值无法区分"完整但整体下移"（1870 +4.7%~+6.5%、1885 +6.3%、
>    1994 +11.3% 均根盒完整）与真裁切。
> 3. **新增 Rule A"底部结构缺失"**：根盒完整时底部长边强带必在 y∈[0.85,1.0]
>    出现（实测完整族带起点 0.938~0.985）；真裁切族该区 tall 峰值全池 ≤0.249、
>    正常池最低 0.260，以 pos_thr=0.25 分界。**暗图守卫**：单灯管暗帧底部区
>    同样无强带但全图均值 ≤62.6（裁切族 ≥68.4、完整族 ≥67.1）→ 均值 <65 豁免
>    （底部区局部亮度不可分：裁切 53.0 vs 暗帧 52.5）。前提"剖面存在强长边带"。
>
> 下侧 `y_bot<0.935`（含可信下限 0.5 守卫）保留：抓顶裁/整体上移族
> （2142=0.909、1791=0.933、2388/2431=0.927，顶边均为金属触边；
> 1870 族顶边为土壤，不触发）。dataed 全池 907 图回归：正常池 85 单元零误报；
> 不全池 312 单元中 274 翻正常、38 保持不全（含 1791/2142/2388/2431 等）。
> 已知 2 例拼接缝伪影（错位\2193.png、错位\2373.png：缝两侧断带使
> bot_zone_max≈0.246 被 Rule A 命中，其帧图五项全过证明根盒完整）——
> 按接缝伪影接受，重分类时人工复核。
>
> **2026-10-12 二次修订（同日，用户裁定 2142/2431/1791 正常、
> 2373 "只有错位"）**，对上面两处遗留的进一步修正：
>
> 4. **移除下侧 `y_bot<0.935` 位置触发**——2142=0.909、1791=0.933、
>    2388/2431=0.927 族虽长边位置偏低，但底部金属带+螺栓结构完整可见
>    （bot_zone_max 0.31~0.58），属正常构图；真下裁切由 Rule A 兜底。
> 5. **Rule A 增"缝台阶豁免"**——拼缝两侧同一根盒上下错位会把底部金属
>    长边逐行打碎、将 tall 强梯度列占比稀释到 pos_thr 之下（伪"无强带"）。
>    豁免复用错位单张缝组逻辑（`_seam_step_misalign`：自适应缝候选 → 缝两侧
>    dy → 符号交替链），chain≥3 条即豁免。实测 2373/2193/2059 平图 chain=3
>    （落差 13~31px 交替），Rule A 族其余 48 张链长 ≤2 全保持。
>
> dataed 全池 828 图回归：8 单元翻转（1791/2142/2142.png/2431.png → 正常；
> 2059.png/2193.png/2373.png/2388.png → 错位）、35 保持不全（零新增误报）、
> 两表同步（不全 43→35 行）。保持族中 2053/2119/2163 等贴阈样本待复核。
## 上线标定（重要）

0. **质检输入必须是相机原图**（未裁剪、未缩放），默认画幅 **5472×3648**。
   "根盒边框角点越界"等检查依赖全幅语义，裁剪/缩略图会导致误报；分辨率守卫会把
   其他尺寸判 `undetermined`。换相机/换分辨率必须同时改
   `expected_width/expected_height` 并重新标定刀口。
1. **阈值默认值基于 2026-09-07 F:\2026_maize_root_ertao 现场重标定**
   （全天 1170 组分布 + 问题样本逐张核对），原 D:\yy1 夜间调优值见 config.py 注释；
   换产线/换拍摄环境必须重新标定。
   `blur_led_max_width / completeness_hull / alignment_shift / alignment_inlier`
   四项已于 2026-09-08 按 bigbox 线（`D:\pic\data` 已有命中样本 + 抽检）调整，
   当天仍存疑的 `blur_var_min` 建议在硬盘回归后取一批正常图复核。
2. 建议流程：新环境跑 `scripts/smoke_test.py` + `scripts/opt_test.py` +
   `scripts/regression_5472.py` 确认管线/画幅/竖拍可用 → 抽取 3~5 天真实照片
   用离线批量模式跑一遍，对照检出率核对阈值 → 需要调整只改 `config.py:THRESHOLDS`。
3. 标定时重点看 metric 分布（报告 JSON 里有每张的 metric），动态调阈值。
4. `blur_var_min`（ROI 拉普拉斯）建议按现场清晰图基线标定后固定。
5. **刀口/布局跟随相机角度**：默认 C；旧角度批次用 `layout="A"` 或 `"B"`。
   换角度必须重新测刀口并整套切换（模糊存活列、单张缝位、固定缝窗），
   只改 `blur_strip_cuts` 会让另外两类判据错位。
6. **拼接器与拼接后复检**：生产必须同时配置 `paths.stitch_script`（提供
   `stitch_from_qc(img_a, img_b, out_path)`）和 `paths.stitch_output_dir`；
   上线前用真实一对图跑通 `verify_pair`，确认 `stitched_status="pass"`。未配目录时
   只完成单图+成对检查，不满足“拼接后复检再继续拍摄”的流程。

## 用法三：导出坏图（筛选 + 整夹分类剪切）

从落盘目录筛出问题图，**整夹（按来源编号目录）剪切**到指定目录，文件名不动。
**命中的来源目录/文件会被移走**（多类命中时先复制到其余分类、最后剪切一次），
不确定时先 `--dry-run` 看计划：

```bash
python scripts/export_bad_photos.py -i F:\...\Data0525 -o D:\qc_bad            # 剪切 + 明细.csv
python scripts/export_bad_photos.py -i F:\...\Data0525 -o D:\qc_bad --dry-run  # 只列计划，不动文件
python scripts/export_bad_photos.py -i F:\...\Data0525 -o D:\qc_bad --multi-copy  # 多类命中每类一份
python scripts/export_bad_photos.py -i F:\...\Data0525 -o D:\qc_bad --thresholds '{"tilt_angle_max_deg": 5.0}'
```

输出：`损坏|模糊|倾斜|不全|错位|重复拍摄\<编号目录>\`（目录内 1.png/2.png 原样）+ `明细.csv`
（来源路径/导出目录/主分类/全部命中/原因，UTF-8-BOM）。

规则说明：
- 以"来源目录"为单位：某编号目录内任一图片命中，整个目录剪切到对应分类下
  （如 `Date0525_left\001\` → `错位\001\`，源目录随之移走）；左右/分批目录同名时
  自动加上级目录前缀（`Date0525_right\001` → `Date0525_right_001`），
  同一来源目录同分类只导出一次。
- 平铺在输入根目录里的单个文件（拼接成品等）按原文件名单独剪切。
- 一张图命中多类时按优先级归主类（损坏>模糊>倾斜>不全>错位>重复拍摄），明细.csv "全部命中" 列完整记录；
  `--multi-copy` 时每个命中类都放一份。
- 多类命中的目录/文件先复制到其余各分类、最后剪切一次（源只移走一次，各分类下内容完整）；
  任一副本失败则保留源不剪切，避免丢数据。
- 全部图片检查完后才统一执行剪切（不会边检边移），实际执行的复制/剪切见控制台 `[复制]/[剪切]` 行。
- 拼接成品受限于单张信息，只能覆盖单张类检查，"两张间错位"与"重复拍摄"只对原图成对生效。

**单独部署**（不依赖整个项目）：拷贝 `export_bad_photos.py`、`export_bad_photos.bat`、
`requirements.txt` 与 `rootbox_qc\` 包到目标机器任意目录即可运行（脚本自动定位同目录的
rootbox_qc）。目标机需装 Python 3.9+，且 `pip install -r requirements.txt`；
如 Python 路径不同，改 `.bat` 顶部 `PY=` 一行。
**输入/输出路径在脚本顶部改**（`INPUT_DIR` / `OUTPUT_DIR` 两个常量），改完双击 .bat 即可；
命令行 `-i`/`-o` 可临时覆盖，运行开头会打印当前使用的输入/输出目录。

## 用法四：目录监听服务（上线形态，2026-10-30）

`main.py` 是离线批处理；线上要的是"小车把一对图落到目录 → 自动质检 → 把结论反馈回去"。
`run_service.py` 就是这个壳：轮询落图目录、自动配对、调 `verify_pair`、写反馈（可选 HTTP 回调），
单对异常不会带崩服务，启动时配置不全直接拒绝启动。

### 落图约定（小车侧）

| 写法 | 例子 | pair_id |
|---|---|---|
| 编号子目录 | `<watch>\052\1.png` + `2.png` | `052` |
| 任意两张 | `<watch>\052\a.png` + `b.png`（目录里正好 2 张） | `052` |
| 批次 + 编号 | `<watch>\20261030\052\1.png` | `20261030__052` |
| 平铺 | `<watch>\052_1.png` + `052_2.png` | `052` |

- 目录里正好 2 张图即视为一对；多于 2 张只认 `1/2` 命名，否则跳过并在日志 warning。
- 支持 `.png/.jpg/.jpeg/.bmp`；忽略隐藏目录、`__pycache__`、`done/`、`output/`、`feedback/`、`logs/`。
- 监听目录**不能等于**输出目录（否则会把自己的输出当落图，启动自检会拦）。
- 文件正在写时按 PNG/JPEG 文件头判断"未就绪"，等写完再处理，不会读到半张图。

### 启动

```bat
set ROOTBOX_QC_STITCH_DIR=D:\yy1\qc\stitch
python run_service.py --watch D:\yy1\qc_in --out D:\yy1\qc_out --health-port 8090
rem 或：run_service.bat --watch D:\yy1\qc_in --out D:\yy1\qc_out --health-port 8090
```

- **fail-closed 自检**（不过就退出码 3，不会带半套配置上线）：监听/输出/反馈/日志目录可写且不重叠；
  `require_stitch` 时 `stitch_output_dir` 必须配、`stitch_script` 必须存在且能加载出 `stitch_from_qc`；
  画幅 5472×3648；`layout` 合法；`blur_strip_cuts` 三个升序整数；cv2 真跑一次；回调地址合法。
- `--no-require-stitch`：调试用，跳过拼接复检（反馈 `degraded: ["stitch-disabled"]`），生产别用。
- `--shadow`：旁路只记录不拦车；`--once` 扫一轮退出；`--dry-run` 只列候选；`--print-config` 打印生效配置。

### 反馈（小车侧读什么）

| 文件 | 用途 |
|---|---|
| `<out>\feedback\<pair_id>.json` | 单对结论，**小车只读这一个**；原子写（tmp+fsync+replace），不会读到半个 JSON |
| `<out>\feedback\feedback.jsonl` | 追加流水，夜间批量汇总用 |
| `<out>\<pair_id>.json` | 完整报告（每张图逐项 checks），排障用 |
| `<out>\recapture_queue.jsonl` | 重拍队列（shadow 时写 `shadow_queue.jsonl`） |

单对结论示例：

```json
{
  "pair_id": "1491",
  "status": "fail",
  "passed": false,
  "recapture_needed": true,
  "targets": ["D:\\yy1\\qc_in\\1491\\1.png", "D:\\yy1\\qc_in\\1491\\2.png"],
  "reasons": ["[1.png] 底部金属长边结构缺失/不明显（底部挺直度 0.220 < 0.35 且目标底梁带 0.220 < 0.28），疑似未拍全根盒下侧"],
  "attempt": 1,
  "max_attempts": 3,
  "escalate_manual": false,
  "systemic_alert": null,
  "failure_categories": ["不全"],
  "stitched_status": null,
  "resolution_mismatch": false,
  "elapsed_ms": 1512.0,
  "processed_at": "2026-10-30T13:30:12",
  "report_file": "1491.json",
  "degraded": []
}
```

- `recapture_needed=true` 才拦车；`targets` 是重拍哪几张（绝对路径）。
- `status=undetermined`（分辨率不对/读图失败/成品缺失）默认也 `recapture_needed=true`，别当通过。
- `escalate_manual=true`：已达 `max_attempts`（默认 3）仍不合格 → 转人工，不要再让小车重拍。
- `resolution_mismatch=true`：输入被裁剪/缩放，先查落图链路（压缩/转码），别改阈值。
- `attempt` 按 pair_id + 文件指纹递增：重拍落回同一个 pair_id 目录即可。
- shadow 模式下 `recapture_needed` 恒为 `false`，真实判断放在 `would_recapture` / `would_reasons` / `would_targets`。

### 重拍、幂等与异常

- 状态文件 `<out>\service_state.json`：记录每对（文件名+大小+mtime）。文件没变 → 不重复质检；
  重拍改了文件 → `attempt+1`；服务重启从状态恢复，不重跑、不丢次数。
- 单对异常（坏文件、拼接器崩）最多重试 3 次，超过写一条 `status=error` + `escalate_manual=true`
  的反馈，然后继续处理后面的对。
- HTTP 回调（`--callback http://<小车>:8080/qc`）带 `Idempotency-Key: <pair_id>:<attempt>`；
  网络重试是"至少一次"投递，对端按这个键去重。合格对默认不回调（`notify_on_pass` 可开）。

### 运维面

- `/health`：200 + JSON（uptime、pairs_total、pending、p50/p95、last）；心跳超时返回 503 `status: stale`。
- `/metrics`：Prometheus 文本（`rootbox_qc_pairs_total` 等）。
- 单实例锁 `<out>\.watch_service.lock`：同一输出目录只允许一个进程；崩溃残留锁按 pid 存活自动接管。
- 日志 `<log-dir>\watch_service.log`，5MB × 5 轮转。
- 建议 `schtasks /sc onstart` 或 NSSM 包装，重启策略 always。

### 上线顺序（别跳）

1. `--shadow` 跑 1~2 周：只记录不拦车，每天抽 20~30 对人工核对 `would_recapture`，统计"若真拦会误停几次"。
2. 冻结阈值，真实样本池盲测（先跑后对答案），达到验收线再切 live：
   正常误报≤1%、损坏/不全漏检≤5%、`undetermined`<1%、p95≤3s/对。
3. 切 `live` 第一周保留人工放行通道（删 `service_state.json` 里该对或重跑即可）。

实测（本机，含拼接复检）：合格对 5.4~5.6s，不合格对 1.4~1.5s，串行。多工位并发需多进程/多输出目录。

```bash
python scripts/service_test.py    # 真图 × 真拼接脚本 × 真 HTTP，期望 31 项全 PASS
```

## 自检

```bash
python scripts/smoke_test.py       # 合成图全链路：期望 SMOKE OK
python scripts/opt_test.py         # 五类信号/复用/刀口/三态/拼接复检/不全上下对称+顶部覆盖/成品图纹理：期望 ALL PASS
python scripts/regression_5472.py  # 5472 横/竖图 + 真实锚点：期望 11/11 PASS
```

`opt_test.py` 第 9 节用 `fake_stitch_good/black/raise.py` 覆盖拼接成品通过、
成品缺陷阻塞、拼接异常、关闭复检、单图失败短路、缺目录等分支；第 10 节覆盖
“顶梁贴顶/下移/缺失”、052 类、066 暗底豁免、底部缺失/偏上/目标带弱、727 类成品顶部覆盖和 LED 门；第 2 节另有成品图 6×8 分块纹理 p10 的清晰/轻失焦对照。真实拼接器
仍需用现场一对正常图手工跑一次 `verify_pair` 确认可产出且 `stitched_status="pass"`。

`regression_5472.py` 只读 `D:\rootguard\dataed` 的少量锚点（正常 917/915/912、
模糊 007~010），不会改动数据；换机器用 `--data` 指定数据根目录。

## 常见问题

| 现象 | 处理 |
|---|---|
| `ModuleNotFoundError: cv2` | `pip install -r requirements.txt` |
| 提示"无缝拼接脚本不存在" | 配置 `paths.stitch_script` 或环境变量 `ROOTBOX_QC_STITCH_SCRIPT`，指向提供 `stitch_from_qc` 的脚本 |
| 扫描到 0 对 | 检查 INPUT_DIR；也可设环境变量 `ROOTBOX_QC_INPUT_DIR` |
| 好图全被标模糊 | 先看报告 `input_kind`/`layout` 是否与画幅匹配（不匹配应报 undetermined，而不是模糊）；再核对 1/2 号角色和 C 刀口，最后才动 `blur_strip_alive_var_min` |
| 提示"分辨率 ... 不是 5472x3648" | 输入被裁剪/缩放，或换了相机；改 `expected_width/expected_height` 并重标刀口，不要直接关守卫 |
| `stitched_status` 是 `None` | 没配 `stitch_output_dir`，只做了单图+成对检查；生产设 `ROOTBOX_QC_STITCH_DIR`，离线人工审片可忽略 |
| 拼接图判 `fail/undetermined` | 看报告 `stitched_checks` / `stitch_error`；成品问题会阻塞整对并指向两张原图，先排查拼接器输出、分辨率与 C 刀口 |
| 拼接后每对都变慢/占磁盘 | 每个通过的对都会写 `<pair_id>.png` 并多跑一遍检查；离线只审片传 `stitch_output_dir=""` 关闭 |
| 好图全被标倾斜 | 上调 `tilt_angle_max_deg`（旧值 8.714 经 D:\yy1 调优） |
| 监听服务启动报"未配置拼接输出目录" | 设 `ROOTBOX_QC_STITCH_DIR` 指向拼接输出目录（生产必须）；仅调试可加 `--no-require-stitch` |
| 落图后反馈目录一直没新文件 | 看日志和 `/health`：`pending` 是否为 0（图片数不对/命名不匹配/目录被 skip），或文件还在写（文件头未就绪会等）；确认监听目录没配成输出目录 |
| 同一对反复重拍仍是 fail | 正常：`attempt` 到 `max_attempts`（默认 3）会 `escalate_manual=true` 转人工；看 `failure_categories` 和完整报告定位 |
| 想手工重跑某一对 | 删 `service_state.json` 里该 pair 的记录（或用新 pair_id 落一次）；文件指纹没变不会重复质检 |
| 大图跑得慢 | 本机实测 5472×3648 单张约 0.8~1.5s、成对约 1.8~3.2s（不含拼接）；线上可用 `align_orb_downscale` 调大提速 |

## 日志

`scripts` 运行自动写 `./logs/rootbox_qc.log`（控制台 + 文件）。
