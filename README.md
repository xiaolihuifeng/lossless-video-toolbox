# 无损视频工具箱（Lossless Toolbox）

一个基于 PySide6 的桌面工具，对视频做「搬运式」处理——改封装、按关键帧剪切、
合并、抽取音轨、加/抽软字幕、改元数据——全程流拷贝，不重新编码，画质与音轨逐
字节原样保留。支持多文件批量排队处理。

底层命令由 ffmpeg 生成并执行；ffprobe 负责探测流信息与关键帧索引。

## 安装

发行包自带静态编译的 ffmpeg/ffprobe，无需单独安装。开发环境运行则需要 ffmpeg
与 ffprobe 位于 `PATH`，或通过环境变量 `LOSSLESS_TOOLBOX_FFMPEG_PATH` /
`LOSSLESS_TOOLBOX_FFPROBE_PATH` 指向绝对路径。

- **Linux**：`tar.gz`（onedir 目录）与 `AppImage`（可双击运行）。
- **Windows**：安装包（由 GitHub Actions 的 Windows runner 构建；本机不做交叉
  编译）。

运行后可用命令行自检工具链：

```
lossless-toolbox --probe-self          # 打印 ffmpeg/ffprobe 路径与版本
lossless-toolbox --strict-bundled --probe-self  # 仅接受捆绑二进制，禁用 PATH 回退
```

## 功能

九项操作全部以流拷贝实现，对应 UI 的六个操作面板与批量执行层：

1. **转封装（remux 混流）**：MP4/MKV/MOV/TS 等容器互转。目标容器能否承载某条流
   由运行时探测 `ffmpeg -h muxer=<name>` 决定，不硬编码矩阵；TS 目标自动加
   `h264_mp4toannexb` / `hevc_mp4toannexb` 比特流滤镜；`.aac` 裸流输出加
   `-f adts`。
2. **关键帧剪切**：`-ss` 置于 `-i` 之前做输入侧定位；切点吸附到关键帧，UI 显示
   实际切点（见「无损边界」）；切头时加 `-avoid_negative_ts make_zero`；时长用
   `-t (end-start)` 相对值。
3. **无损合并（concat）**：concat demuxer，文件列表经 stdin 管道喂给 `-i -`
   （`-f concat -safe 0 -protocol_whitelist file,pipe,fd`）；容器元数据与章节
   取自第一个真实输入（`-map_metadata 1 -map_chapters 1`）；合并后显式重放每条流
   的 `-disposition`；预检各输入流参数（codec/profile/分辨率/采样率/声道数）一致，
   不一致则阻止并列出差异。
4. **音轨提取 / 剥离 / 替换**：按流 `-map` + `-c copy`。提取单条音轨
   （`-map 0:a:N`）；剥离即仅保留勾选的流；均不做音量/延时/归一化（重编码域）。
   替换（以另一文件的音轨替换原音轨）已在底层引擎实现并有单元/集成测试覆盖，
   会先探测新音轨编码与目标容器兼容性、不兼容则报错并提示先转封装；该模式 UI
   尚未开放，当前界面仅提供「提取音轨」「剥离流」两种操作。
5. **软字幕封装 / 抽取**：MKV 走 srt/ass 纯拷贝；MP4 目标 srt → mov_text 文本
   转码并在结果中显式标注警告；ass/webvtt/dvb 进 MP4 构造期拒绝；webm 仅 webvtt。
   抽取统一输出 SRT，mov_text/ass/webvtt 源走文本转换并标注警告，位图字幕
   （dvb_subtitle/PGS/VobSub）拒绝。
6. **元数据改动**：title / language / creation_time 注入；章节经 ffmetadata 侧车
   文件读写（导出 `-f ffmetadata`，写入为第二输入 + `-map_chapters 1`）；旋转仅
   MP4/MOV（`-display_rotation:v:0 <360-deg>` 输入侧注入，MKV 目标拒绝）；封面
   MP4 用 `attached_pic` disposition、MKV 用 `-attach`。均不做像素级旋转或封面缩放。
7. **批量队列**：多文件批量提交任一操作，顺序单并发执行；单个任务失败只记录错误
   并继续后续任务，不中断整批；支持取消当前任务或取消全部。
8. **进度 / 错误 / 汇总**：解析 ffmpeg `-progress pipe:1 -nostats` 的机器可读输出
   （`out_time_us` / `frame` / `speed`）驱动进度条；失败时保留 stderr 末尾 4KB 供
   错误展示；完成后给出「N 成功 / M 失败 / K 取消」汇总与打开输出目录入口。
9. **合并 stdin 全链路**：concat 文件列表以 UTF-8 字节经 `build_stdin_data` →
   队列 → runner 的 stdin 通道喂给 ffmpeg，全程不经临时文件。

## 无损边界

