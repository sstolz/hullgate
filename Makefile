# hullgate — development, quality gate and operations targets (F-004).
#
# Session (development workstation)      the server (/opt/hullgate, clone of the repository)
#   make gate            quality gate        make deploy REF=<ref>   deployment
#   make fmt             format + lint --fix make backup             hot backup
#   make test            unit + integration  make restore-check      restore check
#   make test-container  image tests         make verify             acceptance checks
#   make image           local image         make status / logs      state and logs
#   make image-multiarch amd64 + arm64
#   make prod-smoke      prod + local Traefik    make verify-auth ROLE=<path>  foundation checks
#                                                make verify-connections CONNECTION=<name>  connections
#                                                make verify-relay ROLE=<path> [SENSITIVE=<path>] [TOOL=<name>]  relay
#   make bench-sdk       replay of the bench's v1 target (mcp SDK upgrade)
#   make test-browser    console in Chromium (F-016)    make verify-admin  console checks
#   make clean-images    purge of the build images
#   make qa              analysis tools (F-007): coverage, dependency audit, SonarQube
#   make qa-cov | qa-audit | sonar    one tool alone; `make sonar`: analysis of main, on go

SHELL := /bin/sh
.DEFAULT_GOAL := help

UV ?= uv
RUN := $(UV) run --locked --no-sync
IMAGE ?= hullgate:dev
# F-016 (BR-035 v2, BR-029 v2): the console's Node, pinned by version and by index digest
# (26.13.0-trixie-slim); the Dockerfile's `console` stage names the same digest.
NODE_VERSION := 26.13.0
NODE_IMAGE := node@sha256:5adad64f9a1fca2d51972caa65983634c8a774eb300d72b20f7f2a0f60368072
WEB := sh scripts/web.sh $(NODE_VERSION) $(NODE_IMAGE)
PLATFORMS := linux/amd64 linux/arm64

# One gate step: prints the tool and its version, runs the command, stops on `GATE: KO <step>`.
# $(1) step, $(2) version command, $(3) command.
define gate_step
	@printf '== GATE %s : %s\n' '$(1)' "$$($(2) 2>&1 | head -n 1)"
	@$(3) || { echo 'GATE: KO $(1)'; exit 1; }
endef

.PHONY: help
help:
	@sed -n '2,18p' Makefile

# ---------------------------------------------------------------------------------------------
# Development and quality gate (session)
# ---------------------------------------------------------------------------------------------

.PHONY: sync
sync:
	@echo '== uv sync --locked'
	@$(UV) sync --locked --quiet

.PHONY: gate
gate:
	@echo '== GATE : uv sync --locked'
	@$(UV) sync --locked --quiet || { echo 'GATE: KO lock'; exit 1; }
	$(call gate_step,lock,$(UV) --version,$(UV) lock --check --quiet)
	$(call gate_step,format,$(RUN) ruff --version,$(RUN) ruff format --check .)
	$(call gate_step,lint,$(RUN) ruff --version,$(RUN) ruff check .)
	$(call gate_step,typecheck,$(RUN) mypy --version,$(RUN) mypy --strict)
	$(call gate_step,tests,$(RUN) pytest --version,$(RUN) python scripts/operator_names.py && rm -f coverage.sha && $(RUN) pytest tests/unit tests/integration --cov --cov-report=xml && python3 scripts/qa_tree.py coverage.sha)
	$(call gate_step,sdk-guard,$(RUN) python --version,$(RUN) python scripts/sdk_guard.py)
	$(call gate_step,web,$(WEB) version,$(WEB) gate)
	@echo 'GATE: OK'

.PHONY: fmt
fmt: sync
	@echo '== ruff format ; ruff check --fix'
	$(RUN) ruff format .
	$(RUN) ruff check --fix .

.PHONY: lint
lint: sync
	@echo '== ruff format --check ; ruff check'
	$(RUN) ruff format --check .
	$(RUN) ruff check .

.PHONY: typecheck
typecheck: sync
	@echo '== mypy --strict'
	$(RUN) mypy --strict

.PHONY: test
test: sync
	@echo '== pytest tests/unit tests/integration'
	$(RUN) pytest tests/unit tests/integration

.PHONY: test-container
test-container: sync image image-multiarch
	@echo '== pytest tests/container (image $(IMAGE))'
	HULLGATE_IMAGE=$(IMAGE) $(RUN) pytest tests/container

