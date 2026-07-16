PYTHON := .venv/bin/python
CLI := .venv/bin/video-translator
BOOK_CLI := .venv/bin/book-translator
PAPER_PODCAST_CLI := $(PYTHON) -m video_translator.paper_podcast.cli

# Usage:
#   make list URL='https://www.bilibili.com/video/BV.../'
#   make download URL='https://www.bilibili.com/video/BV.../'
#   make download URL='https://www.bilibili.com/video/BV.../' PARTS='1-3,68'
#   make download URL='...' COOKIES_FROM_BROWSER=chrome DOWNLOAD_IMPERSONATE=chrome
URL ?=
JOB_ID ?=
PARTS ?=
EXTRA ?=
MAX_DOWNLOAD_HEIGHT ?= 1080
DOWNLOAD_BACKEND ?= auto
COOKIES_FROM_BROWSER ?=
DOWNLOAD_PROXY ?=
DOWNLOAD_IMPERSONATE ?=
EXTRACT_ARGS ?=
TRANSCRIBE_ARGS ?=
ASR_BACKEND ?= faster_whisper
ASR_MODEL ?= small
ASR_DEVICE ?= auto
ASR_COMPUTE_TYPE ?= auto
ASR_UNCLEAR_THRESHOLD ?= 0.45
ENABLE_DIARIZATION ?= false
DIARIZATION_MODEL ?= pyannote/speaker-diarization-3.1
DIARIZATION_AUTH_TOKEN ?=
SOURCE_LANGUAGE ?=
TARGET_LANGUAGE ?= 简体中文
TRANSLATOR_PROVIDER ?= openai_compatible
ifeq ($(TRANSLATOR_PROVIDER),kimi)
TRANSLATOR_DEFAULT_BASE_URL := https://api.moonshot.cn/v1
TRANSLATOR_DEFAULT_MODEL := kimi-k2.6
else ifeq ($(TRANSLATOR_PROVIDER),minimax)
TRANSLATOR_DEFAULT_BASE_URL := https://api.minimaxi.com/v1
TRANSLATOR_DEFAULT_MODEL := MiniMax-M2.7
else ifeq ($(TRANSLATOR_PROVIDER),deepseek)
TRANSLATOR_DEFAULT_BASE_URL := https://api.deepseek.com
TRANSLATOR_DEFAULT_MODEL := deepseek-v4-flash
else
TRANSLATOR_DEFAULT_BASE_URL := http://127.0.0.1:11434/v1
TRANSLATOR_DEFAULT_MODEL := qwen3:8b
endif
TRANSLATOR_BASE_URL ?= $(TRANSLATOR_DEFAULT_BASE_URL)
TRANSLATOR_MODEL ?= $(TRANSLATOR_DEFAULT_MODEL)
TRANSLATOR_API_KEY ?=
TRANSLATOR_TIMEOUT_SECONDS ?= 180
TRANSLATOR_RETRIES ?= 2
TRANSLATOR_RETRY_BACKOFF_SECONDS ?= 3
TRANSLATION_BATCH_SIZE ?= $(if $(filter codex_cli,$(TRANSLATOR_PROVIDER)),48,12)
TRANSLATOR_CODEX_BIN ?= codex
TRANSLATOR_CODEX_MODEL ?=
TRANSLATOR_CODEX_STRATEGY ?= balanced
TRANSLATE_ARGS ?=
GLOSSARY ?=
TTS_PROVIDER ?= edge
TTS_VOICE ?= zh-CN-XiaoxiaoNeural
TTS_RATE ?= +0%
TTS_VOLUME ?= +0%
TTS_HTTP_URL ?=
TTS_HTTP_API_KEY ?=
SPEAKER_VOICE_MAP ?=
TTS_TIMEOUT_SECONDS ?= 180
TTS_RETRIES ?= 2
TTS_RETRY_BACKOFF_SECONDS ?= 2
COSYVOICE_BASE_URL ?= http://127.0.0.1:50000
COSYVOICE_MODE ?= sft
COSYVOICE_SAMPLE_RATE ?= 22050
SYNTHESIZE_ARGS ?=
DUB_SAMPLE_RATE ?= 24000
MAX_TEMPO_FACTOR ?= 1.8
ALIGN_ARGS ?=
KEEP_ORIGINAL_AUDIO ?= true
DUCK_ORIGINAL_AUDIO ?= true
BURN_SUBTITLES ?= false
ENABLE_DEMUCS ?= false
ENABLE_LIP_SYNC ?= false
LIP_SYNC_COMMAND ?=
MUX_ARGS ?=
SLEEP_BETWEEN ?= 2
PODCAST_ARGS ?= --list-only
BOOK_FILE ?=
BOOK_ID ?=
BOOK_MODE ?= translated_only
BOOK_PROVIDER ?= codex_cli
BOOK_CODEX_STRATEGY ?= quality
BOOK_ARGS ?=
BOOK_OCR_MODE ?= auto
BOOK_OCR_LANGUAGES ?= eng
BOOK_OCR_BACKEND ?= ocrmypdf
PAPER_FILE ?=
PAPER_PODCAST_ID ?=
PAPER_PODCAST_STYLE ?= deep_dive
PAPER_PODCAST_SCRIPT_BACKEND ?= builtin
PAPER_PODCAST_COMPARE_MODELS ?=
PAPER_PODCAST_DURATION ?= 8
PAPER_PODCAST_PROVIDER ?= ollama
PAPER_PODCAST_MODEL ?= $(if $(filter ollama,$(PAPER_PODCAST_PROVIDER)),qwen3:8b,)
PAPER_PODCAST_CODEX_STRATEGY ?= balanced
PAPER_PODCAST_TTS_PROVIDER ?= edge
PAPER_PODCAST_VOICE_A ?= zh-CN-XiaoxiaoNeural
PAPER_PODCAST_VOICE_B ?= zh-CN-YunxiNeural
PAPER_PODCAST_TTS_RATE ?= +0%
PAPER_PODCAST_SILENCE_MS ?= 220
PAPER_PODCAST_MAKE_VIDEO ?= false
PAPER_PODCAST_ARGS ?=

