#!/usr/bin/env bash
# Backup do PostgreSQL (formato custom do pg_dump, compactado).
# Uso: ./scripts/backup.sh [dias_de_retencao]
#   Ex.: ./scripts/backup.sh 14   -> mantém apenas backups dos últimos 14 dias
set -euo pipefail

cd "$(dirname "$0")/.."
CONTAINER="${PG_CONTAINER:-telegram_postgres}"
RETENTION_DAYS="${1:-}"

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "Container $CONTAINER não está em execução." >&2
  exit 1
fi

mkdir -p backups
STAMP="$(date +%Y%m%d_%H%M%S)"
FILE="backups/telegram_vendas_${STAMP}.dump"

# As variáveis POSTGRES_USER/POSTGRES_DB são lidas de dentro do container
# (a senha não precisa ser exposta: conexão local via socket).
docker exec "$CONTAINER" sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc --no-owner' > "$FILE.partial"
mv "$FILE.partial" "$FILE"
chmod 600 "$FILE"

SIZE="$(du -h "$FILE" | cut -f1)"
echo "Backup criado: $FILE ($SIZE)"

if [[ -n "$RETENTION_DAYS" ]]; then
  find backups -maxdepth 1 \( -name "telegram_vendas_*.dump" -o -name "auto_*.dump" \) -type f -mtime +"$RETENTION_DAYS" -print -delete
fi
