.PHONY: help reset-2fa lint test-docker up down restart logs logs-backend logs-bot logs-offers logs-db ps migrate makemigration seed admin check backup restore psql test db-up

help:
	@echo "make up             - sobe todos os serviços (build + migrations)"
	@echo "make down           - para os serviços (dados preservados)"
	@echo "make restart        - reinicia backend, bot de vendas e módulo de ofertas"
	@echo "make logs-bot / logs-offers - logs do bot de vendas / do módulo de ofertas"
	@echo "make logs           - acompanha os logs de todos os serviços"
	@echo "make migrate        - executa as migrations (alembic upgrade head)"
	@echo "make makemigration m='descricao' - gera nova migration"
	@echo "make seed           - cria planos/conteúdos iniciais"
	@echo "make admin u=admin  - cria administrador (superusuário)"
	@echo "make reset-2fa u=admin - desativa o 2FA (perdeu o celular)"
	@echo "make backup         - backup do banco em backups/"
	@echo "make restore f=backups/arquivo.dump - restaura backup"
	@echo "make psql           - abre o psql no container"
	@echo "make test           - roda os testes (requer .venv e banco ativo)"
	@echo "make test-docker    - roda lint + testes num container (não precisa de .venv)"
	@echo "make lint           - verifica o código com ruff"

up:
	docker compose up -d --build

db-up:
	docker compose up -d postgres

down:
	docker compose down

restart:
	docker compose restart backend telegram_bot offers_bot

logs:
	docker compose logs -f --tail=200

logs-backend:
	docker compose logs -f --tail=200 backend

logs-bot:
	docker compose logs -f --tail=200 telegram_bot

logs-offers:
	docker compose logs -f --tail=200 offers_bot

logs-db:
	docker compose logs -f --tail=200 postgres

ps:
	docker compose ps

migrate:
	docker compose run --rm migrate

makemigration:
	docker compose run --rm -v "$(PWD)/migrations:/app/migrations" migrate alembic revision --autogenerate -m "$(m)"

seed:
	docker compose exec backend python manage.py seed

admin:
	docker compose exec backend python manage.py create-admin --username $(u) --superuser

reset-2fa:
	docker compose exec backend python manage.py reset-2fa --username $(u)

check:
	docker compose exec backend python manage.py check

backup:
	./scripts/backup.sh

restore:
	./scripts/restore.sh $(f)

psql:
	docker exec -it telegram_postgres sh -c 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

test:
	.venv/bin/pytest -q

lint:
	.venv/bin/ruff check .

# Roda ruff + pytest dentro de um container ligado ao Postgres do compose.
# Os testes usam um banco SEPARADO (<POSTGRES_DB>_test) — o banco real não é tocado.
test-docker:
	docker compose run --rm --no-deps -v "$(PWD):/app" -u root --entrypoint sh migrate -c \
	  "pip install -q -r requirements-dev.txt && ruff check . && pytest -q -p no:cacheprovider"
