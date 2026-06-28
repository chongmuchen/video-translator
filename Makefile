.PHONY: bootstrap doctor test run clean

bootstrap:
	./scripts/bootstrap.sh

doctor:
	.venv/bin/video-translator doctor

test:
	.venv/bin/pytest

run:
	.venv/bin/video-translator serve

clean:
	rm -rf .pytest_cache .coverage