ifeq ($(strip $(PARTS)),)
COLLECTION_SELECTION := --all
else
COLLECTION_SELECTION := --parts "$(PARTS)"
endif

GLOSSARY_ARG = $(if $(strip $(GLOSSARY)),--glossary "$(GLOSSARY)",)
KEEP_ORIGINAL_ARG = $(if $(filter true 1 yes,$(KEEP_ORIGINAL_AUDIO)),--keep-original-audio,--no-keep-original-audio)
DUCK_ORIGINAL_ARG = $(if $(filter true 1 yes,$(DUCK_ORIGINAL_AUDIO)),--duck-original-audio,--no-duck-original-audio)
BURN_SUBTITLES_ARG = $(if $(filter true 1 yes,$(BURN_SUBTITLES)),--burn-subtitles,--no-burn-subtitles)
RUN_SOURCE_LANGUAGE_ARG = $(if $(strip $(SOURCE_LANGUAGE)),--source-language "$(SOURCE_LANGUAGE)",)
RUN_ORIGINAL_ARG = $(if $(filter true 1 yes,$(KEEP_ORIGINAL_AUDIO)),,--no-original-audio)
RUN_BURN_SUBTITLES_ARG = $(if $(filter true 1 yes,$(BURN_SUBTITLES)),--burn-subtitles,)
DOWNLOAD_ARGS = --download-backend "$(DOWNLOAD_BACKEND)" \
	$(if $(strip $(COOKIES_FROM_BROWSER)),--cookies-from-browser "$(COOKIES_FROM_BROWSER)",) \
	$(if $(strip $(DOWNLOAD_PROXY)),--proxy "$(DOWNLOAD_PROXY)",) \
	$(if $(strip $(DOWNLOAD_IMPERSONATE)),--impersonate "$(DOWNLOAD_IMPERSONATE)",)

CLEANUP_DAYS ?= 7

.PHONY: help bootstrap doctor test plan run web auto clean cleanup-data list download download-one podcast extract transcribe translate synthesize align mux next resume status book-run book-import book-extract book-translate book-render book-status paper-podcast-run paper-podcast-status

