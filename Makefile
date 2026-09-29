.PHONY: test train up down demo evaluate

test:
	python3 -m unittest discover -s tests -v
train:
	docker run --rm --platform linux/amd64 -u 0:0 -e RASA_TELEMETRY_ENABLED=false -v "$(CURDIR)/rasa:/app" rasa/rasa:2.8.14-full@sha256:7cf4d9174278e3a104cab9ac1e80bbca5d592f2f96a13e67c741479ce95c9ef1 train --fixed-model-name handoff
up:
	python3 scripts/setup_env.py
	docker compose up --build -d
down:
	docker compose down
demo:
	python3 scripts/wait_ready.py
	python3 scripts/integration_demo.py
evaluate:
	python3 scripts/evaluate.py
