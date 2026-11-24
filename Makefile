PY ?= python

.PHONY: help install data pipeline report test lint format clean all docker

help:  ## show this help
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-10s %s\n", $$1, $$2}'

install:  ## install the package with dev dependencies (editable)
	$(PY) -m pip install -e ".[dev]"

data:  ## generate the synthetic landing zone (~14 MB)
	shopflow generate

pipeline:  ## ingest -> silver -> DQ -> gold -> DQ -> churn model -> scores -> report
	shopflow run

test:  ## run the test suite
	$(PY) -m pytest

lint:  ## static checks
	ruff check src tests

format:  ## auto-format
	ruff check --fix src tests
	ruff format src tests

clean:  ## remove generated data and model artifacts (keeps the committed report)
	rm -rf data artifacts .pytest_cache

all: data pipeline  ## generate data and run the full pipeline

docker:  ## build and run everything in a container
	docker build -t shopflow .
	docker run --rm -v $(PWD)/reports:/app/reports shopflow
