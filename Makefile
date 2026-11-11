.PHONY: bootstrap doctor test clean

bootstrap:
	./scripts/bootstrap.sh

doctor:
	.venv/bin/video-translator doctor

test:
	.venv/bin/pytest

clean:
	rm -rf .pytest_cache .coverage

