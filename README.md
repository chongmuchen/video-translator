# Video Translator

端到端视频翻译与中文配音工具。当前目标是先完成一个可验证的单机 MVP：

```text
YouTube / Bilibili / 本地视频
  → 获取视频
  → 提取音频
  → 语音识别
  → 翻译为简体中文
  → 生成中文语音
  → 配音时长对齐
  → 原声压低或背景音分离
  → 混音、字幕和双音轨封装
  → 输出 MP4
```

本文既是使用说明，也是当前开发进度和逐步验收手册。测试时不要一上来就跑
两小时视频；先按本文顺序，用 5～15 秒样例逐层确认。

## 推荐入口：一键模板或分步执行

日常处理不需要再手动执行 7 次。启动网页：

```bash
make web
```

打开 <http://127.0.0.1:8000>，新建任务时保持：

```text
运行方式：按模板自动跑完 7 步
配置模板：Mac + Codex CLI + Edge TTS
```

第一次按需要展开模板修改，之后点击“保存为我上次的模板”。ASR、翻译、TTS、
对齐和混音配置保存在当前浏览器；云模型 API Key 加密保存在当前 Mac 的
系统“钥匙串访问”中，浏览器模板只记录不可逆推出密钥的引用。自动流程中途失败时，
已经成功的步骤仍会保留，可以从失败步骤单独重跑；翻译还会在每批完成后写入断点，
后续继续时不会重新消耗已经完成部分的模型额度。

命令行也可以一次跑完：

```bash
make auto \
  URL='视频URL' \
  ASR_BACKEND=mlx_whisper \
  ASR_MODEL=small.en \
  SOURCE_LANGUAGE=en \
  TRANSLATOR_PROVIDER=codex_cli
```

可把固定配置写入 `.env`，以后只需要：

```bash
make auto URL='视频URL'
```

开发、排错或质量检查时，再使用下面的分步入口。

根目录的 `main.py` 是推荐测试入口，它把每个阶段包成独立 Python 命令：

```bash
cd /Users/marscmchen/myspace/video-translator

# 查看全部流程及尚未完成的 TODO
.venv/bin/python main.py plan

# 一次只下载一个或多个视频
.venv/bin/python main.py download \
  "BILIBILI_URL_1" \
  "BILIBILI_URL_2" \
  "YOUTUBE_URL"
```

每个视频会返回独立任务 ID。然后一次只执行下一步：

```bash
.venv/bin/python main.py next JOB_ID
```

反复执行 `next`，流程会依次推进：

```text
download → extract → transcribe → translate → synthesize → align → mux
```

也可以明确指定一个步骤：

```bash
.venv/bin/python main.py step JOB_ID transcribe
```

常用步骤也提供了 Makefile 包装。例如，下载完成后抽取识别音频：

```bash
make extract JOB_ID='下载命令返回的任务ID'
```

查看状态：

```bash
.venv/bin/python main.py status JOB_ID
```

从断点连续执行剩余流程：

```bash
.venv/bin/python main.py resume JOB_ID
```

新建任务并一口气执行全部流程：

```bash
.venv/bin/python main.py run "VIDEO_URL"
```

若要使用 URL 文件批量测试：

```bash
cp test-videos.example.txt test-videos.txt
# 编辑 test-videos.txt，填入两个 B站和一个 YouTube 授权视频
.venv/bin/python main.py download --file test-videos.txt
```

`test-videos.txt` 已加入 `.gitignore`，不会误提交测试链接。未完成事项同时记录
在 `TODO.md`，并可通过 `main.py plan` 查看。

## Makefile 完整步骤与推荐参数

先查看全部命令：

```bash
make help
```

按步骤执行：

```bash
make download-one URL='视频或播客单集URL'
make extract JOB_ID='任务ID'
make transcribe JOB_ID='任务ID' ASR_MODEL=small SOURCE_LANGUAGE=en
make translate JOB_ID='任务ID'
make synthesize JOB_ID='任务ID'
make align JOB_ID='任务ID'
make mux JOB_ID='任务ID'
```

下载参数：

| Make 参数 | 默认值 | 推荐与说明 |
|---|---|---|
| `MAX_DOWNLOAD_HEIGHT` | `1080` | 普通翻译视频推荐 1080；只做语音验证可用 720。 |
| `DOWNLOAD_BACKEND` | `auto` | 推荐 `auto`；B站会优先使用 curl，其他站点使用 yt-dlp native。 |
| `COOKIES_FROM_BROWSER` | 空 | 公共内容留空；登录内容使用 `chrome`、`safari` 或 `firefox`。 |
| `DOWNLOAD_PROXY` | 空 | 使用系统网络；B站 TLS/412 问题可尝试 `direct`。 |
| `DOWNLOAD_IMPERSONATE` | 空 | 普通内容留空；风控时尝试 `chrome`。 |
| `PARTS` | 空 | 合集下载部分集，例如 `1-3,68`；不填表示明确下载全集。 |
| `SLEEP_BETWEEN` | `2` | 合集每集之间等待秒数，降低站点风控概率。 |
| `EXTRA` | 空 | 仅用于传入尚未封装的额外 CLI 参数。 |

后台并发参数（对应 `.env` 中的 `VT_*` 环境变量）：

| 环境变量 | 默认值 | 说明 |
|---|---:|---|
| `VT_WORKER_COUNT` | `3` | 视频任务总并发数；不同课程可同时推进。 |
| `VT_ASR_WORKER_COUNT` | `1` | 语音识别并发数；默认串行，避免 MLX/GPU 显存争用。 |
| `VT_TRANSLATION_WORKER_COUNT` | `2` | LLM 翻译并发数，兼顾吞吐和接口限流。 |
| `VT_MEDIA_WORKER_COUNT` | `2` | 抽取、对齐、混音和封装等 FFmpeg 阶段的并发数。 |

任务进入总线程池或等待阶段资源名额时显示“排队等待”；只有实际获得执行名额后才显示“执行中”。

识别参数：

| Make 参数 | 默认值 | 推荐与说明 |
|---|---|---|
| `ASR_BACKEND` | `faster_whisper` | 当前 Mac 网页推荐 `mlx_whisper` 使用 Apple GPU；兼容模式用 `faster_whisper`。 |
| `ASR_MODEL` | `small` | 多语言首次推荐 `small`；纯英语推荐 `small.en`；正式质量用 `large-v3`。 |
| `SOURCE_LANGUAGE` | 空 | 留空自动检测；明确英语填 `en`、中文填 `zh`、日语填 `ja`。 |
| `ASR_DEVICE` | `auto` | 仅 faster-whisper 使用：CPU 填 `cpu`，NVIDIA 机器可填 `cuda`。 |
| `ASR_COMPUTE_TYPE` | `auto` | 仅 faster-whisper 使用：CPU 推荐 `int8`，NVIDIA GPU 推荐 `float16`。 |
| `ASR_UNCLEAR_THRESHOLD` | `0.45` | 低于该置信度的语音不猜测，字幕标为 `【原音不清，未能可靠识别】`；原始识别候选仍保留在 `segments.json`。 |
| `ENABLE_DIARIZATION` | `false` | 可选说话人分离。正式使用前需安装 `.[diarization]` 并准备 Hugging Face Token。 |
| `DIARIZATION_MODEL` | `pyannote/speaker-diarization-3.1` | 默认 pyannote 官方模型；首次使用需要同意模型条款。 |
| `DIARIZATION_AUTH_TOKEN` | 空 | Hugging Face Token。网页/API 不会把它写进任务 manifest；命令行不要提交到 Git。 |
| `TRANSCRIBE_ARGS` | 空 | 已识别任务重新执行时填 `--force`。 |

翻译参数：

| Make 参数 | 默认值 | 推荐与说明 |
|---|---|---|
| `TARGET_LANGUAGE` | `简体中文` | 当前中文配音流程保持默认。 |
| `TRANSLATOR_PROVIDER` | `openai_compatible` | 可选 `codex_cli`、`ollama`、`kimi`、`minimax`、`deepseek`、通用兼容接口或 `passthrough`。 |
| `TRANSLATOR_BASE_URL` | 按 provider | Kimi、MiniMax、DeepSeek 会自动使用官方中国区预设；通用接口和 Ollama 可自行修改。 |
| `TRANSLATOR_MODEL` | 按 provider | 厂商选项会带推荐模型；仍可传入其他可用模型。 |
| `TRANSLATOR_API_KEY` | 空 | Codex CLI/Ollama 留空；云服务按需填写。网页模板可将它保存到 macOS 钥匙串，任务文件和接口响应均不含明文。 |
| `TRANSLATOR_TIMEOUT_SECONDS` | `180` | 长播客用 Codex CLI 时建议提高到 `300`～`600`；网页翻译步骤也可直接设置。 |
| `TRANSLATOR_RETRIES` | `2` | 翻译批次失败自动重试次数；超时、网络错误或无效 JSON 会重试，配置错误不会重试。 |
| `TRANSLATOR_RETRY_BACKOFF_SECONDS` | `3` | 首次重试等待秒数，后续按指数退避，例如 3、6、12 秒。 |
| `TRANSLATION_BATCH_SIZE` | Codex `48`，其他 `12` | Codex Plus 推荐用较大批次减少调用次数；接口超时或输出漏项时调小到 `24` 或 `12`。 |
| `TRANSLATOR_CODEX_BIN` | `codex` | Codex CLI 命令或绝对路径。 |
| `TRANSLATOR_CODEX_STRATEGY` | `balanced` | `economy`=`gpt-5.4-mini`/低推理，`balanced`=`gpt-5.4-mini`/中推理，`quality`=`gpt-5.5`/中推理；书籍默认 `quality`。 |
| `TRANSLATOR_CODEX_MODEL` | 空 | 仅 Codex CLI 使用；填写后覆盖策略所选模型。一般保持空。 |
| `GLOSSARY` | 空 | JSON 术语表文件路径。 |
| `TRANSLATE_ARGS` | 空 | 重新翻译时填 `--force`。 |

配音、对齐与封装参数：

| Make 参数 | 默认值 | 推荐与说明 |
|---|---|---|
| `TTS_PROVIDER` | `edge` | 首次推荐 Edge；本地/克隆声音可选 CosyVoice。 |
| `TTS_VOICE` | `zh-CN-XiaoxiaoNeural` | Edge 中文女声推荐值。 |
| `TTS_RATE` / `TTS_VOLUME` | `+0%` | 先保持默认，避免与后续时间轴加速叠加。 |
| `TTS_HTTP_URL` | 空 | 仅通用 HTTP TTS 使用。 |
| `COSYVOICE_BASE_URL` | `http://127.0.0.1:50000` | 仅 CosyVoice 使用。 |
| `COSYVOICE_MODE` | `sft` | 首次推荐 `sft`；其他模式需参考音频/指令配置。 |
| `SPEAKER_VOICE_MAP` | 空 | 说话人到音色映射，例如 JSON `{"SPEAKER_00":"zh-CN-XiaoxiaoNeural"}` 或 `SPEAKER_00=voiceA,SPEAKER_01=voiceB`。 |
| `DUB_SAMPLE_RATE` | `24000` | 中文语音推荐 24 kHz。 |
| `MAX_TEMPO_FACTOR` | `1.8` | 不是统一 1.8 倍：先按 1.0，只有读不完的片段才加速，最高不超过 1.8。 |
| `KEEP_ORIGINAL_AUDIO` | `true` | 视频推荐保留第二条原声音轨。 |
| `DUCK_ORIGINAL_AUDIO` | `true` | 推荐开启，中文讲话时自动压低原声。 |
| `BURN_SUBTITLES` | `false` | 推荐软字幕；播放器兼容性不好时再烧录。 |
| `ENABLE_DEMUCS` | `false` | 实验性且耗时，确认安装 Demucs 后再启用。 |
| `ENABLE_LIP_SYNC` | `false` | 可选口型同步。需要自己安装 Wav2Lip/MuseTalk 等外部工具。 |
| `LIP_SYNC_COMMAND` | 空 | 外部命令模板，支持 `{video}`、`{muxed}`、`{audio}`、`{subtitles}`、`{output}`、`{ffmpeg}`、`{ffprobe}`。 |

