.PHONY: install test build up down spike ingest pending demo replays

install:
	python -m pip install -e ".[dev,replay]"

test:
	python -m pytest -q

build:
	docker compose build api

up: build
	docker compose up -d api
	@echo "Dashboard: http://localhost:8000  API docs: http://localhost:8000/docs"

down:
	docker compose down

spike: build
	mkdir -p reports && docker compose run --rm spike --per-bracket 20

# make ingest BRACKET=ancient_divine LIMIT=20
ingest: build
	docker compose run --rm ingest sample --bracket $(or $(BRACKET),ancient_divine) --limit $(or $(LIMIT),20) --request-parses

pending: build
	docker compose run --rm ingest pending

# Seed one synthetic match so the dashboard works offline
demo: build
	docker compose run --rm --entrypoint python ingest -m dota.demo

# make replays LIMIT=5 SAMPLE_TICKS=1   (1 = every tick, 30 = once a second)
replays: build
	docker compose run --rm replays --sample-ticks $(or $(SAMPLE_TICKS),1) run --limit $(or $(LIMIT),5)
