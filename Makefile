COMPOSE?=docker compose
DEV?=-f docker-compose.yml -f docker-compose.dev.yml
STAGE?=-f docker-compose.yml -f docker-compose.stage.yml
PROD?=-f docker-compose.yml -f docker-compose.prod.yml

up: 
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

up-dev:
	$(COMPOSE) $(DEV) --env-file .env.dev up -d --build

down-dev:
	$(COMPOSE) $(DEV) down

up-stage:
	COMMIT_SHA=$$(git rev-parse --short HEAD) $(COMPOSE) $(STAGE) --env-file .env.stage up -d

up-prod:
	COMMIT_SHA=$$(git rev-parse --short HEAD) $(COMPOSE) $(PROD) --env-file .env.prod up -d

logs:
	$(COMPOSE) logs -f --tail=100

ps:
	$(COMPOSE) ps