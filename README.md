# Video Translator

由 Codex 操作的本地视频翻译与配音工具。用户提供 YouTube 链接或本地视频，Codex 负责获取原文、按上下文写中文配音稿、生成配音并验收成片。每个任务交付三份文件：

- `视频名-任务号.mp4`，默认播放中文配音，可切换到原声音轨。
- `视频名-任务号.zh-CN.srt`，简体中文字幕。
- `视频名-任务号.en.srt`，英文字幕。

两份字幕共用原视频时间轴。英文字幕保留转写稿的时间，跨片段的中文字幕按配音重新对齐。播放器可按文件名识别外置字幕，也可以手动选择语言。默认输出的 MP4 只包含视频和音轨。

已整理的系列和单片可从 [项目目录](data/projects/README.md) 查找。项目子目录可以整包搬走，恢复时放回原路径。Codex 的目录操作规则见 [AGENTS.md](AGENTS.md)。

## 安装和检查

在 macOS 或 Linux 的项目根目录运行。需要 Python 3.10～3.13，推荐 3.12。

```bash
./scripts/bootstrap.sh
.venv/bin/video-translator doctor
```

安装脚本创建 `.venv`，安装识别、下载、TTS 和测试依赖，在缺少系统 FFmpeg 时下载项目本地版本。可用 `PYTHON=/path/to/python3.12 ./scripts/bootstrap.sh` 指定解释器。默认配置无需创建 `.env`，需要修改时可从 `.env.example` 复制。

首次语音识别可能需要下载 Whisper 模型。默认使用 `large-v3`，初次冒烟可在命令前设置 `VT_ASR_MODEL=tiny`。Edge TTS 默认在线生成配音，也可按 `.env.example` 配置 HTTP TTS 或 CosyVoice。

## Codex 操作流程

以下两条命令由 Codex 运行。也可以用本地 MP4、MKV、MOV、WebM 或 M4V 路径作为输入。

```bash
.venv/bin/video-translator prepare 'https://www.youtube.com/watch?v=VIDEO_ID' --json
# Codex 按原文片段写完 translations.json，再用 prepare 返回的任务号继续。
.venv/bin/video-translator render JOB_ID --json
```

1. `prepare` 获取视频并识别语音，返回 `status=prepared`、`job_id`、`detected_language`、`segments_path`、`translations_path` 和 `log_path`。原文语言默认自动识别，已知时可加 `--source-language en` 等语言代码。加 `--no-original-audio` 可以省略独立的原声音轨。
2. Codex 从 `segments_path` 读取原文。每个片段有 `index`、`start`、`end` 和 `source_text`。短视频可通读全文，长视频可按话题分批，并共享人名、术语和前后文。将译稿写入 `translations_path`，每个片段用原来的 `index` 作为 `id`，不改原文或时间戳。
3. `render` 读入译稿，生成配音、对齐时间、混音并导出两份字幕。也可通过 `--translations /path/to/translations.json` 提供译稿。完成时返回 `status=completed`，`output_path` 是 MP4，`subtitle_path` 和 `english_subtitle_path` 分别是中文和英文 SRT。

英文原声的英文字幕直接使用原文，译稿只提供简体中文口语：

```json
{"segments":[{"id":0,"text":"你好，欢迎观看。"},{"id":1,"text":"我们开始吧。"}]}
```

其他语言的原声，每条译稿还需用 `en` 提供对应的英文字幕：

```json
{"segments":[{"id":0,"text":"你好，欢迎观看。","en":"Hello, and welcome."}]}
```

片段编号须覆盖所有原文片段且唯一。中文 `text` 同时用于配音和中文字幕，宜简洁、适合朗读，并考虑对应时间槽的长度。同一句话跨相邻片段时，按正常句意写标点。比如第一段写「如果准备好了，」，第二段写「我们就出发。」，渲染器会连续合成整句，中文字幕仍分两条。若语音服务返回词时间戳，字幕按实际语音对齐，否则按文字长度分配这句配音的时长。

原声音轨默认独立保留，中文配音音轨还会混入压低的原声。

## 重试与验收

`--json` 将任务结果写入标准输出，进度与诊断写入标准错误和任务日志。默认中间文件在 `data/jobs/<job_id>/`，CLI 渲染结果写入 `data/outputs/`。用任务返回的绝对路径定位文件。验收后，将选定版本和双语字幕归入对应项目的 `data/projects/<项目名>/delivery/`。

渲染失败时读取 `failed_stage`、`error` 和 `log_path`。若 `overflow_segments` 有数据，它会列出超时台词的片段编号、配音时长和时间槽长度。跨字幕片段的整句用 `cue_ids` 列出涉及的编号。缩短相应译文后，使用同一个任务 ID 再跑 `render`，会复用视频、转写和未改动的有效配音。每次成功渲染会把 MP4 和两份字幕一起写入新的版本目录，便于对照验收。旧任务若缺少原视频或转写文件，需要重新执行 `prepare`。

先用一段短片验收。确认 MP4 默认播放中文音轨，两份 SRT 都能加载，时间码在视频范围内，并抽听密集句和专业术语。真实 YouTube 下载、模型推理和在线 TTS 可用性随运行环境而变化。项目的集成测试用真实 FFmpeg 和替代模型验证编排与输出：

```bash
.venv/bin/pytest
```

## 可选的全自动翻译

需要脱离 Codex 批量处理时，可在 `.env` 中为 `translate` 设置 OpenAI-compatible 翻译服务。它可连接 Ollama、LM Studio 或兼容云服务。Codex 提供译稿的 `prepare` / `render` 路径无需这项服务。

```dotenv
VT_TRANSLATOR_BASE_URL=http://127.0.0.1:11434/v1
VT_TRANSLATOR_MODEL=qwen3:8b
```

```bash
.venv/bin/video-translator doctor --check-translator
.venv/bin/video-translator translate 'https://www.youtube.com/watch?v=VIDEO_ID' --json
```

全自动路径也会输出双语外置字幕。已有的 `--burn-subtitles` 选项可额外把中文字幕烧进画面。