help:
	@echo "Video Translator"
	@echo
	@echo "  make bootstrap"
	@echo "  make doctor"
	@echo "  make test"
	@echo "  make plan"
	@echo "  make run"
	@echo "  make cleanup-data CLEANUP_DAYS=7"
	@echo
	@echo "合集："
	@echo "  make list URL='https://www.bilibili.com/video/BV.../'"
	@echo "  make download URL='https://www.bilibili.com/video/BV.../'"
	@echo "  make download URL='...' PARTS='1-3,68'"
	@echo
	@echo "单集："
	@echo "  make download-one URL='https://www.bilibili.com/video/BV.../?p=68'"
	@echo "  make podcast URL='https://podcasts.apple.com/.../id123'"
	@echo "  make podcast URL='...' PODCAST_ARGS='--latest 1'"
	@echo "  make podcast URL='...' PODCAST_ARGS='--episodes 1-3'"
	@echo
	@echo "抽取音频："
	@echo "  make extract JOB_ID='下载命令返回的任务ID'"
	@echo "  make extract JOB_ID='任务ID' EXTRACT_ARGS='--force'"
	@echo
	@echo "语音识别与时间戳："
	@echo "  make transcribe JOB_ID='任务ID' ASR_BACKEND=mlx_whisper ASR_MODEL=small.en SOURCE_LANGUAGE=en"
	@echo "  make transcribe JOB_ID='任务ID' ENABLE_DIARIZATION=true DIARIZATION_AUTH_TOKEN='hf_...'"
	@echo "  make transcribe JOB_ID='任务ID' ASR_BACKEND=faster_whisper ASR_MODEL=small SOURCE_LANGUAGE=en"
	@echo "  make transcribe JOB_ID='任务ID' ASR_MODEL=large-v3 TRANSCRIBE_ARGS='--force'"
	@echo
	@echo "后续步骤："
	@echo "  make translate JOB_ID='任务ID' TRANSLATOR_PROVIDER=codex_cli"
	@echo "  make translate JOB_ID='任务ID' TRANSLATOR_PROVIDER=ollama"
	@echo "  make translate JOB_ID='任务ID' TRANSLATOR_PROVIDER=kimi TRANSLATOR_API_KEY='...'"
	@echo "  make translate JOB_ID='任务ID' TRANSLATOR_PROVIDER=minimax TRANSLATOR_API_KEY='...'"
	@echo "  make synthesize JOB_ID='任务ID' SPEAKER_VOICE_MAP='{\"SPEAKER_00\":\"zh-CN-XiaoxiaoNeural\",\"SPEAKER_01\":\"zh-CN-YunxiNeural\"}'"
	@echo "  make mux JOB_ID='任务ID' ENABLE_LIP_SYNC=true LIP_SYNC_COMMAND='python /path/Wav2Lip/inference.py --face {video} --audio {audio} --outfile {output}'"
	@echo "  make translate JOB_ID='任务ID' TRANSLATOR_PROVIDER=deepseek TRANSLATOR_API_KEY='...'"
	@echo "  make synthesize JOB_ID='任务ID' TTS_PROVIDER=edge"
	@echo "  make align JOB_ID='任务ID'"
	@echo "  make mux JOB_ID='任务ID'"
	@echo "  make next JOB_ID='任务ID'"
	@echo "  make resume JOB_ID='任务ID'"
	@echo "  make status JOB_ID='任务ID'"
	@echo
	@echo "一键按同一套配置跑完 7 步："
	@echo "  make auto URL='https://...' ASR_BACKEND=mlx_whisper TRANSLATOR_PROVIDER=codex_cli"
	@echo
	@echo "本地网页："
	@echo "  make web"
	@echo "  浏览器打开 http://127.0.0.1:8000"
	@echo
	@echo "PDF / EPUB 书籍翻译："
	@echo "  make book-run BOOK_FILE='/path/book.pdf' BOOK_MODE=translated_only"
	@echo "  make book-run BOOK_FILE='/path/scanned.pdf' BOOK_OCR_MODE=auto BOOK_OCR_LANGUAGES=eng"
	@echo "  make book-run BOOK_FILE='/path/scanned.pdf' BOOK_OCR_BACKEND=docling"
	@echo "  make book-run BOOK_FILE='/path/paper.pdf' BOOK_MODE=pdf2zh_bing_mono"
	@echo "  make book-import BOOK_FILE='/path/book.epub'"
	@echo "  make book-extract BOOK_ID='任务ID' BOOK_OCR_MODE=auto"
	@echo "  make book-translate BOOK_ID='任务ID' BOOK_PROVIDER=codex_cli BOOK_CODEX_STRATEGY=quality"
	@echo "  make book-render BOOK_ID='任务ID' BOOK_MODE=bilingual"
	@echo "  make book-render BOOK_ID='任务ID' BOOK_MODE=pdf2zh_bing_mono"
	@echo "  make book-render BOOK_ID='任务ID' BOOK_MODE=babeldoc_bing_mono"
	@echo "  make book-render BOOK_ID='任务ID' BOOK_MODE=paper_translated_reflow"
	@echo "  make book-render BOOK_ID='任务ID' BOOK_MODE=paper_bilingual_stacked"
	@echo "  专业 PDF 模式优先在网页选择；左右分页推荐 pdf2zh_bing_facing。"
	@echo
	@echo "论文讲解播客："
	@echo "  make paper-podcast-run PAPER_FILE='/path/paper.pdf'"
	@echo "  make paper-podcast-run PAPER_FILE='/path/paper.pdf' PAPER_PODCAST_SCRIPT_BACKEND=notebooklm"
	@echo "  make paper-podcast-run PAPER_FILE='/path/paper.pdf' PAPER_PODCAST_COMPARE_MODELS='qwen3:8b,qwen3:14b'"
	@echo "  make paper-podcast-run PAPER_FILE='/path/paper.pdf' PAPER_PODCAST_MAKE_VIDEO=true"
	@echo "  make paper-podcast-run PAPER_FILE='/path/paper.pdf' PAPER_PODCAST_PROVIDER=codex_cli"
	@echo "  make paper-podcast-run PAPER_FILE='/path/paper.pdf' PAPER_PODCAST_TTS_PROVIDER=cosyvoice"
	@echo "  make paper-podcast-status PAPER_PODCAST_ID='任务ID'"
	@echo
	@echo "登录内容可追加："
	@echo "  COOKIES_FROM_BROWSER=chrome DOWNLOAD_IMPERSONATE=chrome"