# F-016 (BR-035 v2): category `browser`, outside the gate, required before a PR touching web/ or
# src/hullgate/admin/. Playwright's pinned image, Chromium with a CDP virtual authenticator, against
# the real image; captures under docs/captures/F-016/.
PLAYWRIGHT_IMAGE := mcr.microsoft.com/playwright/python@sha256:f9294526294a7bdfb8ca8aa7d815c049125a10e73b65d47fb9a80f5d74f284a6
.PHONY: test-browser
test-browser: image
	@echo '== tests/browser (image $(IMAGE), Playwright $(PLAYWRIGHT_IMAGE))'
	sh tests/browser/run.sh '$(IMAGE)' '$(PLAYWRIGHT_IMAGE)'

# ---------------------------------------------------------------------------------------------
# Quality analysis tools (F-007) — outside the gate (BR-101), run by `make qa`
# ---------------------------------------------------------------------------------------------
#
# QA_TOOLS is the only place where a tool's role is declared (BR-099): <name>:<role>, role
# `blocking` or `info`. One tool = one `qa-<name>` target that prints `== QA <name> : <version>` then
# exactly one line `QA <name> <role> <verdict> — <detail>` (BR-098). To add a tool:
#   1. pin the tool (`==` in the dev group of pyproject.toml, or image QA_IMAGE_<name> @sha256:);
#   2. write the qa-<name> target; 3. add <name>:info to QA_TOOLS;
#   4. write tests/unit/test_br_NNN_qa_<name>.py (the tool's contract test).
# To move a tool from info to blocking: change the word below, under BR-100's condition.
QA_TOOLS := cov:info audit:blocking sonar:info web-audit:info secrets:blocking

# BR-326: bundled scanner 1.1.0, source checksum in qa/secrets-scanner.json.
# Offline native execution; the identical source can run in this pinned runtime image.
QA_IMAGE_secrets := python@sha256:b3575dcf816cdacc5292df77a6e3e59b8941631078373a045efa2ac1e9c5c3b2
SECRETS_RUNTIME ?= native

.PHONY: qa-secrets
qa-secrets:
	@python3 scripts/qa_secrets.py '$(call qa_role,secrets)' '$(SECRETS_RUNTIME)'

# SonarQube scanner image, pinned by the digest of tag 12 (a single architecture: amd64).
QA_IMAGE_sonar := sonarsource/sonar-scanner-cli@sha256:47956b669bf486bdad919e1cbd0533223515aa0b6d1d31763e5a3754942df6d7

# Role of a tool, read from QA_TOOLS: $(call qa_role,<name>).
qa_role = $(patsubst $(1):%,%,$(filter $(1):%,$(QA_TOOLS)))

