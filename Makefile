COMPOSE = docker compose --env-file .env -f infra/docker-compose.local.yaml

.PHONY: up down

up:
	$(COMPOSE) up --build

down:
	$(COMPOSE) down