bootstrap:
	./scripts/bootstrap.sh

doctor:
	$(CLI) doctor

test:
	$(PYTHON) -m pytest

plan:
	$(PYTHON) main.py plan

run:
	$(CLI) serve

web: run

auto:
	@test -n "$(strip $(URL))" || \
		(echo "错误：缺少 URL。用法：make auto URL='https://...'" >&2; exit 2)
	VT_MAX_DOWNLOAD_HEIGHT="$(MAX_DOWNLOAD_HEIGHT)" \
	VT_ASR_BACKEND="$(ASR_BACKEND)" \
	VT_ASR_MODEL="$(ASR_MODEL)" \
	VT_ASR_DEVICE="$(ASR_DEVICE)" \
	VT_ASR_COMPUTE_TYPE="$(ASR_COMPUTE_TYPE)" \
	VT_ASR_UNCLEAR_THRESHOLD="$(ASR_UNCLEAR_THRESHOLD)" \
	VT_ENABLE_DIARIZATION="$(ENABLE_DIARIZATION)" \
	VT_DIARIZATION_MODEL="$(DIARIZATION_MODEL)" \
	VT_DIARIZATION_AUTH_TOKEN="$(DIARIZATION_AUTH_TOKEN)" \
	VT_TRANSLATOR_PROVIDER="$(TRANSLATOR_PROVIDER)" \
	VT_TRANSLATOR_BASE_URL="$(TRANSLATOR_BASE_URL)" \
	VT_TRANSLATOR_MODEL="$(TRANSLATOR_MODEL)" \
	VT_TRANSLATOR_API_KEY="$(TRANSLATOR_API_KEY)" \
	VT_TRANSLATOR_TIMEOUT_SECONDS="$(TRANSLATOR_TIMEOUT_SECONDS)" \
	VT_TRANSLATOR_RETRIES="$(TRANSLATOR_RETRIES)" \
	VT_TRANSLATOR_RETRY_BACKOFF_SECONDS="$(TRANSLATOR_RETRY_BACKOFF_SECONDS)" \
	VT_TRANSLATION_BATCH_SIZE="$(TRANSLATION_BATCH_SIZE)" \
	VT_TRANSLATOR_CODEX_BIN="$(TRANSLATOR_CODEX_BIN)" \
	VT_TRANSLATOR_CODEX_MODEL="$(TRANSLATOR_CODEX_MODEL)" \
	VT_TRANSLATOR_CODEX_STRATEGY="$(TRANSLATOR_CODEX_STRATEGY)" \
	VT_TTS_PROVIDER="$(TTS_PROVIDER)" \
	VT_TTS_VOICE="$(TTS_VOICE)" \
	VT_TTS_RATE="$(TTS_RATE)" \
	VT_TTS_VOLUME="$(TTS_VOLUME)" \
	VT_TTS_HTTP_URL="$(TTS_HTTP_URL)" \
	VT_TTS_HTTP_API_KEY="$(TTS_HTTP_API_KEY)" \
	VT_SPEAKER_VOICE_MAP="$(SPEAKER_VOICE_MAP)" \
	VT_TTS_TIMEOUT_SECONDS="$(TTS_TIMEOUT_SECONDS)" \
	VT_TTS_RETRIES="$(TTS_RETRIES)" \
	VT_TTS_RETRY_BACKOFF_SECONDS="$(TTS_RETRY_BACKOFF_SECONDS)" \
	VT_COSYVOICE_BASE_URL="$(COSYVOICE_BASE_URL)" \
	VT_COSYVOICE_MODE="$(COSYVOICE_MODE)" \
	VT_COSYVOICE_SAMPLE_RATE="$(COSYVOICE_SAMPLE_RATE)" \
	VT_DUB_SAMPLE_RATE="$(DUB_SAMPLE_RATE)" \
	VT_MAX_TEMPO_FACTOR="$(MAX_TEMPO_FACTOR)" \
	VT_DUCK_ORIGINAL_AUDIO="$(DUCK_ORIGINAL_AUDIO)" \
	VT_ENABLE_DEMUCS="$(ENABLE_DEMUCS)" \
	VT_ENABLE_LIP_SYNC="$(ENABLE_LIP_SYNC)" \
	VT_LIP_SYNC_COMMAND="$(LIP_SYNC_COMMAND)" \
	$(PYTHON) main.py run "$(URL)" \
		--target-language "$(TARGET_LANGUAGE)" \
		$(RUN_SOURCE_LANGUAGE_ARG) \
		$(RUN_ORIGINAL_ARG) \
		$(RUN_BURN_SUBTITLES_ARG) \
		$(GLOSSARY_ARG) \
		$(DOWNLOAD_ARGS) $(EXTRA)

