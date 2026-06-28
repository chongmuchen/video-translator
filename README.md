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

## 推荐入口：分步执行

根目录的 `main.py` 是推荐测试入口，它把每个阶段包成独立 Python 命令：

```bash
cd /Users/m/workspace/video-translator

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

## 1. 当前开发进度

状态说明：

- ✅ 已实现，并有自动化或真实媒体集成测试。
- 🟡 已实现，但仍依赖外部站点、模型或人工验收。
- ⬜ 尚未实现。

| 流程 | 状态 | 当前实现 | 已有验证 | 尚需验证 |
|---|---:|---|---|---|
| 本地视频输入 | ✅ | CLI 复制本地 MP4/MKV/MOV/WebM/M4V | 端到端集成测试 | 不同编码和超长视频 |
| YouTube/B站输入 | 🟡 | `yt-dlp`、Deno、Cookies、域名和时长限制 | URL 安全单元测试 | 使用已授权真实链接人工验收 |
| 音频提取 | ✅ | FFmpeg 输出 16 kHz 单声道 WAV | 真实 FFmpeg 集成测试 | 无音轨、损坏媒体等异常样本 |
| 语音识别 | 🟡 | `faster-whisper`、VAD、词级时间戳、片段合并 | 模块测试；本机 tiny 模型真实推理通过 | `large-v3` 质量与长视频性能 |
| 中文翻译 | 🟡 | OpenAI-compatible 接口，支持 Ollama/LM Studio/云服务、批次上下文和术语表 | JSON 解析、ID 完整性单元测试 | 本机尚未安装 Ollama，未做真实翻译验收 |
| Edge 中文 TTS | ✅ | 单一中文音色，逐片段生成 | 本机真实语音生成通过 | 长文本、限流和失败重试 |
| 通用 HTTP TTS | 🟡 | JSON 请求，返回音频字节 | 代码已实现 | 尚未连接真实服务 |
| CosyVoice | 🟡 | 兼容官方 FastAPI 的 SFT、zero-shot、cross-lingual、instruct 接口 | PCM→WAV 封装已实现 | 需要独立 CosyVoice 服务和授权声音验收 |
| 配音时长对齐 | ✅ | 重采样、`atempo` 加速、补静音、裁剪、时间轴拼接 | 单元和端到端集成测试 | 极端长译文的二次缩写 |
| 原声自动压低 | ✅ | FFmpeg sidechain compression + `amix` + `loudnorm` | 真实 FFmpeg 集成测试 | 不同节目类型的参数调优 |
| 人声/BGM 分离 | 🟡 | 可选 Demucs `no_vocals` | 调用代码已实现 | 依赖未默认安装，尚未做模型验收 |
| 字幕与双音轨 MP4 | ✅ | 中文软字幕、中文配音默认音轨、可选原声音轨 | 自动检查 1 视频 + 2 音频 + 1 字幕流 | 更多播放器兼容性 |
| CLI | ✅ | 原 CLI 加根目录 `main.py`：批量下载、分步执行、状态、断点恢复、完整运行 | 冒烟测试及本地 download→extract 验证 | 更完善的交互式界面 |
| Web/API | 🟡 | 创建任务、状态、日志、下载、简易页面 | 健康检查、首页和非法 URL 拒绝已人工冒烟 | API 自动化测试和任务取消 |
| 任务状态与日志 | ✅ | 单机 JSON manifest、每任务日志和中间文件 | JobStore 单元测试 | 数据库、恢复和分布式队列 |
| 多说话人/多音色 | ⬜ | 尚未实现 | — | diarization、说话人到音色映射 |
| 口型同步 | ⬜ | 尚未实现 | — | 需要独立模型和画面重编码 |
| 生产任务系统 | ⬜ | 当前仅单进程线程池 | — | Redis/Celery、对象存储、清理和重试 |

当前自动化测试基线：**23 项测试通过**。

## 2. 每个任务的中间产物

每个任务都有独立目录，调试时应逐层检查，而不是只看最终 MP4：

```text
data/
├── jobs/{job_id}/
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
└── outputs/
    └── {title}-{job_id}.mp4
```

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
cd /Users/m/workspace/video-translator
./scripts/bootstrap.sh
cp .env.example .env
```

如果脚本找不到合适的 Python：

```bash
PYTHON=/path/to/python3.12 ./scripts/bootstrap.sh
```

bootstrap 会：

1. 创建或复用 `.venv`；
2. 安装项目、`yt-dlp`、Deno、`faster-whisper`、Edge TTS 和测试依赖；
3. 在系统没有 FFmpeg 时，将 FFmpeg/ffprobe 下载到项目的 `.runtime/`。

