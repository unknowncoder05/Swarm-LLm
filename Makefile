.DEFAULT_GOAL := help
COMPOSE       := docker compose
ENV_FILE      := .env
SWARM_API     ?= $(shell python3 -c 'from scripts.gen_opencode_config import load_base_url; print(load_base_url().rsplit("/v1", 1)[0])')
SWARM_ROOT    = $(patsubst %/api,%,$(SWARM_API))

# ── colour helpers ────────────────────────────────────────────────────────────
BOLD  := \033[1m
CYAN  := \033[36m
GREEN := \033[32m
YELLOW:= \033[33m
RESET := \033[0m

# ── guard: .env must exist ────────────────────────────────────────────────────
check-env:
	@test -f $(ENV_FILE) || { \
	  echo "$(YELLOW)No .env found. Run 'make init' first.$(RESET)"; exit 1; }

# ─────────────────────────────────────────────────────────────────────────────
## Setup
# ─────────────────────────────────────────────────────────────────────────────

.PHONY: init
init: ## Create .env from .env.example with fresh random keys for all three roles
	@python3 scripts/init-env.py --output "$(ENV_FILE)"

.PHONY: keygen
keygen: ## Print a new random API key (does not write to .env)
	@openssl rand -hex 20

# ─────────────────────────────────────────────────────────────────────────────
## Stack control
# ─────────────────────────────────────────────────────────────────────────────

.PHONY: up
up: check-env ## Start coordinator + gateway (detached)
	$(COMPOSE) up -d coordinator gateway

.PHONY: up-all
up-all: check-env ## Start full stack including UI
	$(COMPOSE) up -d

.PHONY: down
down: ## Stop and remove all containers
	$(COMPOSE) down --remove-orphans

.PHONY: restart
restart: ## Restart all services (picks up .env changes in every container)
	$(COMPOSE) up -d --force-recreate

.PHONY: build
build: check-env ## Rebuild all Docker images (no cache)
	$(COMPOSE) build --no-cache

.PHONY: rebuild
rebuild: check-env ## Rebuild and restart the full stack
	$(COMPOSE) up -d --build coordinator gateway

.PHONY: rebuild-all
rebuild-all: check-env ## Rebuild and restart including UI
	$(COMPOSE) up -d --build

# ─────────────────────────────────────────────────────────────────────────────
## Observability
# ─────────────────────────────────────────────────────────────────────────────

.PHONY: status
status: ## Show running containers and port bindings
	$(COMPOSE) ps

.PHONY: logs
logs: ## Tail logs from all services
	$(COMPOSE) logs -f

.PHONY: logs-coordinator
logs-coordinator: ## Tail coordinator logs only
	$(COMPOSE) logs -f coordinator

.PHONY: logs-gateway
logs-gateway: ## Tail gateway logs only
	$(COMPOSE) logs -f gateway

.PHONY: health
health: ## Hit the health check endpoint (no auth)
	@curl -s http://localhost:8081/swarm/api/healthz | python3 -m json.tool

.PHONY: machines
machines: check-env ## List registered machines (JSON)
	@ADMIN_KEY=$$(grep ^ADMIN_KEY $(ENV_FILE) | cut -d= -f2); \
	  curl -s -H "X-API-Key: $$ADMIN_KEY" http://localhost:8081/swarm/api/machines | python3 -m json.tool

# ─────────────────────────────────────────────────────────────────────────────
## Testing
# ─────────────────────────────────────────────────────────────────────────────

.PHONY: test
test: check-env ## Run the end-to-end test client (waits for an agent, fires a prompt)
	python3 test_client.py

.PHONY: eval-setup
eval-setup: ## Install EvalPlus benchmark harness (HumanEval+ / MBPP+)
	pip install evalplus

.PHONY: eval-humaneval
eval-humaneval: check-env ## Run HumanEval+ pass@1 against the swarm
	@CONSUMER_KEY=$$(grep ^CONSUMER_KEY $(ENV_FILE) | cut -d= -f2); \
	  MODEL=$$(curl -sf -H "X-API-Key: $$CONSUMER_KEY" http://localhost:8081/swarm/api/v1/models \
	    | python3 -c "import sys,json; d=json.load(sys.stdin)['data']; print(d[0]['id'] if d else '')" 2>/dev/null); \
	  if [ -z "$$MODEL" ]; then echo "$(YELLOW)No live agents — connect an agent first.$(RESET)"; exit 1; fi; \
	  echo "$(CYAN)HumanEval+  model=$$MODEL$(RESET)"; \
	  python3 -m evalplus.evaluate \
	    --model "$$MODEL" --dataset humaneval --backend openai \
	    --base_url http://localhost:8081/swarm/api/v1 \
	    --openai_api_key "$$CONSUMER_KEY" --greedy

.PHONY: eval-mbpp
eval-mbpp: check-env ## Run MBPP+ pass@1 against the swarm
	@CONSUMER_KEY=$$(grep ^CONSUMER_KEY $(ENV_FILE) | cut -d= -f2); \
	  MODEL=$$(curl -sf -H "X-API-Key: $$CONSUMER_KEY" http://localhost:8081/swarm/api/v1/models \
	    | python3 -c "import sys,json; d=json.load(sys.stdin)['data']; print(d[0]['id'] if d else '')" 2>/dev/null); \
	  if [ -z "$$MODEL" ]; then echo "$(YELLOW)No live agents — connect an agent first.$(RESET)"; exit 1; fi; \
	  echo "$(CYAN)MBPP+  model=$$MODEL$(RESET)"; \
	  python3 -m evalplus.evaluate \
	    --model "$$MODEL" --dataset mbpp --backend openai \
	    --base_url http://localhost:8081/swarm/api/v1 \
	    --openai_api_key "$$CONSUMER_KEY" --greedy

# ─────────────────────────────────────────────────────────────────────────────
## Coding harness
# ─────────────────────────────────────────────────────────────────────────────

.PHONY: opencode-setup
opencode-setup: ## Install a local OpenCode client and configure Swarm (requires npm)
	@python3 scripts/setup-opencode.py --install

.PHONY: opencode
opencode: ## Run OpenCode with the private Swarm consumer credential
	@python3 scripts/run-opencode.py

.PHONY: harnesses
harnesses: check-env ## Install swarm-goose + swarm-aider wrappers (run once, then use from any directory)
	@python3 scripts/install_harnesses.py

.PHONY: goose-setup
goose-setup: check-env ## Install Goose CLI and configure it to point at the swarm
	@command -v goose >/dev/null 2>&1 \
	  && echo "goose already installed at $$(command -v goose)" \
	  || (echo "$(CYAN)Downloading goose v1.51.0...$(RESET)" && \
	      mkdir -p /tmp/goose-dl && \
	      curl -fsSL https://github.com/block/goose/releases/download/v1.51.0/goose-x86_64-unknown-linux-gnu.tar.gz \
	        | tar -xz -C /tmp/goose-dl && \
	      mkdir -p ~/.local/bin && cp /tmp/goose-dl/goose ~/.local/bin/goose && chmod +x ~/.local/bin/goose && \
	      echo "$(GREEN)Installed to ~/.local/bin/goose$(RESET)")
	@python3 scripts/gen_goose_config.py
	@echo ""
	@echo "  $(CYAN)~/.config/goose/config.yaml$(RESET) written — model auto-detected from live agents"
	@echo ""
	@echo "  $(GREEN)Run: goose session$(RESET)             interactive TUI"
	@echo "  $(GREEN)Run: goose run --text '...'$(RESET)    one-shot task"
	@echo ""

.PHONY: goose
goose: check-env ## Launch an interactive Goose session against the swarm
	@CONSUMER_KEY=$$(grep ^CONSUMER_KEY $(ENV_FILE) | cut -d= -f2); \
	  MODEL=$$(curl -sf -H "X-API-Key: $$CONSUMER_KEY" http://localhost:8081/swarm/api/v1/models \
	    | python3 -c "import sys,json; d=json.load(sys.stdin)['data']; print(d[0]['id'] if d else 'qwen3:14b')" 2>/dev/null); \
	  OPENAI_BASE_URL=$(SWARM_API)/v1 \
	  OPENAI_API_KEY=$$CONSUMER_KEY \
	  GOOSE_PROVIDER=openai \
	  GOOSE_MODEL=$$MODEL \
	  goose session

.PHONY: ping
ping: check-env ## Quick smoke-test: all three keys work
	@ADMIN=$$(grep ^ADMIN_KEY    $(ENV_FILE) | cut -d= -f2); \
	  AGENT=$$(grep ^AGENT_KEY    $(ENV_FILE) | cut -d= -f2); \
	  CONS=$$(grep  ^CONSUMER_KEY $(ENV_FILE) | cut -d= -f2); \
	  check() { \
	    S=$$(curl -s -o /dev/null -w "%{http_code}" -H "X-API-Key: $$2" "$$3"); \
	    [ "$$S" = "$$4" ] \
	      && echo "  $(GREEN)OK$(RESET)  $$1 → HTTP $$S" \
	      || echo "  $(YELLOW)FAIL$(RESET) $$1 → HTTP $$S (expected $$4)"; \
	  }; \
	  echo ""; \
	  check "ADMIN_KEY    /machines  " "$$ADMIN" http://localhost:8081/swarm/api/machines         200; \
	  check "AGENT_KEY    /register  " "$$AGENT" http://localhost:8081/swarm/api/machines         401; \
	  check "CONSUMER_KEY /machines  " "$$CONS"  http://localhost:8081/swarm/api/machines         401; \
	  check "CONSUMER_KEY /v1/models " "$$CONS"  http://localhost:8081/swarm/api/v1/models        200; \
	  check "AGENT_KEY    /v1/models " "$$AGENT" http://localhost:8081/swarm/api/v1/models        401; \
	  check "no key       /healthz   " ""        http://localhost:8081/swarm/api/healthz          200; \
	  echo ""

# ─────────────────────────────────────────────────────────────────────────────
## Agent launcher helpers
# ─────────────────────────────────────────────────────────────────────────────

.PHONY: agent-cmd
agent-cmd: check-env ## Print the PowerShell one-liner to run on a Windows machine
	@AGENT_KEY=$$(grep ^AGENT_KEY $(ENV_FILE) | cut -d= -f2); \
	  echo ""; \
	  echo "$(CYAN)Run this in cmd.exe on the target machine (interactive wizard):$(RESET)"; \
	  echo ""; \
	  echo "  powershell -ExecutionPolicy Bypass -c \"iwr '$(SWARM_ROOT)/agent/agent.ps1' | iex\""; \
	  echo ""; \
	  echo "$(CYAN)Or non-interactive with explicit params:$(RESET)"; \
	  echo ""; \
	  echo "  .\\agent.ps1 -Coordinator $(SWARM_API) -ApiKey $$AGENT_KEY -Model qwen2.5-coder:1.5b"

.PHONY: agent-build
agent-build: ## Rebuild agent-standalone.cmd from agent.ps1 (single distributable file)
	@python3 -c "\
	src = open('agents/agent.ps1').read(); \
	hdr = '@(set \"Z=%~f0\")& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command \"iex([io.file]::ReadAllText(\$\$env:Z))\" & exit /b\n<# --- batch header above is a PowerShell block comment ---\n'; \
	open('agents/agent-standalone.cmd','w',encoding='utf-8').write(hdr + src + '\n#>'); \
	print('$(GREEN)Built agents/agent-standalone.cmd$(RESET)')"

.PHONY: ollama-mirror
ollama-mirror: ## Download latest Ollama portable zip + installer into ./mirror/
	@mkdir -p mirror
	@LATEST=$$(curl -sf https://api.github.com/repos/ollama/ollama/releases/latest | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])"); \
	  echo "Latest Ollama: $$LATEST"; \
	  CURRENT=$$(cat mirror/.ollama-version 2>/dev/null || echo none); \
	  if [ "$$CURRENT" = "$$LATEST" ] && [ -f mirror/ollama-windows-amd64.zip ] && [ -f mirror/OllamaSetup.exe ]; then \
	    echo "$(GREEN)Already up to date ($$LATEST) — nothing to do$(RESET)"; \
	  else \
	    echo "Downloading ollama-windows-amd64.zip $$LATEST (portable — no admin required)..."; \
	    curl -L --progress-bar \
	      -o mirror/ollama-windows-amd64.zip \
	      "https://github.com/ollama/ollama/releases/download/$$LATEST/ollama-windows-amd64.zip"; \
	    echo "Downloading OllamaSetup.exe $$LATEST (installer fallback)..."; \
	    curl -L --progress-bar \
	      -o mirror/OllamaSetup.exe \
	      "https://github.com/ollama/ollama/releases/download/$$LATEST/OllamaSetup.exe" && \
	    echo "$$LATEST" > mirror/.ollama-version && \
	    echo "$(GREEN)mirror/ updated to $$LATEST$(RESET)"; \
	  fi

# ─────────────────────────────────────────────────────────────────────────────
## Cleanup
# ─────────────────────────────────────────────────────────────────────────────

.PHONY: clean
clean: down ## Stop stack and remove Docker volumes
	$(COMPOSE) down -v --remove-orphans
	@echo "$(GREEN)Volumes removed.$(RESET)"

.PHONY: clean-all
clean-all: clean ## Also remove built images
	docker rmi swarm-llm-coordinator swarm-llm-ui 2>/dev/null || true
	@echo "$(GREEN)Images removed.$(RESET)"

# ─────────────────────────────────────────────────────────────────────────────
## Help
# ─────────────────────────────────────────────────────────────────────────────

.PHONY: help
help: ## Show this help
	@echo ""
	@echo "  $(BOLD)swarm-llm$(RESET) — distributed LLM swarm control plane"
	@echo ""
	@awk 'BEGIN {FS = ":.*##"} \
	  /^## / { printf "\n  $(CYAN)%s$(RESET)\n", substr($$0,4) } \
	  /^[a-zA-Z_-]+:.*##/ { printf "    $(BOLD)%-20s$(RESET) %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@echo ""