list:
	@test -n "$(strip $(URL))" || \
		(echo "错误：缺少 URL。用法：make list URL='https://...'" >&2; exit 2)
	VT_MAX_DOWNLOAD_HEIGHT="$(MAX_DOWNLOAD_HEIGHT)" \
	$(PYTHON) scripts/download_collection.py "$(URL)" --list-only \
		$(DOWNLOAD_ARGS) $(EXTRA)

download:
	@test -n "$(strip $(URL))" || \
		(echo "错误：缺少 URL。用法：make download URL='https://...' [PARTS='1-3,68']" >&2; exit 2)
	VT_MAX_DOWNLOAD_HEIGHT="$(MAX_DOWNLOAD_HEIGHT)" \
	$(PYTHON) scripts/download_collection.py "$(URL)" \
		$(COLLECTION_SELECTION) \
		--sleep-between "$(SLEEP_BETWEEN)" \
		$(DOWNLOAD_ARGS) $(EXTRA)

download-one:
	@test -n "$(strip $(URL))" || \
		(echo "错误：缺少 URL。用法：make download-one URL='https://.../?p=68'" >&2; exit 2)
	VT_MAX_DOWNLOAD_HEIGHT="$(MAX_DOWNLOAD_HEIGHT)" \
	$(PYTHON) main.py download "$(URL)" $(DOWNLOAD_ARGS) $(EXTRA)

