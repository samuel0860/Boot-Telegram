#!/bin/sh
# Backup diário automático do PostgreSQL (roda no serviço "backup" do docker-compose).
# - Faz 1 backup por dia no horário BACKUP_HOUR (padrão 03h).
# - Apaga backups com mais de BACKUP_KEEP_DAYS dias (padrão 14).
# - Arquivos em ./backups/auto_AAAAMMDD_HHMMSS.dump (formato do pg_dump -Fc).
set -eu

HOUR="${BACKUP_HOUR:-3}"
KEEP="${BACKUP_KEEP_DAYS:-14}"
DIR=/backups

echo "[backup] serviço iniciado: backup diário às ${HOUR}h, mantendo ${KEEP} dias"

until pg_isready -q; do sleep 5; done

while true; do
  today="$(date +%Y%m%d)"
  now_hour="$(date +%H | sed 's/^0//')"
  if [ "${now_hour:-0}" -ge "$HOUR" ] && ! ls "$DIR"/auto_"$today"_*.dump >/dev/null 2>&1; then
    file="$DIR/auto_$(date +%Y%m%d_%H%M%S).dump"
    if pg_dump -Fc --no-owner -f "$file.partial"; then
      mv "$file.partial" "$file"
      chmod 600 "$file"
      echo "[backup] criado: $file ($(du -h "$file" | cut -f1))"
      find "$DIR" -maxdepth 1 -name 'auto_*.dump' -type f -mtime +"$KEEP" -print -delete || true
    else
      echo "[backup] ERRO ao gerar backup" >&2
      rm -f "$file.partial"
    fi
  fi
  sleep 600
done