每一步正常重复执行会自动跳过或从缺失部分继续。需要覆盖旧结果时，对应的
`*_ARGS` 传 `--force`；它会让该步骤之后的任务状态失效并要求重新执行。

继续/重试规则：

- 不带 `--force`：复用已经完成的前置步骤和本地文件。例如翻译失败后再执行
  `make translate`，会保留 `segments.json` 里已有译文，只翻译还没有
  `translated_text` 的片段。
- 只调整 `TRANSLATION_BATCH_SIZE` 或 `TRANSLATOR_TIMEOUT_SECONDS`：适合不带
  `--force` 继续，常用于长播客 Codex 超时。
- 修改目标语言、术语表、翻译模型或翻译提供方：建议加 `TRANSLATE_ARGS='--force'`，
  否则可能出现前半段旧参数、后半段新参数的混合译文。
- 强制重跑某一步会清掉该步骤及后续步骤状态。例如强制重跑 `transcribe` 后，
  `translate/synthesize/align/mux` 都需要重新执行。

## 1. 当前开发进度

状态说明：

- ✅ 已实现，并有自动化或真实媒体集成测试。
- 🟡 已实现，但仍依赖外部站点、模型或人工验收。
- ⬜ 尚未实现。

| 流程 | 状态 | 当前实现 | 已有验证 | 尚需验证 |
|---|---:|---|---|---|
| 本地媒体输入 | ✅ | CLI 复制常见视频及 MP3/M4A/AAC/WAV/FLAC/OGG/OPUS | 视频端到端及纯音频 download→extract 集成测试 | 不同编码和超长媒体 |
| YouTube/B站输入 | 🟡 | `yt-dlp`、Deno、Cookies、域名和时长限制 | URL 安全单元测试 | 使用已授权真实链接人工验收 |
| Apple Podcasts | 🟡 | Apple Lookup API→发布者 RSS；支持列目录、最新 N 集、指定集、全集下载，以及纯音频中文混音/原声双音轨 M4A 输出 | 真实节目成功读取 359 集 RSS；本地音频提取测试；纯音频 M4A 封装集成测试 | 更长节目和更多播放器兼容性 |
| 音频提取 | ✅ | FFmpeg 输出 16 kHz 单声道 WAV | 真实 FFmpeg 集成测试 | 无音轨、损坏媒体等异常样本 |
| 语音识别 | 🟡 | 可选 MLX Apple GPU 或 faster-whisper CPU/CUDA；词级时间戳、片段合并、低置信度特殊占位及原始候选保留；可选 pyannote 说话人分离；占位时间槽不朗读猜测内容 | 两个适配器、模糊片段静音和说话人匹配自动化测试；本机 MLX/Metal tiny.en 真实推理通过 | `large-v3` 质量、阈值、长视频性能和 pyannote 权重授权验收 |
| 中文翻译 | 🟡 | Codex CLI、Ollama、Kimi、MiniMax、DeepSeek 和通用 OpenAI-compatible 接口；Plus 分档、批次上下文、术语表和逐批断点 | JSON/ID/断点校验、云端预设、Codex CLI 真实短句翻译 | 仍需用完整长视频人工评价译文质量 |
| Edge 中文 TTS | ✅ | 单一中文音色或按 `speaker` 映射多音色，逐片段生成；支持超时、重试、指数退避和过长译文自动缩写重合成 | 本机真实语音生成、TTS 重试/缩写路径和 speaker→voice 映射自动化测试 | 更多声音和长节目人工听感调优 |
| 通用 HTTP TTS | 🟡 | JSON 请求，返回音频字节 | 代码已实现 | 尚未连接真实服务 |
| CosyVoice | 🟡 | 兼容官方 FastAPI 的 SFT、zero-shot、cross-lingual、instruct 接口 | PCM→WAV 封装已实现 | 需要独立 CosyVoice 服务和授权声音验收 |
| 配音时长对齐 | ✅ | 重采样、`atempo` 加速、补静音、裁剪、时间轴拼接；超长片段会先尝试缩写译文再合成 | 单元和端到端集成测试 | 极端口播密度的人工听感调优 |
| 原声自动压低 | ✅ | 根据完整识别时间槽生成控制轨，再用 FFmpeg sidechain compression；中文提前结束也不会释放英文尾音 | 控制轨边界单元测试、真实 FFmpeg 集成测试 | 不同节目类型的参数调优 |
| 人声/BGM 分离 | 🟡 | 可选 Demucs `no_vocals` | 调用代码已实现 | 依赖未默认安装，尚未做模型验收 |
| 字幕与双音轨 MP4 | ✅ | 中文软字幕、中文配音默认音轨、可选原声音轨；可选调用外部 lip-sync 命令生成最终视频 | 自动检查 1 视频 + 2 音频 + 1 字幕流；lip-sync 命令模板单元覆盖 | 更多播放器兼容性；具体 lip-sync 模型权重验收 |
| CLI | ✅ | 原 CLI 加根目录 `main.py`：批量下载、分步执行、状态、断点恢复、完整运行 | 冒烟测试及本地 download→extract 验证 | 更完善的交互式界面 |
| Web/API | ✅ | 视频/书籍/论文播客任务列表、模板自动执行、钥匙串密钥、参数说明、日志、片段预览、选中片段重译/重配音、取消任务和输出下载 | API 自动化测试、真实历史任务加载 | 合集批量操作和更强的生产权限系统 |
| EPUB 书籍翻译 | 🟡 | 按 spine 抽取、逐批翻译、保留图片/CSS/链接；纯译文或段落双语；重建导航文本 | 含图片和目录链接的 EPUB 自动化测试 | 大型复杂 EPUB 与不同阅读器人工验收 |
| PDF 书籍翻译 | 🟡 | 内置固定页数覆盖排版、保留原页图片、中文字体嵌入、纯译文或块内双语、目录标题更新；扫描 PDF 可通过 OCRmyPDF 生成文字层，也可选 Docling 结构化抽取；复杂 PDF 可用 BabelDOC/PDFMathTranslate 外部引擎 | 真实 PDF 生成、页数/目录自动检查和逐页渲染目检 | 极端密排/低清晰扫描件仍需人工校样；Marker/PaddleOCR 可作为后续增强后端 |
| 书籍中间缓存 | ✅ | 导入→抽取→翻译→排版独立执行；文本块、译文哈希、逐批断点、版本输出和阅读库元数据长期保留；支持标签、阅读状态、优先级、质量评分、排序过滤和过期中间文件清理 | 改排版模式不重复翻译、清理命令、阅读库元数据和排序 API 自动化测试 | 版本迁移策略 |
| 任务状态与日志 | ✅ | 单机 JSON manifest、每任务日志、中间文件和 SQLite 任务队列记录 | JobStore、任务队列和 API 单元测试 | 多实例部署时迁移到 Redis/Celery |
| 多说话人/多音色 | 🟡 | 论文播客支持双主持人 A/B 音色；视频翻译可选 pyannote diarization，并按 `SPEAKER_00=voice` 映射音色 | 论文播客多音色、视频 speaker 分配和 TTS 映射自动化测试 | pyannote 模型下载、授权条款和长视频人工验收 |
| 口型同步 | 🟡 | 已支持外部命令模板调用 Wav2Lip/MuseTalk 等工具，默认关闭 | 命令模板、输出路径和 mux 集成逻辑自动化测试 | 需要用户安装具体模型并验收画面质量 |
| 生产任务系统 | 🟡 | 单机线程池执行；SQLite 记录队列/运行结果；支持合作式取消、步骤指标、质量报告、授权声明、每日配额、审计事件、删除任务和中间文件清理 | API/CLI 自动化测试 | 多用户权限、Redis/Celery、外部数据库和对象存储 |

当前自动化测试基线：**84 项测试通过**。

## 2. 每个任务的中间产物

每个任务都有独立目录，调试时应逐层检查，而不是只看最终 MP4：

```text
data/
├── jobs/{待下载-source-hint|title}--{job_id}/
│   ├── manifest.json       # 当前状态、进度、错误和文件路径
│   ├── pipeline.log        # FFmpeg 和各阶段日志
│   ├── source.mp4          # 下载或复制的原视频
│   ├── speech-16k.wav      # ASR 输入
│   ├── segments.json       # 原文、译文、时间戳和 TTS 文件
│   ├── zh-CN.srt           # 中文字幕
│   ├── tts/                # 原始逐句中文配音
│   ├── aligned/            # 已适配时间槽的逐句配音
│   ├── dub-timeline.wav    # 完整中文配音时间轴
│   └── separated/          # 启用 Demucs 后的人声分离结果
├── collections/
│   └── {collection_id}.json  # 合集分集与独立任务 ID 对照
├── books/jobs/{title}--{book_id}/
│   ├── source.pdf|epub       # 本地保存的原书
│   ├── book-manifest.json    # 书籍步骤、模式、错误和输出路径
│   ├── blocks.json           # 文本块、原文、译文和翻译缓存哈希
│   └── extracted/            # EPUB 解包结构等可复用中间产物
├── books/outputs/
│   └── {title}-{book_id}-zh.pdf|epub
└── outputs/
    └── {title}-{job_id}.mp4
```

远程媒体的标题要在读取视频信息后才能确定，因此任务会先创建一个可读的
`待下载-{站点或来源提示}--{完整任务ID}` 目录和 `manifest.json`；下载成功后
自动改为 `{视频标题}--{完整任务ID}`。后续命令仍只需传任务 ID，同名视频也不会
互相覆盖；旧版的纯 ID 目录仍然兼容。网页任务详情会直接显示任务目录、
`manifest.json`、`pipeline.log` 和已生成产物的绝对路径。

任务状态依次为：

```text
queued
→ downloading
→ downloaded
→ extracting
→ extracted
→ transcribing
→ transcribed
→ translating
→ translated
→ synthesizing
→ synthesized
→ aligning
→ aligned
→ muxing
→ completed / failed
```

## 3. 第一步：安装和环境检查

需要 Python 3.10～3.13，推荐 Python 3.12。

```bash
cd /Users/marscmchen/myspace/video-translator
./scripts/bootstrap.sh
cp .env.example .env
```

如果脚本找不到合适的 Python：

```bash
PYTHON=/path/to/python3.12 ./scripts/bootstrap.sh
```

