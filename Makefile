.PHONY: help up down restart logs logs-backend logs-bot logs-db ps migrate makemigration seed admin check backup restore psql test db-up

help:
	@echo "make up             - sobe todos os serviços (build + migrations)"
	@echo "make down           - para os serviços (dados preservados)"
	@echo "make restart        - reinicia backend e bot"
	@echo "make logs           - acompanha os logs de todos os serviços"
	@echo "make migrate        - executa as migrations (alembic upgrade head)"
	@echo "make makemigration m='descricao' - gera nova migration"
	@echo "make seed           - cria planos/conteúdos iniciais"
	@echo "make admin u=admin  - cria administrador (superusuário)"
	@echo "make backup         - backup do banco em backups/"
	@echo "make restore f=backups/arquivo.dump - restaura backup"
	@echo "make psql           - abre o psql no container"
	@echo "make test           - roda os testes (requer .venv e banco ativo)"

up:
	docker compose up -d --build

db-up:
	docker compose up -d postgres

down:
	docker compose down

restart:
	docker compose restart backend telegram_bot

logs:
	docker compose logs -f --tail=200

logs-backend:
	docker compose logs -f --tail=200 backend

logs-bot:
	docker compose logs -f --tail=200 telegram_bot

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