podcast:
	@test -n "$(strip $(URL))" || \
		(echo "错误：缺少 URL。用法：make podcast URL='https://podcasts.apple.com/.../id123'" >&2; exit 2)
	$(PYTHON) scripts/download_apple_podcast.py "$(URL)" $(PODCAST_ARGS)

book-run:
	@test -n "$(strip $(BOOK_FILE))" || \
		(echo "错误：缺少 BOOK_FILE。" >&2; exit 2)
	VT_TRANSLATOR_API_KEY="$(TRANSLATOR_API_KEY)" \
	VT_TRANSLATOR_TIMEOUT_SECONDS="$(TRANSLATOR_TIMEOUT_SECONDS)" \
	VT_TRANSLATOR_RETRIES="$(TRANSLATOR_RETRIES)" \
	VT_TRANSLATOR_RETRY_BACKOFF_SECONDS="$(TRANSLATOR_RETRY_BACKOFF_SECONDS)" \
	$(BOOK_CLI) run "$(BOOK_FILE)" \
		--mode "$(BOOK_MODE)" \
		--target-language "$(TARGET_LANGUAGE)" \
		--ocr-mode "$(BOOK_OCR_MODE)" \
		--ocr-languages "$(BOOK_OCR_LANGUAGES)" \
		--ocr-backend "$(BOOK_OCR_BACKEND)" \
		--provider "$(BOOK_PROVIDER)" \
		--codex-strategy "$(BOOK_CODEX_STRATEGY)" \
		$(GLOSSARY_ARG) $(BOOK_ARGS)

book-import:
	@test -n "$(strip $(BOOK_FILE))" || \
		(echo "错误：缺少 BOOK_FILE。" >&2; exit 2)
	$(BOOK_CLI) import "$(BOOK_FILE)" --mode "$(BOOK_MODE)"

book-extract:
	@test -n "$(strip $(BOOK_ID))" || \
		(echo "错误：缺少 BOOK_ID。" >&2; exit 2)
	$(BOOK_CLI) extract "$(BOOK_ID)" \
		--ocr-mode "$(BOOK_OCR_MODE)" \
		--ocr-languages "$(BOOK_OCR_LANGUAGES)" \
		--ocr-backend "$(BOOK_OCR_BACKEND)"

book-translate:
	@test -n "$(strip $(BOOK_ID))" || \
		(echo "错误：缺少 BOOK_ID。" >&2; exit 2)
	VT_TRANSLATOR_API_KEY="$(TRANSLATOR_API_KEY)" \
	VT_TRANSLATOR_TIMEOUT_SECONDS="$(TRANSLATOR_TIMEOUT_SECONDS)" \
	VT_TRANSLATOR_RETRIES="$(TRANSLATOR_RETRIES)" \
	VT_TRANSLATOR_RETRY_BACKOFF_SECONDS="$(TRANSLATOR_RETRY_BACKOFF_SECONDS)" \
	$(BOOK_CLI) translate "$(BOOK_ID)" \
		--target-language "$(TARGET_LANGUAGE)" \
		--provider "$(BOOK_PROVIDER)" \
		--codex-strategy "$(BOOK_CODEX_STRATEGY)" \
		$(GLOSSARY_ARG) $(BOOK_ARGS)

book-render:
	@test -n "$(strip $(BOOK_ID))" || \
		(echo "错误：缺少 BOOK_ID。" >&2; exit 2)
	VT_TRANSLATOR_TIMEOUT_SECONDS="$(TRANSLATOR_TIMEOUT_SECONDS)" \
	$(BOOK_CLI) render "$(BOOK_ID)" --mode "$(BOOK_MODE)"

book-status:
	$(BOOK_CLI) status $(BOOK_ID)

