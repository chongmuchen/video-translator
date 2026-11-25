# Video Translator

由 Codex 操作的本地视频翻译与配音工具。用户提供 YouTube 链接或本地视频，Codex 负责获取原文、按上下文写中文配音稿、生成配音并验收成片。每个任务交付三份文件：

- `视频名-任务号.mp4`，默认播放中文配音，可切换到原声音轨。
- `视频名-任务号.zh-CN.srt`，简体中文字幕。
- `视频名-任务号.en.srt`，英文字幕。

两份字幕共用原视频时间轴。英文字幕保留转写稿的时间，跨片段的中文字幕按配音重新对齐。播放器可按文件名识别外置字幕，也可以手动选择语言。默认输出的 MP4 只包含视频和音轨。

已整理的系列和单片可从 [项目目录](data/projects/README.md) 查找。项目子目录可以整包搬走，恢复时放回原路径。Codex 的目录操作规则见 [AGENTS.md](AGENTS.md)。

## 安装和检查

在 macOS 或 Linux 的项目根目录运行。需要 Python 3.10～3.12，推荐 3.12。

```bash
./scripts/bootstrap.sh
.venv/bin/video-translator doctor
```

安装脚本创建 `.venv`，安装识别、下载和测试依赖，以及项目本地的 Kokoro 中文模型和音色。在缺少系统 FFmpeg 时下载项目本地版本。可用 `PYTHON=/path/to/python3.12 ./scripts/bootstrap.sh` 指定解释器。默认配置无需创建 `.env`，需要修改时可从 `.env.example` 复制。任务目录在实际处理任务时创建。

首次语音识别可能需要下载 Whisper 模型。默认使用 `large-v3`，初次冒烟可在命令前设置 `VT_ASR_MODEL=tiny`。

配音使用本地 Kokoro 82M v1.1 中文模型。默认音色为 `zm_010`，合成语速为 `1.12`。运行 `doctor` 查看已安装的音色，设置 `VT_TTS_VOICE=zf_001` 可改用该音色。其他兼容音色的官方 `.pt` 文件放在 `.runtime/puzzle-kokoro-model/` 下即可选择。

## Codex 操作流程

以下两条命令由 Codex 运行。将 `project-name` 换成 `data/projects/` 下的项目目录名，两步使用同一个项目名。也可以用本地 MP4、MKV、MOV、WebM 或 M4V 路径作为输入。

```bash
.venv/bin/video-translator prepare 'https://www.youtube.com/watch?v=VIDEO_ID' --project project-name --json
# Codex 按原文片段写完 translations.json，再用 prepare 返回的任务号继续。
.venv/bin/video-translator render JOB_ID --project project-name --json
```

1. `prepare` 获取视频并识别语音，返回 `status=prepared`、`job_id`、`detected_language`、`segments_path`、`translations_path` 和 `log_path`。原文语言默认自动识别，已知时可加 `--source-language en` 等语言代码。加 `--no-original-audio` 可以省略独立的原声音轨。
2. 通过 ChatGPT 登录的 Codex 使用 Pro 会员额度，从 `segments_path` 读取原文。每个片段有 `index`、`start`、`end` 和 `source_text`。短视频可通读全文，长视频可按话题分批，并共享人名、术语和前后文。在最后一轮中文定稿中同时判断疑难多音字，将正文和可选读音标记写入 `translations_path`，每个片段用原来的 `index` 作为 `id`。会员额度不足时暂停，API 额度始终禁用。
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

## 多音字读音

`pronunciations` 是可选的读音标记列表。Codex 在生成中文正文的同一次请求中，只为读法明确、容易误读的汉字添加标记。例如“得”在“你得记住”中读 děi，在“她得了第一名”中读 dé，在“说得很好”中读轻声 de。

```json
{"segments":[{"id":0,"text":"你得记住。","pronunciations":[{"start":1,"char":"得","pinyin":"dei3"}]}]}
```

- `start` 是目标字在该条 `text` 中从 0 开始的字符位置，空格和标点也计数。
- `char` 是这个位置的一个汉字，用来核对标记是否仍匹配正文。
- `pinyin` 使用小写拼音和声调数字，1～4 为四声，5 为轻声，ü 写成 v，例如 `dei3`、`de2`、`de5`、`lv4`。

普通句省略整个 `pronunciations` 字段。中文定稿后同步标注，随后修稿时同步更新位置和读音。相同汉字在一句里出现多次时按各自位置指定，字幕仍从 `text` 输出。旧译稿直接使用默认发音。

Kokoro 在本机把标记转换为音素，也就是模型使用的发音符号。标记随整句拼接、拆句和任务保存传递，读音改变会重新生成对应句子的配音。位置不匹配或重复的标记会在导入时报告错误，合法格式但本机词典不支持的读音会记录提示并按默认读音合成。生成音频无需云端额度。

## 重试与验收

`--json` 将任务结果写入标准输出，进度与诊断写入标准错误和任务日志。使用 `--project` 时，任务记录和中间文件写入 `data/projects/<项目名>/job/jobs/<job_id>/`，渲染预览写入同一项目的 `job/outputs/`。用任务返回的绝对路径定位文件。验收后，将选定版本和双语字幕归入对应项目的 `data/projects/<项目名>/delivery/`。

独立试跑默认使用 `.runtime/cli/`，可设置 `VT_DATA_DIR` 指定其他目录。同时指定 `--project` 和 `VT_DATA_DIR` 时以项目为准。未指定项目且未设置 `VT_DATA_DIR` 的 `render`，会先查找默认目录中的任务，找不到时再尝试读取已有的 `data/jobs/<job_id>/manifest.json`。旧脚本可以显式设置 `VT_DATA_DIR=./data` 继续使用旧目录。归档恢复按各项目 README 重建所需的兼容链接。

渲染失败时读取 `failed_stage`、`error` 和 `log_path`。若 `overflow_segments` 有数据，它会列出超时台词的片段编号、配音时长和时间槽长度。跨字幕片段的整句用 `cue_ids` 列出涉及的编号。缩短相应译文后，使用同一个任务 ID 再跑 `render`，会复用视频、转写和未改动的有效配音。每次成功渲染会把 MP4 和两份字幕一起写入新的版本目录，便于对照验收。旧任务若缺少原视频或转写文件，需要重新执行 `prepare`。

先用一段短片验收。确认 MP4 默认播放中文音轨，两份 SRT 都能加载，时间码在视频范围内，并抽听密集句和专业术语。项目的集成测试用真实 FFmpeg 和替代模型验证编排与输出：

```bash
.venv/bin/pytest
```

旧 `translate` 服务调用入口已停用。历史配置仍可读取，运行时会提示使用上述 Codex 流程。`passthrough` 仅供离线管线测试，可通过 `--project` 指定项目。
