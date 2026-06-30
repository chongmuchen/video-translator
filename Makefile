PYTHON := .venv/bin/python
CLI := .venv/bin/video-translator

# Usage:
#   make list URL='https://www.bilibili.com/video/BV.../'
#   make download URL='https://www.bilibili.com/video/BV.../'
#   make download URL='https://www.bilibili.com/video/BV.../' PARTS='1-3,68'
#   make download URL='...' EXTRA='--cookies-from-browser chrome --impersonate chrome'
URL ?=
JOB_ID ?=
PARTS ?=
EXTRA ?= --cookies-from-browser chrome --impersonate chrome
EXTRACT_ARGS ?=
TRANSCRIBE_ARGS ?=
ASR_MODEL ?= small
ASR_DEVICE ?= auto
ASR_COMPUTE_TYPE ?= auto
SOURCE_LANGUAGE ?=
TARGET_LANGUAGE ?= 简体中文
TRANSLATOR_PROVIDER ?= openai_compatible
TRANSLATOR_BASE_URL ?= http://127.0.0.1:11434/v1
TRANSLATOR_MODEL ?= qwen3:8b
TRANSLATOR_API_KEY ?=
TRANSLATE_ARGS ?=
GLOSSARY ?=
TTS_PROVIDER ?= edge
TTS_VOICE ?= zh-CN-XiaoxiaoNeural
TTS_RATE ?= +0%
TTS_VOLUME ?= +0%
TTS_HTTP_URL ?=
COSYVOICE_BASE_URL ?= http://127.0.0.1:50000
COSYVOICE_MODE ?= sft
SYNTHESIZE_ARGS ?=
DUB_SAMPLE_RATE ?= 24000
MAX_TEMPO_FACTOR ?= 1.8
ALIGN_ARGS ?=
KEEP_ORIGINAL_AUDIO ?= true
DUCK_ORIGINAL_AUDIO ?= true
BURN_SUBTITLES ?= false
ENABLE_DEMUCS ?= false
MUX_ARGS ?=
SLEEP_BETWEEN ?= 2
PODCAST_ARGS ?= --list-only

ifeq ($(strip $(PARTS)),)
COLLECTION_SELECTION := --all
else
COLLECTION_SELECTION := --parts "$(PARTS)"
endif

GLOSSARY_ARG = $(if $(strip $(GLOSSARY)),--glossary "$(GLOSSARY)",)
KEEP_ORIGINAL_ARG = $(if $(filter true 1 yes,$(KEEP_ORIGINAL_AUDIO)),--keep-original-audio,--no-keep-original-audio)
DUCK_ORIGINAL_ARG = $(if $(filter true 1 yes,$(DUCK_ORIGINAL_AUDIO)),--duck-original-audio,--no-duck-original-audio)
BURN_SUBTITLES_ARG = $(if $(filter true 1 yes,$(BURN_SUBTITLES)),--burn-subtitles,--no-burn-subtitles)

.PHONY: help bootstrap doctor test plan run web clean list download download-one podcast extract transcribe translate synthesize align mux next resume status

help:
	@echo "Video Translator"
	@echo
	@echo "  make bootstrap"
	@echo "  make doctor"
	@echo "  make test"
	@echo "  make plan"
	@echo "  make run"
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
	@echo "  make transcribe JOB_ID='任务ID' ASR_MODEL=small SOURCE_LANGUAGE=en"
	@echo "  make transcribe JOB_ID='任务ID' ASR_MODEL=large-v3 TRANSCRIBE_ARGS='--force'"
	@echo
	@echo "后续步骤："
	@echo "  make translate JOB_ID='任务ID' TRANSLATOR_MODEL='qwen3:8b'"
	@echo "  make synthesize JOB_ID='任务ID' TTS_PROVIDER=edge"
	@echo "  make align JOB_ID='任务ID'"
	@echo "  make mux JOB_ID='任务ID'"
	@echo "  make next JOB_ID='任务ID'"
	@echo "  make resume JOB_ID='任务ID'"
	@echo "  make status JOB_ID='任务ID'"
	@echo
	@echo "本地网页："
	@echo "  make web"
	@echo "  浏览器打开 http://127.0.0.1:8000"
	@echo
	@echo "登录内容可追加："
	@echo "  EXTRA='--cookies-from-browser chrome --impersonate chrome'"

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

list:
	@test -n "$(strip $(URL))" || \
		(echo "错误：缺少 URL。用法：make list URL='https://...'" >&2; exit 2)
	$(PYTHON) scripts/download_collection.py "$(URL)" --list-only $(EXTRA)

download:
	@test -n "$(strip $(URL))" || \
		(echo "错误：缺少 URL。用法：make download URL='https://...' [PARTS='1-3,68']" >&2; exit 2)
	$(PYTHON) scripts/download_collection.py "$(URL)" \
		$(COLLECTION_SELECTION) \
		--sleep-between "$(SLEEP_BETWEEN)" \
		$(EXTRA)

download-one:
	@test -n "$(strip $(URL))" || \
		(echo "错误：缺少 URL。用法：make download-one URL='https://.../?p=68'" >&2; exit 2)
	$(PYTHON) main.py download "$(URL)" $(EXTRA)

podcast:
	@test -n "$(strip $(URL))" || \
		(echo "错误：缺少 URL。用法：make podcast URL='https://podcasts.apple.com/.../id123'" >&2; exit 2)
	$(PYTHON) scripts/download_apple_podcast.py "$(URL)" $(PODCAST_ARGS)

extract:
	@test -n "$(strip $(JOB_ID))" || \
		(echo "错误：缺少 JOB_ID。用法：make extract JOB_ID='下载命令返回的任务ID'" >&2; exit 2)
	$(PYTHON) main.py step "$(JOB_ID)" extract $(EXTRACT_ARGS)

transcribe:
	@test -n "$(strip $(JOB_ID))" || \
		(echo "错误：缺少 JOB_ID。用法：make transcribe JOB_ID='任务ID' [ASR_MODEL=small] [SOURCE_LANGUAGE=en]" >&2; exit 2)
	VT_ASR_MODEL="$(ASR_MODEL)" \
	VT_ASR_DEVICE="$(ASR_DEVICE)" \
	VT_ASR_COMPUTE_TYPE="$(ASR_COMPUTE_TYPE)" \
	VT_SOURCE_LANGUAGE="$(SOURCE_LANGUAGE)" \
	$(PYTHON) main.py step "$(JOB_ID)" transcribe $(TRANSCRIBE_ARGS)

translate:
	@test -n "$(strip $(JOB_ID))" || \
		(echo "错误：缺少 JOB_ID。用法：make translate JOB_ID='任务ID'" >&2; exit 2)
	VT_TRANSLATOR_PROVIDER="$(TRANSLATOR_PROVIDER)" \
	VT_TRANSLATOR_BASE_URL="$(TRANSLATOR_BASE_URL)" \
	VT_TRANSLATOR_MODEL="$(TRANSLATOR_MODEL)" \
	VT_TRANSLATOR_API_KEY="$(TRANSLATOR_API_KEY)" \
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
	VT_COSYVOICE_BASE_URL="$(COSYVOICE_BASE_URL)" \
	VT_COSYVOICE_MODE="$(COSYVOICE_MODE)" \
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