**本工具绝不重编码。** 除下述两条白名单例外，所有操作的 ffmpeg 命令都不含视频/音频
编码器（`-c:v` / `-c:a` 后一律 `copy`），并由 `tests/unit/test_no_reencode.py`
机器审计把关：任一操作出现白名单外的非 `copy` 编码器即构建失败。

- **GOP 定律 → 帧级精确剪切不可能。** 流拷贝只能从关键帧边界开始/结束，无法从
  任意帧切开。因此剪切是「关键帧内吸附」：实际起点取不小于请求起点的第一个关键帧，
  实际终点取不小于请求终点的第一个关键帧，保证完整包含请求区间。UI 的「吸附预览」
  会实时显示吸附后的实际切点，而非用户输入的原始时间点。要帧级精确只能对切点附近
  重编码，本工具明确不做（不做 smart-cut）。
- **mov_text 字幕文本转码是唯一的媒体流例外。** MP4 只能承载 mov_text 文本字幕，
  把 SRT 封进 MP4 需要 `-c:s mov_text`——这是内容保持的文本级转码，不是流拷贝，
  因此必须显式警告用户（UI 红色提示，结果标注 `transcode_warning`），绝不静默标成
  无损。其余不兼容字幕（ass/webvtt/dvb → MP4、位图字幕）一律构造期拒绝。
- **封面 `-c:v:1 png` 是容器级白名单。** 给 MP4 加封面时，静态图片以单帧 PNG 视频
  流封装（`attached_pic` disposition），源 A/V 流仍 `-c copy`，非媒体重编码，
  不改变 A/V 码流。
- **moov 重建 ≠ 重编码。** 转封装、剪切、合并时 ffmpeg 会重写容器索引（如 MP4 的
  `moov` atom）并可能移动元数据到文件头（`-movflags +faststart`），这只是容器
  层面的重组，媒体流的压缩数据（视频帧/音频样本）始终逐字节拷贝，不涉及解码与
  再编码。
- **凡会让文件变样的操作本工具直接不做。** 裁剪画面、缩放、加水印、压制字幕、
  变速、调音量、像素级旋转/翻转——这些必然重编码，一律不实现，也不留「高级选项」
  入口；发现相关参数即视为越界。

## 开发指南

要求 Python ≥ 3.10，ffmpeg/ffprobe 在 `PATH`（或经环境变量指定）。

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"   # 或 uv sync
```

测试用 pytest 分三个 marker（`pytest.ini` 中登记）：

- `unit`：无 I/O 的快速单元测试（命令构建器、probe 解析、纯逻辑）。
- `integration`：真实调用 ffmpeg/ffprobe 的集成测试（合成媒体样本）。
- `gui`：pytest-qt 的界面测试，需 offscreen 平台。

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -m "unit or integration or gui" -q
.venv/bin/ruff check .        # lint 全绿
.venv/bin/basedpyright src    # 0 errors
bash scripts/qa_smoke.sh      # 一键全量冒烟（退出码即 pytest 退出码）
```

打包由两个脚本承担：Linux 用 `packaging/build_linux.sh`，Windows 用
`.github/workflows/` 下的 CI workflow（Windows runner）。规格见下节。

## 打包

使用 PyInstaller onedir，规格文件 `packaging/lossless-toolbox.spec`。

- **Linux**：`packaging/build_linux.sh` 产出 onedir `tar.gz` 与 AppImage
  （linuxdeploy）。
- **Windows**：GitHub Actions 的 Windows runner 产出 exe 目录 + 安装包。
- **发行包捆绑静态 ffmpeg/ffprobe**（BtbN linux64-gpl / gyan.dev essentials，
  SHA256 钉住），运行时不依赖系统安装。

## 故障排查

- **找不到 ffmpeg/ffprobe（ToolchainError）**：定位器按顺序回退——捆绑目录
  （`resources/bin`）→ 环境变量 `LOSSLESS_TOOLBOX_FFMPEG_PATH` /
  `LOSSLESS_TOOLBOX_FFPROBE_PATH` → `PATH` 查找，命中后以 `-version` 校验。报错
  信息附带完整搜索日志与安装指引。打包 QA 用 `--strict-bundled` 禁用 `PATH` 回退，
  强制只接受捆绑二进制。
- **兼容性拦截（字幕 → MP4 的 mov_text 阻断）**：remux 时若源文件带 srt/ass/
  vobsub/pgs 字幕且目标是 MP4 家族，会在构建命令前直接阻断（MP4 只承载 mov_text，
  直拷会导致字幕转码），UI 提示改用字幕操作或剔除字幕流，而不是静默产出坏文件。
- **合并重复流限制**：concat 预检要求各输入流的 codec/profile/分辨率/pix_fmt/
  采样率/声道数逐项一致且至少 2 个输入，不一致会列出字段级差异并阻止；跨编码合并
  不可能无损，故一律拒绝。此外剪切对「非零起始时间」的容器（如带 `-output_ts_offset`
  的 TS）会拒绝，要求先转封装归零再重试。