paper-podcast-run:
	@test -n "$(strip $(PAPER_FILE))" || \
		(echo "错误：缺少 PAPER_FILE。" >&2; exit 2)
	VT_TRANSLATOR_API_KEY="$(TRANSLATOR_API_KEY)" \
	$(PAPER_PODCAST_CLI) run "$(PAPER_FILE)" \
		--style "$(PAPER_PODCAST_STYLE)" \
		--script-backend "$(PAPER_PODCAST_SCRIPT_BACKEND)" \
		$(if $(strip $(PAPER_PODCAST_COMPARE_MODELS)),--compare-models "$(PAPER_PODCAST_COMPARE_MODELS)",) \
		--duration-minutes "$(PAPER_PODCAST_DURATION)" \
		--target-language "$(TARGET_LANGUAGE)" \
		--provider "$(PAPER_PODCAST_PROVIDER)" \
		--base-url "$(TRANSLATOR_BASE_URL)" \
		--model "$(PAPER_PODCAST_MODEL)" \
		--codex-strategy "$(PAPER_PODCAST_CODEX_STRATEGY)" \
		--tts-provider "$(PAPER_PODCAST_TTS_PROVIDER)" \
		--voice-a "$(PAPER_PODCAST_VOICE_A)" \
		--voice-b "$(PAPER_PODCAST_VOICE_B)" \
		--tts-rate "$(PAPER_PODCAST_TTS_RATE)" \
		--cosyvoice-url "$(COSYVOICE_BASE_URL)" \
		--http-tts-url "$(TTS_HTTP_URL)" \
		--silence-ms "$(PAPER_PODCAST_SILENCE_MS)" \
		$(if $(filter true 1 yes,$(PAPER_PODCAST_MAKE_VIDEO)),--make-video,) \
		$(GLOSSARY_ARG) $(PAPER_PODCAST_ARGS)

paper-podcast-status:
	$(PAPER_PODCAST_CLI) status $(PAPER_PODCAST_ID)

extract:
	@test -n "$(strip $(JOB_ID))" || \
		(echo "错误：缺少 JOB_ID。用法：make extract JOB_ID='下载命令返回的任务ID'" >&2; exit 2)
	$(PYTHON) main.py step "$(JOB_ID)" extract $(EXTRACT_ARGS)

transcribe:
	@test -n "$(strip $(JOB_ID))" || \
		(echo "错误：缺少 JOB_ID。用法：make transcribe JOB_ID='任务ID' [ASR_MODEL=small] [SOURCE_LANGUAGE=en]" >&2; exit 2)
	VT_ASR_BACKEND="$(ASR_BACKEND)" \
	VT_ASR_MODEL="$(ASR_MODEL)" \
	VT_ASR_DEVICE="$(ASR_DEVICE)" \
	VT_ASR_COMPUTE_TYPE="$(ASR_COMPUTE_TYPE)" \
	VT_ASR_UNCLEAR_THRESHOLD="$(ASR_UNCLEAR_THRESHOLD)" \
	VT_ENABLE_DIARIZATION="$(ENABLE_DIARIZATION)" \
	VT_DIARIZATION_MODEL="$(DIARIZATION_MODEL)" \
	VT_DIARIZATION_AUTH_TOKEN="$(DIARIZATION_AUTH_TOKEN)" \
	VT_SOURCE_LANGUAGE="$(SOURCE_LANGUAGE)" \
	$(PYTHON) main.py step "$(JOB_ID)" transcribe $(TRANSCRIBE_ARGS)

translate:
	@test -n "$(strip $(JOB_ID))" || \
		(echo "错误：缺少 JOB_ID。用法：make translate JOB_ID='任务ID'" >&2; exit 2)
	VT_TRANSLATOR_PROVIDER="$(TRANSLATOR_PROVIDER)" \
	VT_TRANSLATOR_BASE_URL="$(TRANSLATOR_BASE_URL)" \
	VT_TRANSLATOR_MODEL="$(TRANSLATOR_MODEL)" \
	VT_TRANSLATOR_API_KEY="$(TRANSLATOR_API_KEY)" \
	VT_TRANSLATOR_TIMEOUT_SECONDS="$(TRANSLATOR_TIMEOUT_SECONDS)" \
	VT_TRANSLATOR_RETRIES="$(TRANSLATOR_RETRIES)" \
	VT_TRANSLATOR_RETRY_BACKOFF_SECONDS="$(TRANSLATOR_RETRY_BACKOFF_SECONDS)" \
	VT_TRANSLATION_BATCH_SIZE="$(TRANSLATION_BATCH_SIZE)" \
	VT_TRANSLATOR_CODEX_BIN="$(TRANSLATOR_CODEX_BIN)" \
	VT_TRANSLATOR_CODEX_MODEL="$(TRANSLATOR_CODEX_MODEL)" \
	VT_TRANSLATOR_CODEX_STRATEGY="$(TRANSLATOR_CODEX_STRATEGY)" \
	$(PYTHON) main.py step "$(JOB_ID)" translate \
		--target-language "$(TARGET_LANGUAGE)" \
		$(GLOSSARY_ARG) $(TRANSLATE_ARGS)