bootstrap 会：

1. 创建或复用 `.venv`；
2. 安装项目、`yt-dlp`、Deno、`faster-whisper`、Edge TTS 和测试依赖；Apple Silicon 还会安装 `mlx-whisper`；
3. 在系统没有 FFmpeg 时，将 FFmpeg/ffprobe 下载到项目的 `.runtime/`。

运行检查：

```bash
.venv/bin/video-translator doctor
```

验收条件：

- `ffmpeg`、`ffprobe`、`yt-dlp`、当前选择的 ASR 后端、`edge-tts` 和 Deno 为 `✓`；
- 数据目录可用；
- 在尚未启动 Ollama 时，只有“翻译服务”显示 `✗` 是预期现象。

如果 FFmpeg 缺失：

```bash
.venv/bin/video-translator bootstrap-media
```

## 4. 第二步：按模块运行自动化测试

不要先运行全量测试。按流水线顺序执行，哪一步失败就先停在那一步。

### 4.1 输入链接和安全限制

```bash
.venv/bin/pytest tests/test_downloader.py -v
```

验证内容：

- 接受 YouTube、`youtu.be`、Bilibili、`b23.tv`；
- 拒绝 `file://`、IP、localhost、账号密码和任意域名；
- 该测试不真正下载视频。

### 4.2 ASR 分段整理

```bash
.venv/bin/pytest tests/test_transcriber.py -v
```

验证内容：

- 相邻短句按时间间隔合并；
- 已结束的完整句子不会错误合并。

该测试不下载 Whisper 模型。

### 4.3 翻译结果校验

```bash
.venv/bin/pytest tests/test_translator.py -v
```

验证内容：

- 从模型返回内容中提取 JSON；
- 按片段 ID 写回译文；
- 任何缺失片段都会导致失败，避免字幕悄悄丢句。
- Codex CLI 使用只读、临时会话和 JSON Schema；
- Kimi、MiniMax、DeepSeek 的默认地址与模型正确。

该测试不调用真实 LLM。

### 4.4 时间轴、字幕和任务状态

```bash
.venv/bin/pytest \
  tests/test_media.py \
  tests/test_store.py \
  tests/test_stepwise.py \
  -v
```

验证内容：

- SRT 时间格式；
- `atempo` 链；
- 静音填充和总时长；
- manifest 的原子写入和状态恢复。
- 分步执行顺序和强制重跑后的下游失效。

### 4.5 真实 FFmpeg 封装

```bash
.venv/bin/pytest tests/test_ffmpeg_integration.py -v
```

该测试会临时生成一个视频，执行真实混音和 MP4 封装。

验收条件：

- 输出包含 1 个视频流；
- 包含中文混音和原声两个音频流；
- 包含 1 个中文字幕流。

### 4.6 不依赖外部模型的端到端编排

```bash
.venv/bin/pytest tests/test_pipeline_integration.py -v
```

该测试使用假的 ASR、翻译和 TTS，但使用真实 FFmpeg，验证：

```text
本地视频
→ 音频提取
→ 分段数据
→ 字幕
→ TTS 文件
→ 对齐
→ 混音
→ 最终 MP4
```

它证明程序编排已连通，但不能证明真实模型的识别和翻译质量。

### 4.7 全量测试

前面全部通过后再运行：

```bash
.venv/bin/pytest
```

当前预期：

```text
84 passed
```

## 5. 第三步：验证真实中文 TTS

先只测 TTS，不运行完整视频。

```bash
mkdir -p work/acceptance

.venv/bin/edge-tts \
  --voice zh-CN-XiaoxiaoNeural \
  --text "你好，这是视频翻译工具的中文配音测试。" \
  --write-media work/acceptance/tts-zh.mp3
```

定位项目 FFprobe：

```bash
FFPROBE="$(find .runtime -type f -name ffprobe | head -1)"
"$FFPROBE" \
  -v error \
  -show_entries format=duration \
  -of default=nw=1 \
  work/acceptance/tts-zh.mp3
```

验收条件：

- 文件存在且大小不为 0；
- ffprobe 能读出大于 0 的时长；
- 人工播放时中文清晰、没有截断。

失败时检查：

- 网络是否可访问 TTS 服务；
- `.env` 中的 `VT_TTS_VOICE` 是否有效；
- 是否被代理、限流或证书设置拦截。

## 6. 第四步：验证真实 Whisper

第一次运行会下载模型。先使用 `tiny` 验证线路，不要直接下载 `large-v3`。

先生成一段测试语音：

```bash
.venv/bin/edge-tts \
  --voice en-US-AriaNeural \
  --text "Hello, this is a short speech recognition test." \
  --write-media work/acceptance/asr-en.mp3
```

运行真实识别：

```bash
.venv/bin/python - <<'PY'
from faster_whisper import WhisperModel

model = WhisperModel("tiny", device="cpu", compute_type="int8")
segments, info = model.transcribe(
    "work/acceptance/asr-en.mp3",
    language="en",
    vad_filter=True,
    word_timestamps=True,
)

print("language:", info.language)
for segment in segments:
    print(f"[{segment.start:.2f} -> {segment.end:.2f}] {segment.text}")
PY
```

验收条件：

- 模型能够加载；
- 能输出 `en` 和带起止时间的文本；
- tiny 模型文字可能不完全准确，这一步只验证线路。

质量测试再依次尝试：

```dotenv
VT_ASR_MODEL=small
```

最终生产配置：

```dotenv
VT_ASR_MODEL=large-v3
```

Apple Silicon 可以改用 `VT_ASR_BACKEND=mlx_whisper` 走 Metal GPU。
长视频正式使用前仍应记录处理耗时和内存，再决定模型大小。

## 7. 第五步：配置并验证真实翻译

网页的翻译步骤可以直接切换以下后端：

| 选择 | 默认地址 / 模型 | 数据位置 |
|---|---|---|
| 本机 Codex CLI | 复用 `codex` 当前登录和默认模型 | CLI 在本机运行；模型通常仍在 OpenAI 云端 |
| Ollama | `127.0.0.1:11434/v1` / `qwen3:8b` | 完全本地 |
| Kimi API | `api.moonshot.cn/v1` / `kimi-k2.6` | 字幕发送给 Kimi |
| MiniMax API | `api.minimaxi.com/v1` / `MiniMax-M2.7` | 字幕发送给 MiniMax |
| DeepSeek API | `api.deepseek.com` / `deepseek-v4-flash` | 字幕发送给 DeepSeek |
| 其他 OpenAI-compatible | 自行填写 | 取决于所填服务 |

预设按 2026-06-30 的官方文档核对；网页中的地址和模型都可编辑。

### 7.1 使用本机 Codex CLI

先检查 CLI 和登录态：

```bash
codex --version
codex login status
```

运行某个已完成语音识别的任务：

```bash
make translate \
  JOB_ID='任务ID' \
  TRANSLATOR_PROVIDER=codex_cli \
  TRANSLATOR_CODEX_STRATEGY=balanced
```

这里复用的是 `codex login` 保存的 ChatGPT 登录态；用 Plus 登录时走 Plus
方案的 Codex 使用额度，不要求购买 OpenAI Platform API token。三档策略为：

| 策略 | 模型和推理强度 | 推荐用途 |
|---|---|---|
| `economy` | `gpt-5.4-mini` + low | 先跑草稿、超长但不重要的内容，最节省额度 |
| `balanced` | `gpt-5.4-mini` + medium | 视频字幕默认；大批次减少请求次数，质量与额度平衡 |
| `quality` | `gpt-5.5` + medium | 书籍、术语密集内容、最终定稿 |
| `account_default` | 账号当前默认模型 + medium | 不希望项目固定模型时使用 |

较高推理强度会更快消耗方案限额，因此当前不把 `high` 作为默认值。程序每批
翻译后立即保存 `segments.json`；中断后重跑只翻未完成片段。视频 Codex 默认
每批 48 段，书籍质量模式默认每批 24 个文本块。方案限额仍受账号、任务大小和
服务端规则影响，不等同于无限使用。

一般通过策略选模型。确实需要手动覆盖时：

```bash
make translate \
  JOB_ID='任务ID' \
  TRANSLATOR_PROVIDER=codex_cli \
  TRANSLATOR_CODEX_MODEL='你的 Codex 账号支持的模型'
```

程序通过非交互式 `codex exec` 调用，启用 `read-only`、`--ephemeral`
和 JSON Schema。每一批翻译都是独立临时会话，不会修改项目文件，也不会把
会话 rollout 保存下来。它会复用 Codex CLI 已保存的登录，不需要在网页填写
OpenAI API Key。

注意：“本机 Codex CLI”表示命令在本机启动，不表示模型离线运行；字幕内容
通常会发送给 OpenAI。如果内容不能离开电脑，应选择 Ollama。

### 7.2 使用 Ollama

Ollama 的配置为：

```dotenv
VT_TRANSLATOR_PROVIDER=ollama
VT_TRANSLATOR_BASE_URL=http://127.0.0.1:11434/v1
VT_TRANSLATOR_MODEL=qwen3:8b
VT_TRANSLATOR_API_KEY=
```

可以连接 Ollama、LM Studio 或兼容云服务。以 Ollama 为例，在终端 A 启动：

```bash
ollama serve
```

在终端 B 下载模型：

```bash
ollama pull qwen3:8b
```

然后测试接口：

```bash
curl http://127.0.0.1:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3:8b",
    "temperature": 0.2,
    "messages": [
      {
        "role": "user",
        "content": "只返回 JSON：{\"segments\":[{\"id\":0,\"text\":\"你好，世界。\"}]}"
      }
    ]
  }'
```

再次运行：

```bash
.venv/bin/video-translator doctor
```

验收条件：

- curl 返回 HTTP 200；
- 返回内容包含 `choices[0].message.content`；
- `doctor` 的“翻译服务”变为 `✓`。

### 7.3 使用 Kimi、MiniMax 或 DeepSeek

Makefile 会根据 provider 自动选择上述地址和推荐模型。例如：

```bash
export KIMI_API_KEY='你的密钥'
make translate \
  JOB_ID='任务ID' \
  TRANSLATOR_PROVIDER=kimi \
  TRANSLATOR_API_KEY="$KIMI_API_KEY"
```

MiniMax 和 DeepSeek 分别把 provider 改成 `minimax`、`deepseek`。也可以直接
在网页中选择厂商并填写 API Key。保存模板时，密钥写入当前用户的 macOS
钥匙串；模板只保存随机引用，接口不会回显，任务 manifest 也不会写入明文。
切换厂商或更新密钥后重新保存模板即可。命令行参数不会自动进入钥匙串，仍不要把
真实密钥提交到 Git。

厂商地址或模型变更时，可以显式覆盖：

```bash
make translate \
  JOB_ID='任务ID' \
  TRANSLATOR_PROVIDER=openai_compatible \
  TRANSLATOR_BASE_URL='https://你的兼容接口/v1' \
  TRANSLATOR_MODEL='模型名' \
  TRANSLATOR_API_KEY="$API_KEY"
```