运行检查：

```bash
.venv/bin/video-translator doctor
```

验收条件：

- `ffmpeg`、`ffprobe`、`yt-dlp`、`faster-whisper`、`edge-tts` 和 Deno 为 `✓`；
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
23 passed
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

Apple Silicon 当前通常走 CPU。长视频正式使用前需要记录处理耗时和内存，
再决定使用本机模型、NVIDIA GPU 或独立 ASR 服务。

## 7. 第五步：配置并验证真实翻译

默认翻译接口为 OpenAI-compatible：

```dotenv
VT_TRANSLATOR_PROVIDER=openai_compatible
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

如果连接云端服务，把 API key 放入 `.env`，不要提交到 Git。

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
VT_ASR_MODEL=tiny
VT_ASR_DEVICE=cpu
VT_ASR_COMPUTE_TYPE=int8
VT_TRANSLATOR_BASE_URL=http://127.0.0.1:11434/v1
VT_TRANSLATOR_MODEL=qwen3:8b
VT_TTS_PROVIDER=edge
VT_TTS_VOICE=zh-CN-XiaoxiaoNeural
```

运行完整流程：

```bash
.venv/bin/video-translator translate \
  work/acceptance/source-en.mp4 \
  --source-language en
```

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

启动服务：

```bash
.venv/bin/video-translator serve
```

验证健康检查：

```bash
curl http://127.0.0.1:8000/api/health
```

预期：

```json
{"status":"ok"}
```

打开 <http://127.0.0.1:8000>，提交一个你有权处理的短视频 URL。

也可以直接调用 API：

```bash
curl http://127.0.0.1:8000/api/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.youtube.com/watch?v=AUTHORIZED_VIDEO_ID",
    "options": {
      "target_language": "简体中文",
      "keep_original_audio": true,
      "burn_subtitles": false,
      "glossary": {}
    }
  }'
```

保存返回的 `id`：

```bash
curl http://127.0.0.1:8000/api/jobs/{job_id}
curl http://127.0.0.1:8000/api/jobs/{job_id}/log
curl -L -o translated.mp4 \
  http://127.0.0.1:8000/api/jobs/{job_id}/download
```

安全检查：

```bash
curl http://127.0.0.1:8000/api/jobs \
  -H "Content-Type: application/json" \
  -d '{"url":"http://127.0.0.1/private"}'
```

预期返回 HTTP 400，而不是访问本机地址。

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

公开视频不需要 Cookies，可以去掉 `--cookies-from-browser`。

每个成功结果应显示：

```text
状态:       downloaded
已完成步骤: download
下一步:     extract
视频:       .../data/jobs/{job_id}/source.mp4
```

逐个检查：

```bash
.venv/bin/python main.py status JOB_ID
```

确认下载没问题后，只提取音频：

```bash
.venv/bin/python main.py next JOB_ID
```

预期状态变为 `extracted`，并生成 `speech-16k.wav`。

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
| YouTube 无法解析 | Deno、yt-dlp 版本、Cookies | 运行 `doctor`，再执行 metadata simulate |
| B站出现 412 | 风控、代理、Cookies | 稍后重试；使用 Cookies 和 `--impersonate chrome` |
| B站出现 SSL EOF | 系统代理或 CDN TLS | 使用 curl 后端；必要时 `--proxy direct` |
| Whisper 首次很慢 | 模型正在下载或 CPU 推理 | 先用 `tiny`，确认后再升级模型 |
| 没有识别结果 | `speech-16k.wav` | 播放该文件，确认有人声且语言设置正确 |
| 翻译丢句 | `segments.json`、翻译响应 | 程序会因缺失 ID 失败；查看 `pipeline.log` |
| TTS 文件为空 | `tts/`、网络或 TTS 服务 | 单独执行第 5 或第 11 节 |
| 中文句尾被裁掉 | 日志中的“达到最大加速” | 缩短译文、提高 TTS 语速或增加二次改写 |
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

- 单一中文音色，没有多说话人和多角色音色映射；
- 不做口型同步；
- 超长配音目前是加速后裁剪，尚无自动缩写并重新合成循环；
- 外部下载、LLM 和 TTS 尚未实现统一重试及熔断；
- 任务状态存在本地 JSON，尚无数据库；
- 只适合单机 MVP，不适合多实例生产部署；
- 中间文件不会自动清理。

## 17. 内容授权

仅处理你拥有版权、取得明确授权，或适用法律允许下载、翻译和生成衍生版本的
内容。第三方站点的服务条款和视频版权是两件不同的事。正式产品应把“用户
上传本地源文件”作为一等入口，而不是只提供第三方链接下载。
