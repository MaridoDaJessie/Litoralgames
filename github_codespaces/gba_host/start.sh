#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -f .env ]]; then
  token="$(python3 -c 'import secrets;print(secrets.token_urlsafe(36))')"
  (umask 077 && printf 'GBA_TOKEN=%s\nGBA_BIND=0.0.0.0\nGBA_PORT=8765\nGBA_STREAM_FPS=30\n' "$token" > .env)
  echo 'Chave de teste criada em gba_host/.env (não enviar ao GitHub).'
fi
set -a
# .env é um arquivo local criado por este script; não copie comandos externos nele.
source .env
set +a
if [[ ! -f roms/super-mario-advance-4.gba ]]; then
  echo 'ERRO: coloque a ROM fornecida em gba_host/roms/super-mario-advance-4.gba' >&2
  exit 1
fi
if [[ ! -x ../.venv/bin/python ]]; then
  echo 'ERRO: ambiente Python ausente. Espere o postCreateCommand terminar.' >&2
  exit 1
fi
echo 'Servidor iniciando. Abra http://localhost:8765 para o painel de teste.'
exec ../.venv/bin/python host.py