官方参考：[Codex 方案与额度](https://developers.openai.com/codex/pricing)、
[Codex 模型](https://developers.openai.com/codex/models)、
[Codex 非交互模式](https://developers.openai.com/codex/noninteractive)、
[Kimi API](https://platform.kimi.com/docs/guide/start-using-kimi-api)、
[MiniMax OpenAI 兼容接口](https://platform.minimaxi.com/docs/api-reference/text-openai-api)、
[DeepSeek API](https://api-docs.deepseek.com/)。

## 8. 第六步：制作一个本地端到端测试视频

先使用本地 5～15 秒视频排除 YouTube/B站变量。

定位项目 FFmpeg：

```bash
FFMPEG="$(find .runtime -type f -name ffmpeg | head -1)"
FFPROBE="$(find .runtime -type f -name ffprobe | head -1)"
test -x "$FFMPEG" && test -x "$FFPROBE"
printf 'FFmpeg: %s\nFFprobe: %s\n' "$FFMPEG" "$FFPROBE"
```

生成英文测试语音：

```bash
.venv/bin/edge-tts \
  --voice en-US-AriaNeural \
  --text "Hello. This short video explains how an automatic translation tool works." \
  --write-media work/acceptance/source-en.mp3
```

生成测试视频：

```bash
"$FFMPEG" -y \
  -f lavfi -i "color=c=0x243447:s=1280x720:r=25" \
  -i work/acceptance/source-en.mp3 \
  -shortest \
  -c:v libx264 \
  -pix_fmt yuv420p \
  -c:a aac \
  work/acceptance/source-en.mp4
```

为了快速验证，把 `.env` 暂时设置为：

```dotenv
VT_ASR_BACKEND=mlx_whisper
VT_ASR_MODEL=tiny
VT_TRANSLATOR_PROVIDER=codex_cli
VT_TRANSLATOR_CODEX_STRATEGY=economy
VT_TTS_PROVIDER=edge
VT_TTS_VOICE=zh-CN-XiaoxiaoNeural
```

如果已经启动 Ollama，也可以把翻译配置换成：

```dotenv
VT_TRANSLATOR_PROVIDER=ollama
VT_TRANSLATOR_BASE_URL=http://127.0.0.1:11434/v1
VT_TRANSLATOR_MODEL=qwen3:8b
```

运行完整流程：

```bash
.venv/bin/video-translator translate \
  work/acceptance/source-en.mp4 \
  --source-language en
```

当前本机已用 15 秒短视频完成一次真实验收：faster-whisper tiny.en →
Codex CLI economy → Edge TTS → 对齐 → 双音轨 MP4，输出文件位于
`data/outputs/local-short-95a83c81.mp4`。如果你清理了 `data/`，按本节步骤
重新跑一遍即可。

验收顺序：

1. 查看命令最终输出的 MP4 路径；
2. 找到最新任务目录：

   ```bash
   JOB_DIR="$(ls -td data/jobs/* | head -1)"
   echo "$JOB_DIR"
   ```

3. 查看状态：

   ```bash
   .venv/bin/python -m json.tool "$JOB_DIR/manifest.json"
   ```

4. 确认以下文件存在：

   ```bash
   test -s "$JOB_DIR/speech-16k.wav"
   test -s "$JOB_DIR/segments.json"
   test -s "$JOB_DIR/zh-CN.srt"
   test -s "$JOB_DIR/dub-timeline.wav"
   find "$JOB_DIR/tts" -type f
   find "$JOB_DIR/aligned" -type f
   ```

5. 检查原文、译文和时间戳：

   ```bash
   .venv/bin/python -m json.tool "$JOB_DIR/segments.json"
   ```

6. 检查最终媒体流：

   ```bash
   OUTPUT="$(ls -t data/outputs/*.mp4 | head -1)"
   "$FFPROBE" \
     -v error \
     -show_entries stream=index,codec_type:stream_tags=language,title \
     -of json \
     "$OUTPUT"
   ```

7. 用 VLC、IINA 或其他支持多音轨的播放器人工验收：

   - 默认音轨是中文；
   - 可以切换到原声音轨；
   - 中文字幕时间基本同步；
   - 中文讲话时原声被压低；
   - 视频总时长没有明显变化；
   - 中文句尾没有被裁掉。

如果失败，先看：

```bash
tail -100 "$JOB_DIR/pipeline.log"
```

不要跳过本地样例直接测试网络视频。

## 9. 第七步：验证 Web 和 API

### 9.1 启动本地网页

推荐使用 Makefile：

```bash
cd /Users/marscmchen/myspace/video-translator
make web
```

等价命令：

```bash
.venv/bin/video-translator serve
```

然后打开 <http://127.0.0.1:8000>。

网页是本地流水线控制台，直接读取和修改项目目录下的 `data/`：

- 左侧统一显示视频、播客/音频和 PDF/EPUB 书籍历史任务，并可按类型过滤；
- 新建任务会先落盘 `manifest.json` 和任务目录，再提交下载或自动 7 步；
- 新建任务可选“一键模板”自动连续执行全部 7 步，也可切回只下载的分步模式；
- 模板记住上一次 ASR、翻译、TTS、对齐和混音配置；API Key 存入 macOS
  钥匙串，浏览器模板只保存随机引用；
- 下载、抽取、识别、翻译、配音、对齐、封装均有独立按钮；
- 翻译页可选 Codex CLI、Ollama、Kimi、MiniMax、DeepSeek 或自定义兼容接口；
- “翻译 PDF / EPUB”可上传书籍，选择纯译文或原文/译文相邻排版，一键或分步执行；
- 每个步骤都显示参数含义、默认值、推荐值和等价 Make 命令；
- 页面可以查看任务目录、`manifest.json`、`pipeline.log`、每一步进度、
  继续/强制重跑规则、中间产物路径、最终下载入口和带起止时间的识别片段；
- 语音识别步骤可选 pyannote 说话人分离；配音步骤可配置 speaker→voice 映射；
- 封装步骤可选外部 lip-sync 命令模板，把 Wav2Lip/MuseTalk 等工具接到最终 MP4；
- 页面左侧可开关“任务完成叮一声”，默认开启；设置只保存在当前浏览器；
- API 默认只监听 `127.0.0.1`，任务和媒体文件保存在本机。

注意：当前后台执行器仍是单机 Web 服务进程内的线程池；SQLite 会持久记录队列、
开始/结束时间和失败信息，但不会在服务重启后自动恢复正在运行的 Python 线程。
重启 `make web` 会中断正在运行的下载、翻译或排版线程；已经写入
`manifest.json`、`segments.json`、`blocks.json` 和输出文件的中间结果会保留。
视频任务可从失败步骤继续；翻译步骤会保留 `segments.json` 中已有译文，只处理
未翻译片段。书籍任务可点击“继续完成全部”或重新执行翻译步骤，已翻译且缓存键
未变化的文本块会跳过。页面会在任务详情中显示当前属于“继续”还是“强制重跑”。

当前网页覆盖的步骤：

```text
download → extract → transcribe → translate → synthesize → align → mux
book import → extract → translate → render
```

Mac 参数说明：网页可以选择 `mlx-whisper` 使用 Apple GPU/Metal，也可以选择
`faster-whisper` 走 CPU。后者的 CTranslate2 GPU 后端仅支持 NVIDIA CUDA，
因此不要在 Mac 上给 faster-whisper 填 `cuda`。

### 9.2 验证 API

验证健康检查：

```bash
curl http://127.0.0.1:8000/api/health
```

预期：

```json
{
  "status": "ok",
  "storage": "local",
  "data_directory": "/Users/marscmchen/myspace/video-translator/data"
}
```

创建任务并只执行下载：

```bash
curl http://127.0.0.1:8000/api/jobs/staged \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.youtube.com/watch?v=AUTHORIZED_VIDEO_ID",
    "options": {
      "target_language": "简体中文",
      "keep_original_audio": true,
      "burn_subtitles": false,
      "glossary": {}
    },
    "settings": {
      "max_download_height": 1080,
      "download_backend": "auto"
    }
  }'
```

列出本机历史任务并查看状态：

```bash
curl http://127.0.0.1:8000/api/jobs
curl http://127.0.0.1:8000/api/jobs/{job_id}
curl http://127.0.0.1:8000/api/jobs/{job_id}/log
curl http://127.0.0.1:8000/api/tasks
curl http://127.0.0.1:8000/api/audit/events
```

如果在 `.env` 开启 `VT_REQUIRE_CONTENT_AUTHORIZATION=true`，新建任务需要声明内容
授权。API 可在创建请求中加入：

```json
{
  "authorization": {
    "authorized": true,
    "rights_basis": "owned",
    "notes": "本地测试素材"
  }
}
```

`rights_basis` 可填写 `owned`、`licensed`、`public_domain`、`fair_use` 或
`other`。每日新建任务配额可用 `VT_MAX_JOBS_PER_DAY` 设置；`0` 表示不限制。
审计事件保存在本机 `data/governance.sqlite3`，任务队列记录保存在
`data/task-queue.sqlite3`，这两个文件已加入 `.gitignore`。

执行音频抽取：

```bash
curl -X POST \
  http://127.0.0.1:8000/api/jobs/{job_id}/steps/extract \
  -H "Content-Type: application/json" \
  -d '{"force":false,"options":{},"settings":{}}'
```

使用 Mac 推荐参数执行英语识别：

```bash
curl -X POST \
  http://127.0.0.1:8000/api/jobs/{job_id}/steps/transcribe \
  -H "Content-Type: application/json" \
  -d '{
    "force": false,
    "options": {"source_language": "en"},
    "settings": {
      "asr_backend": "mlx_whisper",
      "asr_model": "small.en"
    }
  }'
```

预览前 50 个识别片段：

```bash
curl http://127.0.0.1:8000/api/jobs/{job_id}/segments
```

最终视频完成后下载：

```bash
curl -L -o translated.mp4 \
  http://127.0.0.1:8000/api/jobs/{job_id}/download
```

删除任务和本机输出文件：

```bash
curl -X DELETE \
  'http://127.0.0.1:8000/api/jobs/{job_id}?delete_outputs=true'

curl -X DELETE \
  'http://127.0.0.1:8000/api/books/{book_id}?delete_outputs=true'

curl -X DELETE \
  'http://127.0.0.1:8000/api/paper-podcasts/{podcast_id}?delete_outputs=true'
```

正在运行的任务会返回 HTTP 409，避免删除后台还在写入的文件。`delete_outputs=false`
时只移除任务记录和任务目录中可安全删除的中间产物，不会碰原始外部路径。

安全检查：

```bash
curl http://127.0.0.1:8000/api/jobs/staged \
  -H "Content-Type: application/json" \
  -d '{"url":"http://127.0.0.1/private"}'
```

预期返回 HTTP 400，而不是访问本机地址。

## 9A. PDF / EPUB 书籍翻译

网页点击“＋ 翻译 PDF / EPUB”，选择文件后可配置：

- 输出模式：
  - `pdf2zh_bing_mono` / `pdf2zh_bing_dual`：PDFMathTranslate 高保真，
    Bing 免 Key，输出纯译文或分页双语 PDF。分页双语通常是原文页和译文页分开，
    不等同于同页左右对照；
  - `pdf2zh_bing_facing`：复用 PDFMathTranslate 的分页双语结果，把每对页面
    无损合成同一张宽页，左侧原文、右侧中文；适合大书快速生成左右分页版；
  - `pdf2zh_google_mono` / `pdf2zh_google_dual`：PDFMathTranslate 高保真，
    Google 免 Key，适合和 Bing 输出对比；
  - `babeldoc_bing_mono` / `babeldoc_bing_dual`：BabelDOC 后端，高保真
    论文引擎；`dual` 是同页左右对照，是当前原文+译文并排阅读的推荐模式；
  - `pdf2zh_openailiked_*` / `babeldoc_openailiked_*`：使用网页上方配置的
    OpenAI-compatible 接口，可接 Kimi 等兼容服务；
  - `pdf2zh_ollama_*` / `babeldoc_ollama_*`：使用本机 Ollama；
  - `pdf2zh_deepseek_*` / `babeldoc_deepseek_*`：使用 DeepSeek API；
  - `pdf2zh_minimax_*` / `babeldoc_minimax_*`：使用 MiniMax API；
  - `translated_only`：普通书籍，只保留中文译文；
  - `bilingual`：普通书籍/固定版 PDF，原文 + 译文对照；
  - `paper_translated_reflow`：内置草稿，纯译文连续重排；
  - `paper_translated_reference`：论文排版，按原 PDF 页序输出纯译文并插入原页标记；
  - `paper_bilingual_stacked`：论文排版，原文在上、译文在下的上下对照重排；
  - `paper_reference`：论文排版，参考原文页序生成左英文、右中文的对照阅读 PDF；
  - `paper_reflow`：论文排版，把论文当作一篇新文章连续重排，仍保持左英文、右中文对照。
- 翻译后端：与视频相同，支持 Codex CLI、Ollama、Kimi、MiniMax、DeepSeek
  和通用 OpenAI-compatible 接口；
- 执行方式：一键执行抽取、翻译、排版，或一步一步执行并检查中间结果；
- Codex 策略：书籍默认 `quality`，重要出版内容建议保持该值；
- 术语表：继续使用 JSON 对象，保证人名、书名、技术词汇前后一致。

论文建议使用 PDF；EPUB 只支持 `translated_only` 和 `bilingual`。对
`Attention Is All You Need` 这类公式、图表、双栏论文，如果只要译文优先试
`pdf2zh_bing_mono`；如果要快速得到左原文、右译文，选
`pdf2zh_bing_facing`；如果希望专业引擎原生重排左右对照，再试
`babeldoc_bing_dual`。
专业 PDF 模式会绕过项目内置 `blocks.json` 翻译缓存，由 PDFMathTranslate /
BabelDOC 自行解析、翻译和重排，这样才能尽量保留公式、图、表和原 PDF 版式。
内置 `paper_*` 模式只是草稿/纯文字兜底，不适合作为严肃论文最终输出。
已有任务不需要重新导入：打开任务详情，在“用所选模式重新排版”旁边选择排版模式，
点击按钮即可生成新版本；已生成过的版本会在详情页列出独立下载链接。

命令行一键运行：

```bash
make book-run \
  BOOK_FILE='/绝对路径/source.epub' \
  BOOK_MODE=bilingual \
  BOOK_PROVIDER=codex_cli \
  BOOK_CODEX_STRATEGY=quality
```

扫描版 PDF 一键运行：

```bash
# 默认 auto：普通 PDF 不 OCR；扫描版没有文字时自动 OCR
make book-run \
  BOOK_FILE='/绝对路径/scanned.pdf' \
  BOOK_MODE=translated_only \
  BOOK_OCR_MODE=auto \
  BOOK_OCR_LANGUAGES=eng

# 中英混排扫描件
make book-run \
  BOOK_FILE='/绝对路径/scanned-cn-en.pdf' \
  BOOK_MODE=translated_only \
  BOOK_OCR_MODE=auto \
  BOOK_OCR_LANGUAGES=chi_sim+eng
```

论文高保真一键运行：

```bash
make book-run \
  BOOK_FILE='/绝对路径/paper.pdf' \
  BOOK_MODE=pdf2zh_bing_mono \
  BOOK_PROVIDER=codex_cli \
  BOOK_CODEX_STRATEGY=quality \
  TRANSLATOR_TIMEOUT_SECONDS=600
```

`TRANSLATOR_TIMEOUT_SECONDS=600` 会让专业 PDF 引擎最长等待约 60 分钟
（内部上限约为该值 × 6）。Llama 3 Herd 这类 90 页以上论文使用
PDFMathTranslate/BabelDOC + Bing 免 Key 时，默认 180 秒配置对应的约 18 分钟
可能不够。

首次建议在网页测试一篇 5～12 页论文：

1. 选择 PDF；
2. 输出模式先选 `BabelDOC · Bing免Key · 左右对照`；
3. 如果只要译文，再切换为 `PDFMathTranslate · Bing免Key · 纯译文`；
4. 如需对比分页双语，再切换为 `PDFMathTranslate · Bing免Key · 分页双语`；
5. 下载各版本，用预览并排比较公式、图、表、脚注和双栏区域。

命令行分步测试普通书籍：

```bash
# 1. 导入；记下返回的书籍任务 ID
make book-import \
  BOOK_FILE='/绝对路径/source.pdf' \
  BOOK_MODE=translated_only

# 2. 抽取结构和文本
make book-extract BOOK_ID='书籍任务ID'

# 3. 翻译；每个模型批次都会写入 blocks.json
make book-translate \
  BOOK_ID='书籍任务ID' \
  BOOK_PROVIDER=codex_cli \
  BOOK_CODEX_STRATEGY=quality

# 4. 从缓存译文排版；切换模式不会重新调用模型
make book-render \
  BOOK_ID='书籍任务ID' \
  BOOK_MODE=translated_only

# 5. 同一份译文缓存，再生成普通双语版用于对比
make book-render \
  BOOK_ID='书籍任务ID' \
  BOOK_MODE=bilingual

make book-status BOOK_ID='书籍任务ID'
```

中间文件保存在 `data/books/jobs/{书名}--{ID}/`。`blocks.json` 为可复用的
文本块缓存；缓存键包含原文、目标语言、术语表、provider、模型和 Codex 策略。
这些会影响译文的值不变时，失败后继续或重新排版不会重复读取、翻译前面的内容，
可以明显减少 Plus/API 额度消耗。

阅读库排序/标注 API：

```bash
curl -X PATCH \
  http://127.0.0.1:8000/api/books/{book_id}/library \
  -H "Content-Type: application/json" \
  -d '{
    "tags": ["transformer", "must-read"],
    "favorite": true,
    "reading_status": "reading",
    "priority": 90,
    "quality_score": 95,
    "summary": "注意力机制经典论文",
    "glossary": {"attention": "注意力"}
  }'

curl 'http://127.0.0.1:8000/api/books?tag=transformer&reading_status=reading&sort_by=quality_score&descending=true'
```

`reading_status` 可用 `unread`、`reading`、`translated`、`reviewing`、
`done`、`archived`；`priority` 和 `quality_score` 都是 0～100。
这套字段会保存在 `book-manifest.json` 的 `metadata.library`，网页历史任务
可以直接复用来做论文阅读队列和排序。

排版处理原则：

- EPUB 按 OPF spine 读取章节，保留原图、CSS、资源和内部链接；目录使用链接而非
  固定页码，翻译目录标题后仍指向原章节位置；
- 扫描版 PDF 会先尝试正常抽取文本；没有可抽取文本时，`BOOK_OCR_MODE=auto`
  会调用 OCRmyPDF 生成带文字层的中间 PDF，再复用现有翻译/排版流程。中间文件保存在
  `data/books/jobs/{书名}--{ID}/ocr/`，包括 `source-ocr.pdf`、`source-ocr.txt`
  和 `ocrmypdf.log`；
- 如果选择 `BOOK_OCR_BACKEND=docling`，会使用 Docling 的结构化文档转换能力导出
  阅读顺序 Markdown/JSON，再把 Markdown 段落转成可翻译块。中间文件保存在
  `data/books/jobs/{书名}--{ID}/structured-ocr/docling/`，包括 `document.md`
  和 `document.json`；
- 专业 PDF 模式调用 PDFMathTranslate / BabelDOC；首次运行会通过 `uv` 准备
  Python 3.12 隔离环境、DocLayout 模型和中文字体。Fast 后端通常更快且不会额外
  加页眉说明；BabelDOC 后端更接近新一代语义/版面 IR，但可能在页顶加入来源说明；
- 内置 PDF 引擎保持原页尺寸和页数，在原文本区域内重新排中文，图片留在原页；
  在 macOS 上优先使用 Hiragino Sans GB / Heiti 等可读中文字体，PDF outline
  目录标题会随译文更新；
- 因 PDF 采用固定页数，原页码不会发生漂移；内容过密无法在最低字号内装下时，
  任务 metadata 会记录 `layout_warnings`，应人工检查对应页面；
- 复杂彩色背景、环绕图文、多栏扫描件和低清晰度扫描仍需人工校样。OCRmyPDF
  基于 Tesseract，主要解决“先让扫描 PDF 有文字可翻译”；Docling 更适合阅读顺序、
  表格和结构化导出。Marker/PaddleOCR 这类更重的后端仍保留为后续增强选项。严谨
  出版的最终版本应逐页检查，工具不能承诺自动排版在所有原书上完全无误。

OCR 依赖安装：

```bash
# macOS
brew install tesseract tesseract-lang ghostscript
./scripts/bootstrap.sh
```

OCR 模式说明：

| 选项 | 含义 | 推荐场景 |
| --- | --- | --- |
| `auto` | 先正常抽取；没有文字时才 OCR | 默认推荐 |
| `always` | 强制对 PDF 重新 OCR | 原 PDF 文字层很差、错乱或不可用 |
| `never` | 禁止 OCR | 快速测试，或不想调用外部 OCR |

OCR 语言：

- 英文论文：`eng`
- 简体中文：`chi_sim`
- 中英混排：`chi_sim+eng`

OCR 后端：

| 后端 | 命令参数 | 适合场景 | 依赖 |
| --- | --- | --- | --- |
| OCRmyPDF | `BOOK_OCR_BACKEND=ocrmypdf` | 扫描 PDF 先生成文字层，稳定、省心 | `brew install tesseract tesseract-lang ghostscript` |
| Docling | `BOOK_OCR_BACKEND=docling` | 需要更好的阅读顺序、表格/结构化 Markdown/JSON | `pip install '.[structured-ocr]'`，若 Python 版本不兼容建议单独环境 |

示例：

```bash
make book-run \
  BOOK_FILE='/path/scanned.pdf' \
  BOOK_OCR_MODE=auto \
  BOOK_OCR_BACKEND=docling \
  BOOK_MODE=translated_only
```

论文排版当前开发进展：

- 已完成：`pdf2zh_*` 专业模式。它调用 PDFMathTranslate fast 后端，真实测试已能
  保留样例论文中的图形框和公式文本，并同时产出 mono/dual 两个版本。
- 已完成：`babeldoc_*` 专业模式。它调用 PDFMathTranslate 的 BabelDOC 后端，并
  为当前 NumPy 2 兼容问题加入子进程补丁；真实测试已能跑通并产出 mono/dual 版本。
- 已完成：网页新建任务和历史任务详情都支持选择专业引擎；同一任务生成过的多个
  输出版本会保留独立下载链接。
- 已完成：`paper_translated_reflow`。这是当前推荐的论文阅读版，去掉双语对照带来
  的列宽损失，把译文排成连续文章，适合 Attention/Transformer 这类双栏论文。
- 已完成：`paper_translated_reference`。按原 PDF 页序组织纯译文，并插入“原 PDF
  第 N 页”标记，适合需要和原文页码对照的精读场景。
- 已完成：`paper_bilingual_stacked`。原文小字号在上、译文正常字号在下，比左右
  对照更占空间，但比窄列左右对照更可读。
- 已完成：`paper_reference`。它按原 PDF 页序插入“原 PDF 第 N 页”标记，输出
  左侧英文原文、右侧中文译文的 A4 阅读 PDF。适合短段落对照；双栏长论文不再推荐
  首选这个模式。
- 已完成：`paper_reflow`。它去掉目录碎片、页眉页脚等页内家具，把论文正文连续
  排成“像新文章”的左右对照版。适合短论文对照；长论文优先用纯译文重排。
- 已完成：所有论文模式都走现有抽取、翻译、缓存、历史任务和下载链路；
  `book-render` 切换论文模式只重新生成 PDF，不重新翻译。任务详情会记录已经生成过
  的每一种论文版本，便于下载对比。
- 已完成：如果原 PDF 页面含图片/图表，任务 metadata 会记录排版警告，提醒最终版
  对照原 PDF 校样。
- 已完成：扫描版 PDF 的 OCRmyPDF 文字层生成、自动抽取 fallback，以及 Docling
  结构化 Markdown/JSON 抽取后端。
- 已完成：批量论文/书籍阅读库的标签、收藏、摘要、术语表、阅读状态、优先级、
  质量评分和排序过滤 API。公式、图表、表格、
  脚注和双栏浮动体应优先使用 `pdf2zh_*` / `babeldoc_*` 专业模式；扫描件如果需要
  更强的公式/版面语义识别，后续可继续接 Marker/PaddleOCR，而不是只依赖内置
  `paper_*` 草稿模式。

### 9B. 论文音频博客 / 讲解播客

这个功能用于“先听懂论文”，不是翻译整本 PDF。当前流程是：

```text
PDF → 抽取论文文本 → 生成中文讲解笔记 → 生成/对比播客脚本 → TTS 合成 MP3 → 可选合成讲解视频 MP4
```

网页入口：左侧点“论文讲解播客”。推荐第一轮这样试：

1. 上传一篇文本型 PDF，例如 Attention Is All You Need；
2. 风格选择“双人深度讲解”；
3. 脚本生成后端优先选 Ollama，本地模型可用 `qwen3:8b`、Qwen2.5/3
   系列或其他中文能力较好的开源模型；如果想比较质量，可在“脚本候选模型”里填
   `qwen3:8b,qwen3:14b` 这类逗号分隔列表；
4. 如果已经部署 CosyVoice，语音后端选 CosyVoice；否则先用 Edge TTS 看效果；
5. 勾选“导入后自动生成脚本和音频”，等待历史任务显示“已完成”后下载 MP3；
   如果同时勾选“生成讲解视频”，会把论文封面/图表页渲染为幻灯片并合成 MP4。

中间文件保存在：

```text
data/paper-podcasts/jobs/{论文名}--{ID}/
├── paper-text.md          # PDF 抽取文本
├── paper-blocks.json      # 按页/块保存的原文
├── paper-notes.json       # 分块阅读笔记
├── podcast-script.json    # 可机读脚本
├── podcast-script.md      # 可人工编辑的讲解稿
├── script-quality.json    # 脚本启发式评分、风险提示和事实核查提示
├── script-comparison.json # 可选：多模型候选脚本对比报告
└── tts/                   # 分句语音片段

data/paper-podcasts/outputs/
├── {论文名}-paper-podcast-{style}-{ID}.mp3
└── {论文名}-paper-podcast-{style}-{ID}.mp4  # 可选讲解视频
```

继续 / 重试规则：

- 已抽取文本会复用；
- 讲解脚本的缓存键包含论文文本、目标语言、风格、时长、术语表、provider、模型和
  Codex 策略；如果启用候选模型对比，候选模型列表也会进入缓存键。这些不变时
  继续执行不会重复生成脚本；
- 更换音色或 TTS 后端时，可以只重跑“3. 音频”，不会重新生成脚本；
- 如果是扫描版 PDF，建议先在书籍翻译里使用 OCRmyPDF 或 Docling 抽取；论文播客
  当前仍以文本型 PDF 抽取为主，后续可继续复用同一套结构化 OCR 后端。

备用命令行：

```bash
make paper-podcast-run \
  PAPER_FILE='/绝对路径/paper.pdf' \
  PAPER_PODCAST_PROVIDER=ollama \
  PAPER_PODCAST_MODEL='qwen3:8b' \
  PAPER_PODCAST_TTS_PROVIDER=edge

# 比较多个本地模型的脚本质量，并自动选择评分最高的脚本继续合成
make paper-podcast-run \
  PAPER_FILE='/绝对路径/paper.pdf' \
  PAPER_PODCAST_PROVIDER=ollama \
  PAPER_PODCAST_MODEL='qwen3:8b' \
  PAPER_PODCAST_COMPARE_MODELS='qwen3:8b,qwen3:14b' \
  PAPER_PODCAST_SCRIPT_BACKEND=notebooklm

# 同时生成讲解视频
make paper-podcast-run \
  PAPER_FILE='/绝对路径/paper.pdf' \
  PAPER_PODCAST_MAKE_VIDEO=true
```

开源优先建议：

- 脚本：Ollama + Qwen 系列 / 其他中文能力强的开源模型；
- 语音：CosyVoice 自部署，或接入你自己的 HTTP TTS；
- Edge TTS 只是省心兜底，不是开源模型。

PDF / 论文翻译开源引擎调研：

- [OCRmyPDF](https://ocrmypdf.readthedocs.io/)：第一版已接入。它把 OCR
  文字层写回扫描 PDF，生成可搜索、可抽取文本的 PDF；底层使用 Tesseract。优点是稳、
  与现有 PDF 翻译链路兼容；局限是 Tesseract 不擅长复杂阅读顺序、段落结构、手写体、
  低质量扫描和表格/公式语义。
- [Docling](https://github.com/docling-project/docling)：适合作为第二阶段结构化抽取。
  它支持扫描 PDF 和图片 OCR、阅读顺序、表格结构、图表理解，并可导出 Markdown/JSON。
- [Marker](https://github.com/datalab-to/marker)：适合作为“PDF/图片 → Markdown/JSON”
  的高级抽取引擎，支持表格、公式、图片保存，也支持 GPU/CPU/MPS；但许可证和商业使用
  需要单独评估。
- [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)：OCR/文档解析能力强，
  支持 100+ 语言、PP-Structure、PDF/图片到结构化数据；适合后续做中文扫描件、表格
  和复杂版面的增强 OCR。
- [PDFMathTranslate](https://github.com/PDFMathTranslate/PDFMathTranslate)：
  Python 项目，定位是保留公式、图表、目录和注释的 PDF 翻译，提供命令行、UI
  和 Docker。README 中的本地命令行示例是 `uv tool install --python 3.12 pdf2zh`
  后执行 `pdf2zh document.pdf`。它适合优先作为“高质量 PDF 引擎”接入。
- [BabelDOC](https://github.com/funstory-ai/BabelDOC)：定位为科学论文 PDF 翻译和
  双语对照库，核心思路是把 PDF 的视觉版面信息和语义文本解耦，再做术语、上下文、
  公式占位和自适应排版。它非常适合后续作为“保留原版式/双语论文引擎”接入。
- [PDFMathTranslate-next](https://github.com/PDFMathTranslate/PDFMathTranslate-next)：
  基于 BabelDOC 的下一代实现，可作为研究 BabelDOC 调用方式和自部署 UI 的参考。

网页 PDF 引擎选项：

```text
内置书籍引擎 / 内置论文排版 / PDFMathTranslate 高保真引擎 / BabelDOC 高保真引擎
```

商业化建议是两条线并行：

1. “高保真产品线”：默认使用 PDFMathTranslate/BabelDOC，重点解决复杂公式、图表、
   表格、双栏版面、注释和原 PDF 视觉还原。
2. “阅读产品线”：保留当前内置 `paper_*` 草稿模式，后续优化批量论文管理、术语
   一致性、阅读体验和可重复渲染。

最小验收顺序：

1. 导入后确认格式、标题和任务目录；
2. 抽取后打开 `blocks.json`，抽查章节顺序、页码和原文；
3. 翻译中途停止一次，再继续，确认已完成块没有重新翻译；
4. 普通书籍用 `translated_only` / `bilingual` 各排一次；
5. 论文需要原文+译文并排时，大书优先用 `pdf2zh_bing_facing`，原生重排可用
   `babeldoc_bing_dual`；只看译文时用 `pdf2zh_bing_mono`；确实想要原文页/
   译文页前后交替时再用 `pdf2zh_bing_dual`；
6. EPUB 用 Apple Books/Calibre 检查目录跳转、图片和段落；PDF 用预览检查目录、
   页数、图片、中文字体、溢出警告和至少每章一页的译文准确度。

## 10. 第八步：验证 YouTube/B站输入

只使用你拥有版权、取得授权或依法允许处理的视频。

推荐先用 Python 包装入口下载三个测试视频：

```bash
cp test-videos.example.txt test-videos.txt
# 将占位符替换成两个 B站 URL 和一个 YouTube URL

.venv/bin/python main.py download \
  --file test-videos.txt \
  --cookies-from-browser chrome
```

公开视频通常不需要 Cookies，可以先去掉 `--cookies-from-browser`；但 YouTube
触发“Sign in to confirm you're not a bot”时，公开视频也需要读取已登录浏览器的
Cookies。可在命令中添加 `--cookies-from-browser chrome`，或持久写入 `.env`：

```dotenv
VT_COOKIES_FROM_BROWSER=chrome
```

每个成功结果应显示：

```text
状态:       downloaded
已完成步骤: download
下一步:     extract
视频:       .../data/jobs/{title}--{job_id}/source.mp4
```

逐个检查：

```bash
.venv/bin/python main.py status JOB_ID
```

确认下载没问题后，只提取音频：

```bash
make extract JOB_ID='JOB_ID'
```

它等价于：

```bash
.venv/bin/python main.py step JOB_ID extract
```

预期状态变为 `extracted`，并在任务目录生成供 Whisper 使用的 16 kHz
单声道 PCM 文件：

```text
data/jobs/视频标题--JOB_ID/speech-16k.wav
```

再次执行时，已完成的抽取步骤会被安全跳过。如果源文件变化、确实需要重新抽取：

```bash
make extract JOB_ID='JOB_ID' EXTRACT_ARGS='--force'
```

如果日志出现：

```text
SSL: UNEXPECTED_EOF_WHILE_READING
```

说明 B站媒体 CDN 的 TLS 连接被代理或网络设备提前断开。项目的 `auto` 模式
现在会为 B站媒体自动使用系统 curl，并启用断点续传、10 次重试和 10 MB
分块。对已有失败任务重试：

```bash
.venv/bin/python main.py step JOB_ID download \
  --force \
  --download-backend curl
```

如果出现 `HTTP Error 412: Precondition Failed`，这是 B站风控拒绝元数据请求。
稍后重试，并提供浏览器登录态：

```bash
.venv/bin/python main.py step JOB_ID download \
  --force \
  --download-backend curl \
  --cookies-from-browser chrome \
  --impersonate chrome
```

若 Chrome Cookies 解密在 macOS 上卡住，完全退出 Chrome 后重试，或者导出
Netscape 格式的 `cookies.txt`：

```bash
.venv/bin/python main.py step JOB_ID download \
  --force \
  --download-backend curl \
  --cookie-file /absolute/path/to/cookies.txt \
  --impersonate chrome
```

当前 macOS 开启全局代理、B站媒体仍发生 TLS 错误时，可仅为这次任务直连：

```bash
.venv/bin/python main.py step JOB_ID download \
  --force \
  --download-backend curl \
  --proxy direct \
  --cookie-file /absolute/path/to/cookies.txt
```

以下原始 `yt-dlp` 命令仅用于下载器故障排查。先只读取元数据，不下载：

```bash
.venv/bin/yt-dlp \
  --simulate \
  --no-playlist \
  --js-runtimes "deno:$PWD/.venv/bin/deno" \
  "AUTHORIZED_VIDEO_URL"
```

验收条件：

- 能识别标题和时长；
- 没有播放列表意外展开；
- YouTube 没有提示缺失 JavaScript runtime。

登录后才能访问、且你有权处理的视频，可在 `.env` 配置：

```dotenv
VT_COOKIES_FROM_BROWSER=chrome
```

或者：

```dotenv
VT_COOKIE_FILE=/absolute/path/to/cookies.txt
```

然后运行完整流程：

```bash
.venv/bin/python main.py resume JOB_ID
```

先测试 1 分钟以内的视频。确认本地流程稳定后再逐步增加到 5、15、30 分钟。

### B站分P/合集下载

合集使用“一个分 P = 一个独立任务”，避免 68 集共用一个巨大任务。即使传入
带 `?p=68` 的 URL，合集脚本也会先读取完整目录。

只列出分集，不下载：

```bash
.venv/bin/python scripts/download_collection.py \
  "https://www.bilibili.com/video/BV1pG6xBrEct/?p=68" \
  --list-only
```

该示例已真实验证能读取 68 个分 P。

下载第 68 集：

```bash
.venv/bin/python scripts/download_collection.py \
  "https://www.bilibili.com/video/BV1pG6xBrEct/" \
  --parts 68
```

下载第 1～3 集和第 68 集：

```bash
.venv/bin/python scripts/download_collection.py \
  "https://www.bilibili.com/video/BV1pG6xBrEct/" \
  --parts 1-3,68 \
  --sleep-between 2
```

明确下载全集：

```bash
.venv/bin/python scripts/download_collection.py \
  "https://www.bilibili.com/video/BV1pG6xBrEct/" \
  --all \
  --sleep-between 2
```

仅仅省略 `p` 不会立即下载全集。没有 `--all` 或 `--parts` 时，脚本只预览
目录，防止误下载几十集。

也可以通过根入口执行同一功能：

```bash
.venv/bin/python main.py download-collection \
  "BILIBILI_COLLECTION_URL" \
  --parts 1-3,68
```

登录内容追加：

```bash
--cookies-from-browser chrome --impersonate chrome
```

下载完成后，`data/collections/{collection_id}.json` 会记录每个分 P 对应的
任务 ID。每一集后续仍独立推进：

```bash
.venv/bin/python main.py next JOB_ID
.venv/bin/python main.py resume JOB_ID
```

### Apple Podcasts（苹果播客）

Apple Podcasts 网页在部分地区会跳转，直接用 yt-dlp 可能拿不到音频。本项目
使用 Apple Lookup API 找到发布者 RSS，再下载 RSS 中的原始音频。

只列出节目，不下载：

```bash
make podcast \
  URL='https://podcasts.apple.com/us/podcast/the-tim-dillon-show/id1135137367'
```

该示例已真实验证能读取 359 集节目目录。

下载最新一集：

```bash
make podcast \
  URL='https://podcasts.apple.com/us/podcast/the-tim-dillon-show/id1135137367' \
  PODCAST_ARGS='--latest 1'
```

按列表序号下载第 1～3 集：

```bash
make podcast \
  URL='APPLE_PODCAST_SHOW_URL' \
  PODCAST_ARGS='--episodes 1-3'
```

下载 RSS 中全部节目：

```bash
make podcast \
  URL='APPLE_PODCAST_SHOW_URL' \
  PODCAST_ARGS='--all --sleep-between 2'
```

也可以直接运行：

```bash
.venv/bin/python scripts/download_apple_podcast.py \
  'APPLE_PODCAST_SHOW_OR_EPISODE_URL' \
  --latest 1
```

文件保存在：

```text
work/podcasts/{节目名称}/
├── 001-{节目标题}.mp3
└── download-summary.json
```

将下载后的音频送入翻译流水线：

```bash
.venv/bin/python main.py download \
  'work/podcasts/{节目名称}/001-{节目标题}.mp3'

.venv/bin/python main.py next JOB_ID
```

对于前面的 Apple Podcasts 单集链接，可以完整地分两步测试：

```bash
# 第一步：下载；终端会打印任务 ID
make download-one \
  URL='https://podcasts.apple.com/cn/podcast/relentless-geekery/id1519329964?i=1000745497980'

# 第二步：把上一条命令打印的任务 ID 填到这里
make extract JOB_ID='JOB_ID'
```

成功后应看到：

```text
状态:       extracted
已完成步骤: download, extract
下一步:     transcribe
识别音频:   .../data/jobs/Episode-233--JOB_ID/speech-16k.wav
```

### 语音识别与时间戳

抽取完成后，可以选择 MLX Apple GPU 或 faster-whisper。当前 Mac 首次验证
推荐 MLX；英文节目明确指定 `SOURCE_LANGUAGE=en` 可以减少语言检测偏差：

```bash
make transcribe \
  JOB_ID='JOB_ID' \
  ASR_BACKEND=mlx_whisper \
  ASR_MODEL=small.en \
  SOURCE_LANGUAGE=en
```

这条命令的完整形式是：

```bash
make transcribe \
  JOB_ID='任务ID' \
  ASR_BACKEND=mlx_whisper \
  ASR_MODEL=small \
  SOURCE_LANGUAGE=en \
  ASR_UNCLEAR_THRESHOLD=0.45 \
  TRANSCRIBE_ARGS=''
```

参数说明：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `JOB_ID` | 无，必填 | 下载步骤返回的 32 位任务 ID。这里填写任务 ID，不是带标题的目录名。程序会用 ID 自动找到 `视频标题--ID` 目录。 |
| `ASR_BACKEND` | `faster_whisper` | `mlx_whisper` 使用 Apple GPU/Metal；`faster_whisper` 使用 CPU 或 NVIDIA CUDA。网页在当前 Mac 上默认选择 MLX。 |
| `ASR_MODEL` | `small` | 使用的 Whisper 模型。模型越大通常越准确，但下载、内存占用和识别时间也越大。 |
| `SOURCE_LANGUAGE` | 空 | 原音频语言代码。`en` 是英语、`zh` 是中文、`ja` 是日语、`ko` 是韩语。留空时自动检测。它不是目标翻译语言。 |
| `ASR_DEVICE` | `auto` | 只对 faster-whisper 生效。`auto` 会在存在 NVIDIA CUDA 时选择 `cuda`，否则选择 `cpu`。 |
| `ASR_COMPUTE_TYPE` | `auto` | 只对 faster-whisper 生效。CPU 推荐 `int8`，NVIDIA CUDA 推荐 `float16`。 |
| `ASR_UNCLEAR_THRESHOLD` | `0.45` | 综合词概率、平均 log probability 和无语音概率得到置信度。低于阈值时不让翻译模型猜测，中文字幕显示 `【原音不清，未能可靠识别】`。设为 `0` 可关闭。 |
| `ENABLE_DIARIZATION` | `false` | 是否在 ASR 后继续跑说话人分离。开启后会给 `segments.json` 的片段写入 `speaker`，后续 TTS 可按说话人选不同音色。 |
| `DIARIZATION_MODEL` | `pyannote/speaker-diarization-3.1` | pyannote 模型名。首次使用需要在 Hugging Face 同意模型条款，并确保本机能下载权重。 |
| `DIARIZATION_AUTH_TOKEN` | 空 | Hugging Face Token。也可写入环境变量 `VT_DIARIZATION_AUTH_TOKEN`；不建议写进 shell 历史或提交到 Git。 |
| `TRANSCRIBE_ARGS` | 空 | 传给分步执行器的附加参数。当前最常用的是 `--force`，用于重新执行已经完成的识别步骤。 |

常用模型：

| 模型 | 适用场景 | 取舍 |
|---|---|---|
| `tiny` / `base` | 检查安装、模型下载和命令是否正常 | 最快，但不适合正式字幕 |
| `small` | 首次识别长音频，支持多语言 | 速度、内存与准确率比较平衡 |
| `small.en` | 只包含英语的节目 | 英语专用；不要用于多语言音频 |
| `medium` | 对准确率要求更高 | 比 `small` 更慢、更占内存 |
| `large-v3` | 正式成片、优先保证识别质量 | 下载和统一内存占用较大 |
| `turbo` | 希望接近大模型质量但提高速度 | 比 `large-v3` 快，准确率可能略有下降 |

项目当前使用的几个内部参数不需要在 Make 命令中填写：

| 内部参数 | 当前值 | 作用 |
|---|---:|---|
| `vad_filter` | `true` | faster-whisper 使用 Silero VAD；MLX 当前使用 Whisper 自带的无语音判断。 |
| `word_timestamps` | `true` | 两个后端都计算词级时间对齐；当前输出再整理为片段级起止时间。 |
| `condition_on_previous_text` | `true` | 识别后续语音时参考前文，改善连续讲话的上下文。 |
| `max_gap` | `0.45` 秒 | 两个短片段间隔不超过该值时，允许合并。 |
| `max_duration` | `12` 秒 | 合并后的翻译片段最长持续时间。 |
| `max_chars` | `180` 字符 | 合并后原文的最大字符数。 |

第一次运行会下载对应后端的 Whisper 模型。MLX 模型来自 MLX Community，
使用 Apple GPU/Metal；进度和错误记录在任务目录的 `pipeline.log`。
被标记为模糊的片段仍在 `segments.json` 保存 `raw_source_text`、置信度和
`asr_unclear=true`，方便后续人工修正；占位文本不会发送给翻译模型，也不会被
中文 TTS 朗读，该时间槽会生成静音，只在字幕中提示观众。

需要多说话人配音时，先安装并启用 diarization：

```bash
.venv/bin/pip install -e ".[diarization]"

make transcribe \
  JOB_ID='JOB_ID' \
  ENABLE_DIARIZATION=true \
  DIARIZATION_AUTH_TOKEN='hf_...' \
  SOURCE_LANGUAGE=en
```

识别完成后检查 `segments.json` 中是否出现 `speaker` 字段。然后按说话人配置音色：

```bash
make synthesize \
  JOB_ID='JOB_ID' \
  SPEAKER_VOICE_MAP='{"SPEAKER_00":"zh-CN-XiaoxiaoNeural","SPEAKER_01":"zh-CN-YunxiNeural"}'
```

如果某个片段没有 speaker 或没有映射，会回退到 `TTS_VOICE` 默认音色。

当前这台 Apple Silicon 机器可以明确写成：

```bash
make transcribe \
  JOB_ID='6320d1982dec44f185f58977ee841f9a' \
  ASR_BACKEND=mlx_whisper \
  ASR_MODEL=small.en \
  SOURCE_LANGUAGE=en
```

需要切回兼容模式时：

```bash
make transcribe \
  JOB_ID='JOB_ID' \
  ASR_BACKEND=faster_whisper \
  ASR_MODEL=small.en \
  SOURCE_LANGUAGE=en \
  ASR_DEVICE=cpu \
  ASR_COMPUTE_TYPE=int8 \
  TRANSCRIBE_ARGS='--force'
```

识别过程中可以在另一个终端查看日志：

```bash
tail -f \
  data/jobs/*--6320d1982dec44f185f58977ee841f9a/pipeline.log
```

识别结果保存在：

```text
data/jobs/视频标题--JOB_ID/segments.json
```

每段包含以秒为单位的开始和结束时间：

```json
[
  {
    "index": 0,
    "start": 0.52,
    "end": 4.87,
    "source_text": "Welcome to the show.",
    "translated_text": null
  }
]
```

程序启用了 Whisper 词级时间戳；faster-whisper 还会启用 Silero VAD。
两个后端都会将过短片段合并成最长约 12 秒的翻译单元，最终写入适合翻译、
字幕和配音对齐的片段级时间戳。

查看前 5 个识别片段：

```bash
jq '.[0:5] | map({index, start, end, source_text})' \
  data/jobs/*--JOB_ID/segments.json
```

成功后应显示 `状态: transcribed`、`下一步: translate`。提高正式成片质量时，
可以强制使用 `large-v3` 重新识别：

```bash
make transcribe \
  JOB_ID='JOB_ID' \
  ASR_BACKEND=mlx_whisper \
  ASR_MODEL=large-v3 \
  SOURCE_LANGUAGE=en \
  TRANSCRIBE_ARGS='--force'
```

不加 `--force` 时，已完成的识别步骤会被跳过。使用 `--force` 会重置
`transcribe` 以及翻译、配音、对齐和封装等后续步骤的任务状态；后续步骤需要
重新执行。

当前纯音频已经支持：

```text
download → extract → transcribe → translate → synthesize → align → mux
```

最后的 `mux` 会输出 `.m4a`：第一条音轨是中文配音与被压低的原声混音，
第二条音轨是可选原声。中文字幕仍作为 `zh-CN.srt` 保存在任务目录，
因为多数播放器对 M4A 内嵌字幕支持不稳定。

## 11. 第九步：可选验证 CosyVoice

CosyVoice 应运行在独立 Conda/Docker 环境，不要直接混入主项目环境。

普通 SFT 音色：

```dotenv
VT_TTS_PROVIDER=cosyvoice
VT_COSYVOICE_BASE_URL=http://127.0.0.1:50000
VT_COSYVOICE_MODE=sft
VT_TTS_VOICE=中文女
VT_COSYVOICE_SAMPLE_RATE=22050
```

先直接验证 CosyVoice 服务：

```bash
curl -X POST \
  http://127.0.0.1:50000/inference_sft \
  -F "tts_text=你好，这是 CosyVoice 测试。" \
  -F "spk_id=中文女" \
  -o work/acceptance/cosyvoice.pcm

test -s work/acceptance/cosyvoice.pcm
```

将裸 PCM 转成可播放 WAV：

```bash
"$FFMPEG" -y \
  -f s16le \
  -ar 22050 \
  -ac 1 \
  -i work/acceptance/cosyvoice.pcm \
  work/acceptance/cosyvoice.wav
```

zero-shot 模式：

```dotenv
VT_COSYVOICE_MODE=zero_shot
VT_COSYVOICE_PROMPT_WAV=/absolute/path/to/authorized-prompt.wav
VT_COSYVOICE_PROMPT_TEXT=参考音频的准确文本
```

只能使用本人或已明确授权的参考声音。

CosyVoice 单句通过后，再重新执行第 8 节的本地端到端视频测试。

## 12. 第十步：可选验证 Demucs

安装：

```bash
.venv/bin/pip install -e ".[separation]"
```

确认模块可用：

```bash
.venv/bin/python -m demucs --help
```

启用：

```dotenv
VT_ENABLE_DEMUCS=true
```

重新执行本地短视频测试。验收：

- `$JOB_DIR/separated/` 下存在 `no_vocals.wav`；
- 最终视频保留背景声，原语言人声显著降低；
- 没有不可接受的音乐破音或水声伪影。

Demucs 首次运行会下载模型，CPU 处理很慢，因此它不是 MVP 默认路径。

## 12A. 可选验证口型同步

项目不内置 lip-sync 模型权重，但 mux 阶段已经支持外部命令模板。你可以把
Wav2Lip、MuseTalk 或其他工具安装在独立环境，然后让本项目把原视频、中文配音
时间轴和字幕路径传给它。

命令模板可使用这些占位符：

| 占位符 | 含义 |
|---|---|
| `{video}` | 原始输入视频，通常是 `source.mp4` |
| `{muxed}` | 已完成字幕/双音轨封装、但尚未 lip-sync 的中间 MP4 |
| `{audio}` | `dub-timeline.wav`，完整中文配音时间轴 |
| `{subtitles}` | `zh-CN.srt` |
| `{output}` | lip-sync 后的最终 MP4 输出路径 |
| `{ffmpeg}` / `{ffprobe}` | 当前项目找到的 FFmpeg/FFprobe |

示例：

```bash
make mux \
  JOB_ID='JOB_ID' \
  ENABLE_LIP_SYNC=true \
  LIP_SYNC_COMMAND='python /path/to/Wav2Lip/inference.py --face {video} --audio {audio} --outfile {output}'
```

启用后，普通封装会先写到 `*-pre-lipsync.mp4`，外部命令成功后最终输出仍是
`data/outputs/{title}-{job_id}.mp4`。如果外部命令失败，任务会失败并保留日志；
可修正命令后重新执行 `make mux ... MUX_ARGS="--force"`。

## 13. 术语表验证

创建 `work/acceptance/glossary.json`：

```json
{
  "prompt": "提示词",
  "fine-tuning": "微调",
  "inference": "推理"
}
```

执行：

```bash
.venv/bin/video-translator translate \
  work/acceptance/source-en.mp4 \
  --source-language en \
  --glossary work/acceptance/glossary.json
```

在最新任务的 `segments.json` 中确认术语一致。

## 14. 常见失败定位

| 现象 | 首先检查 | 常见处理 |
|---|---|---|
| `doctor` 只有翻译服务失败 | Ollama/兼容服务是否启动 | 启动服务，检查 base URL 和模型名 |
| Codex CLI 翻译失败 | `codex --version`、`codex login status` | 先登录；模型不可用时把 Codex 模型留空；超时则减小 batch |
| 云翻译返回 401/403 | API Key、账号余额、区域地址 | 重新生成密钥，核对 provider 和官方控制台 |
| YouTube 无法解析 | Deno、yt-dlp 版本、Cookies | 运行 `doctor`，再执行 metadata simulate |
| B站出现 412 | 风控、代理、Cookies | 稍后重试；使用 Cookies 和 `--impersonate chrome` |
| B站出现 SSL EOF | 系统代理或 CDN TLS | 使用 curl 后端；必要时 `--proxy direct` |
| Whisper 首次很慢 | 模型正在下载或模型过大 | MLX/faster-whisper 都先用 `tiny`，确认后再升级 |
| 没有识别结果 | `speech-16k.wav` | 播放该文件，确认有人声且语言设置正确 |
| 翻译丢句 | `segments.json`、翻译响应 | 程序会因缺失 ID 失败；查看 `pipeline.log` |
| TTS 文件为空 | `tts/`、网络或 TTS 服务 | 单独执行第 5 或第 11 节 |
| 中文句尾被裁掉 | 日志中的“达到最大加速” | 说明达到 1.8 倍仍放不下；缩短译文或谨慎提高上限 |
| 中文读完后冒出英文尾音 | 是否使用新版整段压低、ASR 时间戳是否覆盖尾音 | 强制重跑 `mux`；新版控制轨覆盖完整识别片段并额外保护尾部 |
| 最终视频没有原声 | manifest 配置和媒体流 | 确认 `keep_original_audio=true` |
| 字幕播放器看不到 | ffprobe 字幕流 | 换支持 MP4 `mov_text` 的播放器或启用烧录 |
| Demucs 音质差 | `no_vocals.wav` | 关闭 Demucs，退回原声自动压低 |

## 15. 推荐验收顺序

每次改动后，按以下顺序执行：

```text
1. doctor
2. 对应模块的单元测试
3. FFmpeg 集成测试
4. 假模型端到端测试
5. Edge TTS 单句
6. tiny Whisper 单句
7. 翻译 API 单请求
8. 本地 5～15 秒端到端视频
9. Web/API
10. 已授权网络视频
11. CosyVoice / Demucs 可选流程
12. 更长视频和质量评估
```

只有本地短视频通过后，才开始测试 YouTube/B站和长视频。这样可以明确区分：

- 程序逻辑问题；
- 外部站点下载问题；
- 模型质量问题；
- 网络或服务配置问题；
- 音视频封装问题。

## 16. 当前 MVP 的边界

- 视频翻译已接可选 pyannote 说话人分离和 speaker→voice 映射，但模型权重下载、
  Hugging Face 条款授权和长视频音色一致性仍需人工验收；
- 已支持可选 lip-sync 外部命令模板，但不随项目内置 Wav2Lip/MuseTalk 权重；
  画面质量取决于你安装的具体模型和参数；
- 外部下载、LLM 和 TTS 已有重试、超时和指数退避，但还没有生产级熔断/限流中心；
- 任务 manifest 和产物仍保存在本地文件系统；SQLite 只记录队列和审计事件；
  多实例生产部署仍应迁移到 Redis/Celery、外部数据库和对象存储；
- 当前适合单机本地工作台，不是多租户 SaaS 后端；
- PDF 内置排版采用固定页数覆盖策略；复杂多栏、公式密集、彩色背景和低清晰扫描件
  建议优先使用 BabelDOC/PDFMathTranslate，并逐页校样；
- EPUB 会保留包内资源和目录链接，但最终效果仍受阅读器和原书 CSS 影响；
- 中间文件可通过 `make cleanup-data CLEANUP_DAYS=7 APPLY=true` 清理；默认 dry-run，
  防止误删仍在使用的任务文件。

## 17. 内容授权

仅处理你拥有版权、取得明确授权，或适用法律允许下载、翻译和生成衍生版本的
内容。第三方站点的服务条款和视频版权是两件不同的事。正式产品应把“用户
上传本地源文件”作为一等入口，而不是只提供第三方链接下载。
