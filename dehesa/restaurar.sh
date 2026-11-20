#!/bin/bash
# Restaura o projecto Dehesa num servidor novo (apos git clone para /home/balta/dehesa).
# Recria loja/ (repo, ramo main) e preview/ (worktree, ramo rascunho) a partir de loja.bundle,
# e os ambientes virtuais. Nao precisa de sudo.
set -e
cd "$(dirname "$0")"
ROOT=$PWD
[ "$ROOT" = /home/balta/dehesa ] || echo "AVISO: os caminhos em editor/app.py e nos .service assumem /home/balta/dehesa (esta em $ROOT)"

if [ ! -e loja/.git ]; then
  mkdir -p loja
  git -C loja init -q -b main
  git -C loja fetch -q "$ROOT/loja.bundle" 'refs/heads/*:refs/heads/*' --update-head-ok
  git -C loja checkout -q -f main
fi
[ -e preview/.git ] || git -C loja worktree add "$ROOT/preview" rascunho

mkdir -p editor/data/uploads
# O codigo da loja vem do bundle; o editor/ e o instagram/ vem deste repo.
for d in loja editor; do
  [ -d $d/venv ] || python3 -m venv $d/venv
  $d/venv/bin/pip install -q -r $d/requirements.txt
done
cat <<MSG
Pronto. Falta (com sudo):
  1. sudo bash loja/instalar.sh      # servico dehesa-loja (porta 5055)
  2. sudo bash editor/instalar.sh    # servico dehesa-editor (porta 5056)
     -> ambos escrevem no Caddy o host *.91.99.163.28.sslip.io: edita para o IP/dominio novo
        (dominio real: ver loja/caddy-dominio.txt e DNS de dehesaiberia.com)
  3. O editor usa o Claude Code CLI em /home/balta/.local/bin/claude: instala e faz login.
MSG
