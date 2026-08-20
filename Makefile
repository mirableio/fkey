DEPLOY_HOST ?= appuser@beta.fkey.app
DEPLOY_PATH ?= /opt/apps/fkey
PROD_ENV ?= $(if $(wildcard .env.prod),.env.prod,.env)

COMPOSE_LOCAL = docker compose -p fkey --project-directory . --env-file .env -f infra/docker-compose.local.yaml
COMPOSE_PROD = docker compose -p fkey --project-directory . --env-file .env.prod -f infra/docker-compose.prod.yaml

.PHONY: up down deploy backup

up:
	$(COMPOSE_LOCAL) up --build

down:
	$(COMPOSE_LOCAL) down

deploy:
	@test -r $(PROD_ENV) || { echo "Create .env.prod from .env.prod.example" >&2; exit 1; }
	@grep -Eq '^FKEY_SIGNUP_CODE=.+$$' $(PROD_ENV) || { echo "Set FKEY_SIGNUP_CODE in $(PROD_ENV)" >&2; exit 1; }
	@! grep -q '^FKEY_SIGNUP_CODE=replace-with-' $(PROD_ENV) || { echo "Replace the example FKEY_SIGNUP_CODE in $(PROD_ENV)" >&2; exit 1; }
	ssh $(DEPLOY_HOST) 'mkdir -p $(DEPLOY_PATH)'
	git ls-files -c -m -o --exclude-standard -z | rsync -avz --files-from=- --from0 ./ $(DEPLOY_HOST):$(DEPLOY_PATH)
	scp $(PROD_ENV) $(DEPLOY_HOST):$(DEPLOY_PATH)/.env.prod
	ssh $(DEPLOY_HOST) 'chmod 600 $(DEPLOY_PATH)/.env.prod'
	ssh -t $(DEPLOY_HOST) 'bash -l -c "cd $(DEPLOY_PATH) && sudo DOCKER_BUILDKIT=1 BUILDX_NO_DEFAULT_ATTESTATIONS=1 $(COMPOSE_PROD) up -d --build"'
	ssh $(DEPLOY_HOST) 'for i in $$(seq 1 30); do curl -fsS http://127.0.0.1:9040/signup >/dev/null && exit 0; sleep 1; done; echo "fkey did not become ready" >&2; exit 1'
	ssh -t $(DEPLOY_HOST) 'sudo ln -sfn $(DEPLOY_PATH)/infra/Caddyfile /etc/caddy/sites-enabled/fkey.caddy && sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy'

backup:
	ssh -t $(DEPLOY_HOST) 'bash -l -c "cd $(DEPLOY_PATH) && sudo $(COMPOSE_PROD) exec -T backup fkey-backup /app/data /app/backups"'