.PHONY: qa
qa:
	@fail=''; \
	for tool in $(QA_TOOLS); do \
	  name=$${tool%%:*}; role=$${tool#*:}; \
	  out=$$($(MAKE) -f $(firstword $(MAKEFILE_LIST)) --no-print-directory qa-$$name QA_AGGREGATE=1 2>&1); \
	  rc=$$?; \
	  printf '%s\n' "$$out"; \
	  if ! printf '%s\n' "$$out" | python3 scripts/qa_check.py "$$name" "$$role" "$$rc"; then \
	    [ "$$role" = info ] || fail="$$fail $$name"; \
	  fi; \
	done; \
	if [ -n "$$fail" ]; then echo "QA: KO$$fail"; exit 1; fi; \
	echo 'QA: OK'

.PHONY: qa-cov
qa-cov: sync
	@printf '== QA cov : %s\n' "$$($(RUN) coverage --version | head -n 1)"
	@$(RUN) python scripts/qa_cov.py '$(call qa_role,cov)'

.PHONY: qa-audit
qa-audit: sync
	@printf '== QA audit : %s\n' "$$($(RUN) pip-audit --version)"
	@$(RUN) python scripts/qa_audit.py '$(call qa_role,audit)'

# F-016 (BR-231): the console's lockfile; enters `info`, blocking once green on main (BR-100).
.PHONY: qa-web-audit
qa-web-audit:
	@printf '== QA web-audit : %s\n' "$$($(WEB) version), npm audit"
	@python3 scripts/qa_web_audit.py '$(call qa_role,web-audit)' '$(NODE_VERSION)' '$(NODE_IMAGE)'

.PHONY: qa-sonar sonar
qa-sonar:
	@printf '== QA sonar : %s\n' "sonar-scanner-cli $(QA_IMAGE_sonar)"
	@python3 scripts/qa_sonar.py '$(call qa_role,sonar)' '$(QA_IMAGE_sonar)' $(if $(QA_AGGREGATE),--aggregate)
sonar: qa-sonar

# ---------------------------------------------------------------------------------------------
# Image (session)
# ---------------------------------------------------------------------------------------------

.PHONY: image
image:
	@echo '== docker buildx build --load -t $(IMAGE) (workstation architecture)'
	docker buildx build --load -t $(IMAGE) .

.PHONY: image-multiarch
image-multiarch:
	@echo '== docker buildx build: one --load build per architecture, same Dockerfile'
	@set -e; for p in $(PLATFORMS); do \
	  tag="hullgate:multiarch-$${p#linux/}"; \
	  echo "-- $$p -> $$tag"; \
	  docker buildx build --platform "$$p" --load -t "$$tag" . ; \
	done
	@for p in $(PLATFORMS); do \
	  tag="hullgate:multiarch-$${p#linux/}"; \
	  printf 'IMAGE %s %s %s bytes\n' "$$tag" \
	    "$$(docker image inspect -f '{{.Os}}/{{.Architecture}}' "$$tag")" \
	    "$$(docker image inspect -f '{{.Size}}' "$$tag")"; \
	done

.PHONY: clean-images
clean-images:
	@echo '== removing the build images hullgate:multiarch-*, hullgate:dev, buildx cache'
	-docker image rm hullgate:multiarch-amd64 hullgate:multiarch-arm64 $(IMAGE)
	-docker buildx prune -f

# ---------------------------------------------------------------------------------------------
# Local proof of the production composition (session) and SDK bench
# ---------------------------------------------------------------------------------------------

.PHONY: prod-smoke
prod-smoke:
	@echo '== deploy/smoke/smoke.sh: throwaway clone, local Traefik, make deploy, smoke controls'
	sh deploy/smoke/smoke.sh

.PHONY: bench-sdk
bench-sdk:
	@echo '== conclusive replay of the isolation bench v1 target (mcp SDK upgrade, BR-037)'
	@echo '   prerequisites: version changed in pyproject.toml, `uv lock`, both committed;'
	@echo '   at least 10 GB free; to run where buildx exists (toolbox, README section'
	@echo '   on upgrading the mcp SDK)'
	cd poc/isolation-bench && ./bench.sh --target v1 --purge-images
	$(RUN) python scripts/sdk_guard.py

# ---------------------------------------------------------------------------------------------
# Operations (the server) — each target goes through deploy/ops.sh, under the exclusive lock
# ---------------------------------------------------------------------------------------------

OPS := sh deploy/ops.sh

.PHONY: deploy
deploy:
	@test -n "$(REF)" || { echo 'usage: make deploy REF=<tag|branch|sha>'; exit 2; }
	@$(OPS) deploy '$(REF)'

.PHONY: backup
backup:
	@$(OPS) backup

.PHONY: restore-check
restore-check:
	@$(OPS) restore-check '$(FILE)'

.PHONY: verify
verify:
	@$(OPS) verify

# F-005 (AC-OP): four foundation checks, ROLE read back from `hullgate admin tree show`.
.PHONY: verify-auth
verify-auth:
	@$(OPS) verify-auth '$(ROLE)'

# F-006 (AC-OP): four connection checks, CONNECTION read back from `hullgate connection list`.
.PHONY: verify-connections
verify-connections:
	@$(OPS) verify-connections '$(CONNECTION)'

# F-008 (AC-OP): five relay checks, ROLE read back from `hullgate tree show`, operator token(s)
# on standard input (line 1: ROLE, line 2: SENSITIVE), TOOL read back from the `reading tool` lines.
.PHONY: verify-relay
verify-relay:
	@$(OPS) verify-relay '$(ROLE)' '$(SENSITIVE)' '$(TOOL)'

# F-016 (AC-OP-01): four read-only checks of the console, banner VERIFY-ADMIN: n/4.
.PHONY: verify-admin
verify-admin:
	@$(OPS) verify-admin

.PHONY: status
status:
	@$(OPS) status

.PHONY: logs
logs:
	@$(OPS) logs
