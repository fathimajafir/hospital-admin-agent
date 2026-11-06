PY ?= .venv/bin/python
PIP ?= $(PY) -m pip
INDEX ?= https://pypi.org/simple

.PHONY: help venv install test lint demo run mcp mcp-list eval seed docker clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

venv: ## create the virtualenv
	~/.pyenv/versions/3.11.8/bin/python3 -m venv .venv || python3.11 -m venv .venv

install: ## install the package with dev + mcp extras
	$(PIP) install -q --index-url $(INDEX) -e ".[dev,mcp]"

test: ## run the test suite
	$(PY) -m pytest -q

demo: ## run three representative agent tasks
	$(PY) scripts/demo.py

run: ## start the HTTP API on :8080
	$(PY) -m uvicorn hospital_agent.api.app:app --host 0.0.0.0 --port 8080

mcp: ## start the MCP server on stdio
	$(PY) -m mcp_server.server

mcp-list: ## print the MCP tool catalog
	$(PY) -m mcp_server.server --list

mcp-http: ## start the MCP server on http://127.0.0.1:8765/mcp
	$(PY) -m mcp_server.server --transport http --port 8765

eval: ## run the deterministic evaluation suite
	$(PY) eval/run_eval.py

seed: ## rebuild the synthetic dataset
	$(PY) -m hospital_agent.cli seed --reset

docker: ## build the image
	docker build -t hospital-admin-agent:1.0.0 .

clean:
	rm -rf .pytest_cache var *.egg-info src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
