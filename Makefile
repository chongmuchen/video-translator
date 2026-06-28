PYTHON := .venv/bin/python
CLI := .venv/bin/video-translator

# Usage:
#   make list URL='https://www.bilibili.com/video/BV.../'
#   make download URL='https://www.bilibili.com/video/BV.../'
#   make download URL='https://www.bilibili.com/video/BV.../' PARTS='1-3,68'
#   make download URL='...' EXTRA='--cookies-from-browser chrome --impersonate chrome'
URL ?=
PARTS ?=
EXTRA ?= --cookies-from-browser chrome --impersonate chrome
SLEEP_BETWEEN ?= 2

ifeq ($(strip $(PARTS)),)
COLLECTION_SELECTION := --all
else
COLLECTION_SELECTION := --parts "$(PARTS)"
endif

.PHONY: help bootstrap doctor test plan run clean list download download-one

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

clean:
	rm -rf .pytest_cache .coverage



