#!/usr/bin/env bash
# Restaura um backup gerado por scripts/backup.sh.
# Uso: ./scripts/restore.sh backups/telegram_vendas_AAAAMMDD_HHMMSS.dump
#
# ATENÇÃO: substitui os dados atuais do banco pelos do backup.
set -euo pipefail

cd "$(dirname "$0")/.."
CONTAINER="${PG_CONTAINER:-telegram_postgres}"
FILE="${1:-}"

if [[ -z "$FILE" || ! -f "$FILE" ]]; then
  echo "Uso: $0 <arquivo.dump>" >&2
  ls -1t backups/*.dump 2>/dev/null | head -n 10 || true
  exit 1
fi

echo "Isto vai SUBSTITUIR os dados atuais do banco pelo backup: $FILE"
read -r -p "Digite RESTAURAR para confirmar: " ANSWER
if [[ "$ANSWER" != "RESTAURAR" ]]; then
  echo "Cancelado."
  exit 1
fi

echo "Fazendo backup de segurança do estado atual antes de restaurar..."
./scripts/backup.sh

echo "Parando backend e bot..."
docker compose stop backend telegram_bot >/dev/null 2>&1 || true

echo "Restaurando..."
docker exec -i "$CONTAINER" sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --single-transaction' < "$FILE"

echo "Aplicando migrations (caso o backup seja de uma versão anterior)..."
docker compose run --rm migrate

echo "Iniciando backend e bot..."
docker compose up -d backend telegram_bot
echo "Restauração concluída."