synthesize:
	@test -n "$(strip $(JOB_ID))" || \
		(echo "错误：缺少 JOB_ID。用法：make synthesize JOB_ID='任务ID'" >&2; exit 2)
	VT_TTS_PROVIDER="$(TTS_PROVIDER)" \
	VT_TTS_VOICE="$(TTS_VOICE)" \
	VT_TTS_RATE="$(TTS_RATE)" \
	VT_TTS_VOLUME="$(TTS_VOLUME)" \
	VT_TTS_HTTP_URL="$(TTS_HTTP_URL)" \
	VT_TTS_HTTP_API_KEY="$(TTS_HTTP_API_KEY)" \
	VT_SPEAKER_VOICE_MAP="$(SPEAKER_VOICE_MAP)" \
	VT_TTS_TIMEOUT_SECONDS="$(TTS_TIMEOUT_SECONDS)" \
	VT_TTS_RETRIES="$(TTS_RETRIES)" \
	VT_TTS_RETRY_BACKOFF_SECONDS="$(TTS_RETRY_BACKOFF_SECONDS)" \
	VT_COSYVOICE_BASE_URL="$(COSYVOICE_BASE_URL)" \
	VT_COSYVOICE_MODE="$(COSYVOICE_MODE)" \
	VT_COSYVOICE_SAMPLE_RATE="$(COSYVOICE_SAMPLE_RATE)" \
	$(PYTHON) main.py step "$(JOB_ID)" synthesize $(SYNTHESIZE_ARGS)

align:
	@test -n "$(strip $(JOB_ID))" || \
		(echo "错误：缺少 JOB_ID。用法：make align JOB_ID='任务ID'" >&2; exit 2)
	VT_DUB_SAMPLE_RATE="$(DUB_SAMPLE_RATE)" \
	VT_MAX_TEMPO_FACTOR="$(MAX_TEMPO_FACTOR)" \
	$(PYTHON) main.py step "$(JOB_ID)" align $(ALIGN_ARGS)

mux:
	@test -n "$(strip $(JOB_ID))" || \
		(echo "错误：缺少 JOB_ID。用法：make mux JOB_ID='任务ID'" >&2; exit 2)
	VT_ENABLE_DEMUCS="$(ENABLE_DEMUCS)" \
	VT_ENABLE_LIP_SYNC="$(ENABLE_LIP_SYNC)" \
	VT_LIP_SYNC_COMMAND="$(LIP_SYNC_COMMAND)" \
	$(PYTHON) main.py step "$(JOB_ID)" mux \
		$(KEEP_ORIGINAL_ARG) \
		$(DUCK_ORIGINAL_ARG) \
		$(BURN_SUBTITLES_ARG) \
		$(MUX_ARGS)

next:
	@test -n "$(strip $(JOB_ID))" || \
		(echo "错误：缺少 JOB_ID。用法：make next JOB_ID='任务ID'" >&2; exit 2)
	$(PYTHON) main.py next "$(JOB_ID)"

resume:
	@test -n "$(strip $(JOB_ID))" || \
		(echo "错误：缺少 JOB_ID。用法：make resume JOB_ID='任务ID'" >&2; exit 2)
	$(PYTHON) main.py resume "$(JOB_ID)"

status:
	$(PYTHON) main.py status $(JOB_ID)

clean:
	rm -rf .pytest_cache .coverage

cleanup-data:
	$(CLI) cleanup --days "$(CLEANUP_DAYS)" $(if $(filter true 1 yes,$(APPLY)),--apply,)
