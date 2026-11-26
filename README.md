# Telegram Vendas — Vendas próprias + Bots de ofertas (Python + Telegram + WhatsApp + PostgreSQL)

Página de entrada → bot do Telegram → cobrança PIX (Mercado Pago) → confirmação por webhook → liberação automática do conteúdo, com painel administrativo. Banco de dados: **PostgreSQL 16 em Docker**.

## Sumário
1. [Arquitetura](#1-arquitetura)
2. [Estrutura de pastas](#2-estrutura-de-pastas)
3. [Pré-requisitos e instalação](#3-pré-requisitos-e-instalação)
4. [Criar o bot no BotFather](#4-criar-o-bot-no-botfather)
5. [Configurar o PIX (Mercado Pago)](#5-configurar-o-pix-mercado-pago)
6. [Subir tudo com Docker Compose](#6-subir-tudo-com-docker-compose)
7. [Executar localmente (fora do Docker)](#7-executar-localmente-fora-do-docker)
8. [Comandos do dia a dia](#8-comandos-do-dia-a-dia)
9. [Migrations (Alembic)](#9-migrations-alembic)
10. [Backup e restauração](#10-backup-e-restauração)
11. [Grupos e canais privados](#11-grupos-e-canais-privados)
12. [Hospedar em um servidor](#12-hospedar-em-um-servidor)
13. [Como testar cada funcionalidade](#13-como-testar-cada-funcionalidade)
14. [Segurança](#14-segurança)
15. [Banco de dados](#15-banco-de-dados)
16. [Módulo de ofertas: bots de ofertas](#16-módulo-de-ofertas-bots-de-ofertas-mercado-livre-e-shopee--telegram-e-whatsapp)
17. [Módulo de vendas: cupons, recuperação de PIX, upsell e origem](#17-módulo-de-vendas-cupons-recuperação-de-pix-upsell-e-origem)
18. [Operação](#18-operação)

---

## 1. Arquitetura

O sistema tem **dois módulos independentes**, cada um com processo, bot(s) e configurações próprios. Eles só compartilham o banco, o painel e o cadastro de administradores.

| | Módulo de **VENDAS** (bot próprio) | Módulo de **OFERTAS** (afiliados) |
|---|---|---|
| Objetivo | Vender **seus** produtos/serviços pelo Telegram | Alimentar **seus grupos de ofertas** com promoções do Mercado Livre, da Shopee e da Amazon |
| Bots | **1** bot do Telegram (`TELEGRAM_BOT_TOKEN` no `.env`) | **Vários** bots, cadastrados no painel. Cada um com token próprio do Telegram e número próprio de WhatsApp |
| Dinheiro | Gateway de pagamento configurado por você (hoje Mercado Pago, PIX), direto na **sua conta** | Comissão do programa de afiliados (sem cobrança no sistema) |
| Processo (compose) | `telegram_bot` → `python sales_bot.py` | `offers_bot` → `python offers_bot.py` |
| Código | `sales/` | `offers/` |
| Painel | Vendas · bot próprio | Ofertas · afiliados |

```
 MÓDULO DE VENDAS
 TikTok ──► Página (/) ──► t.me/SeuBot?start=tiktok
                               │
                    ┌──────────▼──────────┐       ┌──────────────────┐
                    │ telegram_bot        │──────►│ Gateway (hoje:   │
                    │ sales_bot.py        │ cria  │ Mercado Pago PIX)│
                    └──────────┬──────────┘ PIX   └───────┬──────────┘
                               │                          │ webhook (assinado)
                    ┌──────────▼──────────┐       ┌───────▼──────────┐
                    │ PostgreSQL 16       │◄──────│ backend (FastAPI)│
                    └──────────▲──────────┘       │ página + painel  │
                               │                  └──────────────────┘
 MÓDULO DE OFERTAS             │
                    ┌──────────┴──────────┐  API   ┌──────────────────┐
                    │ offers_bot          │◄──────►│ Mercado Livre    │
                    │                     │◄──────►│ Shopee (afiliado)│
                    │ offers_bot.py       │        └──────────────────┘
                    │  ├ Bot de ofertas 1 ├──► canais/grupos Telegram (token 1)
                    │  ├ Bot de ofertas 2 ├──► canais/grupos Telegram (token 2)
                    │  └ ...              ├──► grupos WhatsApp (1 número por bot)
                    └─────────────────────┘        via serviço `whatsapp` (Evolution API)
```

| Serviço (compose) | Função |
|---|---|
| `postgres` | Banco (container `telegram_postgres`), volume persistente, healthcheck |
| `migrate` | Executa `alembic upgrade head` e encerra (os demais só iniciam depois dele) |
| `backend` | FastAPI: painel `/admin` (o endereço `/` abre o painel), `/webhooks/mercadopago`, `/privacidade`, `/health` |
| `telegram_bot` | **Vendas**: atendimento, compra, entrega, expirações, controle de grupos pagos |
| `offers_bot` | **Ofertas**: roda todos os bots de ofertas (busca, Telegram, WhatsApp) |
| `whatsapp` | Gateway do WhatsApp (Evolution API) — uma instância (número) por bot de ofertas |
| `backup` | Backup diário do banco |
| `caddy` (opcional, `--profile proxy`) | HTTPS automático (Let's Encrypt) na frente do backend |

**Fluxo do pagamento (nunca libera por clique):**
1. Usuário escolhe um plano → bot cria o pedido (`orders`) e a cobrança PIX no Mercado Pago → envia QR Code + copia e cola.
2. Mercado Pago chama o webhook → backend **valida a assinatura HMAC** → registra em `webhook_events` (idempotente) → **consulta o pagamento na API do Mercado Pago** → confere pedido (`external_reference`), **valor**, **moeda** e **método** → em uma transação com `SELECT ... FOR UPDATE`, marca o pedido como pago e cria o acesso em `subscriptions` (UNIQUE por pedido).
3. O bot verifica a cada 10 s pedidos pagos ainda não entregues e envia o conteúdo (convites individuais para grupos, links, arquivos, textos).
4. Rede de segurança: a cada 2 min o bot reconsulta pedidos pendentes na API (caso um webhook se perca). O botão "Já paguei — verificar" também consulta a API — nunca libera por si só.
5. A cada 5 min: assinaturas vencidas são expiradas, o usuário é removido dos grupos/canais e avisado; lembretes de vencimento são enviados.

## 2. Estrutura de pastas

```
.
├── app.py                  # Backend FastAPI (página, webhook, painel)
├── sales_bot.py            # VENDAS: processo do bot de vendas
├── offers_bot.py           # OFERTAS: processo que roda todos os bots de ofertas
├── config.py               # Configuração via .env (python-dotenv)
├── database.py             # SQLAlchemy: engine, sessões, Base
├── models.py               # Models de todas as tabelas
├── manage.py               # CLI: create-admin, seed, check, reconcile, bot-health, purge...
├── seed_data.json          # Planos/categorias/conteúdos iniciais
├── sales/                  # ===== MÓDULO DE VENDAS =====
│   ├── payments.py         # Gateway de pagamento (hoje Mercado Pago) — ponto de troca
│   ├── payment_service.py  # Checkout (com cupom), webhook, reconciliação (transacional)
│   ├── access_service.py   # Liberação, validade, expiração, revogação
│   ├── sales_service.py    # Cupons, recuperação de PIX, upsell, funil por origem
│   ├── telegram_service.py # Entrega, convites, remoção de grupos, lembretes
│   ├── catalog_service.py  # Planos e catálogo
│   ├── user_service.py     # Cadastro de usuários (UPSERT pelo Telegram ID)
│   ├── alerts.py           # Alertas e resumo diário das vendas
│   ├── texts.py            # Todas as mensagens do bot de vendas
│   └── bot/                # Handlers: menu, planos, /cupom, PIX, admin, grupos, jobs
├── offers/                 # ===== MÓDULO DE OFERTAS =====
│   ├── bots.py             # Cadastro dos bots: token, número, destinos, estado
│   ├── config.py           # Configuração de cada bot (filtros, modelo, ritmo do WhatsApp)
│   ├── collector.py        # Busca no Mercado Livre por bot, filtros e fila
│   ├── posting.py          # Publicação nos canais/grupos do Telegram
│   ├── whatsapp.py         # Envio aos grupos do WhatsApp (Evolution API), limites por número
│   ├── converter.py        # Modo "colar link" (admin manda o link ao bot de ofertas)
│   ├── post_template.py    # Modelo do post (Telegram + WhatsApp)
│   ├── mercadolivre.py     # Cliente da API do Mercado Livre
│   ├── shopee.py           # Cliente da API de afiliados da Shopee (GraphQL assinado)
│   ├── amazon.py           # Cliente da Creators API da Amazon (OAuth2)
│   ├── alerts.py           # Alertas e resumo diário de cada bot de ofertas
│   ├── telegram_bot.py     # Handlers do Telegram de cada bot de ofertas
│   └── runtime.py          # Laço do processo: liga/desliga bots, busca, publica
├── core/                   # ===== COMPARTILHADO =====
│   ├── settings_service.py # Configurações do painel (segredos criptografados)
│   ├── alert_service.py    # Vínculo do Telegram dos admins, cooldown de alertas, sinal de vida
│   ├── audit.py            # Relógio UTC e registro de auditoria
│   ├── error_reporter.py   # Avisa os admins no Telegram quando ocorre um erro
│   └── retention_service.py# LGPD: limpeza automática e anonimização
├── admin/
│   ├── auth.py             # Argon2, sessão, CSRF, limites de tentativas
│   ├── totp.py             # Verificação em duas etapas (TOTP) + QR Code
│   ├── common.py           # Templates, flash, paginação, auditoria
│   ├── routes.py           # Junta as telas sob /admin
│   └── views/              # Uma tela por arquivo (vendas: sales, funnel, coupons, catalog;
│                           #   ofertas: offers; geral: dashboard, accounts, history, auth_views)
├── templates/              # index.html (página), privacy.html + admin/*.html
├── static/                 # css, js, images
├── migrations/             # Alembic (env.py + versions/)
├── scripts/                # backup.sh, restore.sh
├── tests/                  # Testes automatizados (PostgreSQL real)
├── backups/                # Destino dos backups (ignorado no Git)
├── Dockerfile, docker-compose.yml, Caddyfile, Makefile, pyproject.toml (ruff/pytest)
├── requirements.txt, requirements-dev.txt
└── .env.example            # Modelo de configuração (o .env real NÃO vai para o Git)
```

## 3. Pré-requisitos e instalação

- Docker 24+ e Docker Compose v2 (`docker compose version`).
- (Opcional, para rodar fora do Docker) Python 3.11+ com `venv`.

```bash
cp .env.example .env
# Gere segredos:
openssl rand -hex 32   # -> SECRET_KEY
openssl rand -hex 24   # -> POSTGRES_PASSWORD (use a MESMA senha dentro de DATABASE_URL)
```

Edite o `.env` e preencha no mínimo: `SECRET_KEY`, `POSTGRES_PASSWORD`, `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, `MP_ACCESS_TOKEN`, `MP_WEBHOOK_SECRET`, `PUBLIC_BASE_URL`.

Dependências Python (somente para rodar fora do Docker):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
```

## 4. Criar o bot no BotFather

Este é o bot de **vendas**. Os bots de **ofertas** são outros bots, criados do mesmo jeito e cadastrados no painel (seção 16).

1. No Telegram, abra **@BotFather** → `/newbot`.
2. Escolha o nome e o username (precisa terminar em `bot`, ex.: `MinhaLojaBot`).
3. Copie o token para `TELEGRAM_BOT_TOKEN` e o username (sem @) para `TELEGRAM_BOT_USERNAME`.
4. Recomendado no BotFather: `/setdescription`, `/setabouttext`, `/setuserpic`.
5. Descubra seu ID numérico (ex.: com **@userinfobot**) e coloque em `ADMIN_TELEGRAM_IDS` — libera os comandos de administrador (`/chatid` e captura de `file_id`).

Os comandos do menu (`/start`, `/planos`, `/cupom`, `/meusacessos`, `/suporte`) são registrados automaticamente quando o bot inicia.

## 5. Configurar o PIX (Mercado Pago)

1. Crie uma conta em <https://www.mercadopago.com.br> com **chave PIX cadastrada**.
2. Acesse <https://www.mercadopago.com.br/developers/panel/app> → **Criar aplicação** (produto: Pagamentos online / Checkout Transparente).
3. Em **Credenciais de produção**, copie o **Access Token** para `MP_ACCESS_TOKEN`.
   - Para testes, use as **credenciais de teste** e usuários de teste do Mercado Pago.
4. Em **Webhooks** (na aplicação):
   - URL de produção: `https://SEU_DOMINIO/webhooks/mercadopago` (o mesmo `PUBLIC_BASE_URL` + `/webhooks/mercadopago`)
   - Evento: **Pagamentos**
   - Salve e copie a **assinatura secreta** para `MP_WEBHOOK_SECRET`.
5. `PUBLIC_BASE_URL` precisa ser **HTTPS público** (o Mercado Pago não chama `localhost`). Veja a seção 12.
6. `PAYER_EMAIL_DOMAIN`: o Mercado Pago exige e-mail do pagador; o sistema usa `tg<ID>@<seu domínio>`.

Validações aplicadas antes de liberar qualquer conteúdo:
- assinatura `x-signature` (HMAC-SHA256 com `MP_WEBHOOK_SECRET`) — webhooks sem assinatura válida recebem 401;
- o pagamento é **reconsultado na API** (o corpo do webhook nunca é usado como verdade);
- `external_reference` = código do pedido; valor e moeda iguais aos do pedido; método `pix`;
- idempotência por `webhook_events(provider, event_key)`, `payments(provider, provider_payment_id)` e `subscriptions(order_id)`.

> Sem `MP_ACCESS_TOKEN` o bot informa "Pagamentos indisponíveis". **Não existe modo de simulação de pagamento aprovado** no código de produção.

## 6. Subir tudo com Docker Compose

```bash
docker compose up -d --build          # sobe postgres, aplica migrations, sobe backend e bot
docker compose exec backend python manage.py seed                       # planos iniciais (R$ 2,50 / 5,00 / 15,00)
docker compose exec backend python manage.py create-admin --username admin --superuser
docker compose exec backend python manage.py check                      # confere configuração
```

- Página: <http://localhost:8000/>
- Painel: <http://localhost:8000/admin/>
- Saúde: <http://localhost:8000/health>

Dentro do Docker o host do banco é o **nome do serviço** (`postgres`) — o `docker-compose.yml` já monta a `DATABASE_URL` correta para backend, bot e migrate, ignorando o `localhost` do `.env`.

## 7. Executar localmente (fora do Docker)

O banco continua no Docker; backend e bot rodam na sua máquina usando `localhost` e a porta publicada (`POSTGRES_PORT`, só em 127.0.0.1).

```bash
docker compose up -d postgres         # apenas o banco
source .venv/bin/activate
alembic upgrade head                  # migrations
python manage.py seed
python manage.py create-admin --username admin --superuser
uvicorn app:app --reload --port 8000  # terminal 1: backend
python sales_bot.py                   # terminal 2: bot de vendas
python offers_bot.py                  # terminal 3: bots de ofertas
```

## 8. Comandos do dia a dia

| Ação | Comando | Atalho |
|---|---|---|
| Subir / atualizar | `docker compose up -d --build` | `make up` |
| Ver logs (todos) | `docker compose logs -f --tail=200` | `make logs` |
| Logs do bot / backend / banco | `docker compose logs -f telegram_bot` (ou `backend`, `postgres`) | `make logs-bot` |
| Status | `docker compose ps` | `make ps` |
| Parar (dados preservados) | `docker compose down` | `make down` |
| Reiniciar backend e bot | `docker compose restart backend telegram_bot` | `make restart` |
| Executar migrations | `docker compose run --rm migrate` | `make migrate` |
| Backup | `./scripts/backup.sh` | `make backup` |
| Restaurar | `./scripts/restore.sh backups/<arquivo>.dump` | `make restore f=...` |
| Abrir psql | `docker exec -it telegram_postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'` | `make psql` |
| Trocar senha de admin | `docker compose exec backend python manage.py set-password --username admin` | |
| Reconciliar pagamentos manualmente | `docker compose exec backend python manage.py reconcile` | |
| Desativar 2FA de um admin (perdeu o celular) | `docker compose exec backend python manage.py reset-2fa --username admin` | `make reset-2fa u=admin` |
| Limpar registros antigos agora (LGPD) | `docker compose exec backend python manage.py purge` | |
| Lint + testes num container | — | `make test-docker` |

> ⚠️ `docker compose down -v` **apaga o volume do banco**. Nunca use `-v` em produção.

## 9. Migrations (Alembic)

As tabelas são criadas **somente** por migrations (`migrations/versions/`). O serviço `migrate` roda `alembic upgrade head` a cada `docker compose up`.

Ao alterar `models.py`:

```bash
# gera a migration comparando models x banco (arquivo salvo em migrations/versions/)
docker compose run --rm -v "$PWD/migrations:/app/migrations" migrate alembic revision --autogenerate -m "descricao"
# revise o arquivo gerado, depois:
docker compose up -d --build           # rebuild + aplica

# outros comandos
docker compose run --rm migrate alembic current
docker compose run --rm migrate alembic history
docker compose run --rm migrate alembic downgrade -1
```

## 10. Backup e restauração

**Backup** (formato custom do `pg_dump`, compactado, permissão 600):
```bash
./scripts/backup.sh          # cria backups/telegram_vendas_AAAAMMDD_HHMMSS.dump
./scripts/backup.sh 14       # idem, e apaga backups com mais de 14 dias
```

Backup automático diário (cron do servidor, 03:00):
```cron
0 3 * * * cd /caminho/do/projeto && ./scripts/backup.sh 14 >> backups/backup.log 2>&1
```
Copie os backups para fora do servidor (outro disco, S3, etc.) — eles contêm dados pessoais: guarde com segurança.

**Restauração:**
```bash
./scripts/restore.sh backups/telegram_vendas_20250101_030000.dump
```
O script pede confirmação (digite `RESTAURAR`), faz um backup de segurança do estado atual, para backend/bot, restaura com `pg_restore --clean --single-transaction`, aplica migrations pendentes e reinicia os serviços.

Manual (equivalente):
```bash
docker exec telegram_postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > backup.dump
docker exec -i telegram_postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner' < backup.dump
```

## 11. Grupos e canais privados

1. Crie o grupo/canal **privado** e adicione o bot como **administrador** com as permissões **Convidar usuários via link** e **Banir usuários**.
2. Descubra o ID: no grupo, envie `/chatid` (seu ID deve estar em `ADMIN_TELEGRAM_IDS`); em canais, publique `/chatid`.
3. No painel → **Conteúdos** → novo conteúdo tipo **Grupo/canal privado** com o ID (ex.: `-1001234567890`) → vincule-o aos planos.

Comportamento:
- Após o pagamento, o comprador recebe um **convite individual** (`member_limit=1`, expira em `INVITE_LINK_TTL_HOURS`).
- Quem entrar no grupo **sem acesso válido** é removido automaticamente (o bot recebe as atualizações `chat_member` por ser admin).
- Quando a assinatura vence (ou é encerrada/estornada), o bot remove o usuário (ban + unban, permitindo voltar após renovar) e avisa por mensagem — exceto se ele tiver outro acesso válido ao mesmo grupo.
- Renovações somam o período ao final do acesso vigente.

Arquivos, fotos e vídeos: envie o arquivo ao bot pelo seu Telegram de admin; ele responde com o `file_id` para cadastrar no painel. O envio usa `protect_content` (impede encaminhar/salvar no Telegram).

## 12. Hospedar em um servidor

1. VPS Linux (ex.: Ubuntu 22.04+, 1 vCPU / 1 GB RAM) com Docker instalado e um **domínio** apontando (registro A) para o IP do servidor.
2. Copie o projeto (git clone / scp) e crie o `.env` no servidor:
   ```
   APP_ENV=production
   PUBLIC_BASE_URL=https://seudominio.com.br
   DOMAIN=seudominio.com.br
   SESSION_COOKIE_SECURE=true
   ```
   Em produção o backend **não inicia** sem `MP_WEBHOOK_SECRET` e `SECRET_KEY` válidos.
3. Suba com o proxy HTTPS (Caddy obtém o certificado automaticamente):
   ```bash
   docker compose --profile proxy up -d --build
   docker compose exec backend python manage.py seed
   docker compose exec backend python manage.py create-admin --username admin --superuser
   ```
4. Firewall: libere apenas 22, 80 e 443 (ex.: `ufw allow OpenSSH && ufw allow 80,443/tcp && ufw enable`). O PostgreSQL e o backend ficam publicados apenas em `127.0.0.1`. Se não precisar acessar o banco por ferramentas externas, remova o bloco `ports` do serviço `postgres`.
5. Configure o webhook no Mercado Pago com `https://seudominio.com.br/webhooks/mercadopago`.
6. Agende o backup diário (seção 10).
7. Atualizar a aplicação: `git pull && docker compose --profile proxy up -d --build` (migrations rodam automaticamente; dados preservados no volume `telegram_postgres_data`).

Já usa Nginx? Não ative o profile `proxy`; faça `proxy_pass http://127.0.0.1:8000;` com `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for; proxy_set_header X-Forwarded-Proto $scheme;`.

## 13. Como testar cada funcionalidade

**Testes automatizados** (usam o banco `telegram_vendas_test`, criado automaticamente — o banco real não é tocado; o cliente de pagamento falso existe apenas nos testes):
```bash
docker compose up -d postgres
make test-docker            # ruff + pytest dentro de um container
# ou, com .venv local:  ruff check . && pytest -q
```

Cobrem: cadastro sem duplicidade, checkout e reaproveitamento de PIX, clique em "verificar" sem pagamento não libera, aprovação libera uma única vez (inclusive com 8 processamentos concorrentes), divergência de valor bloqueia, pagamento tardio é honrado, estorno revoga, renovação soma período, expiração e remoção de grupos, webhook (assinatura inválida → 401 e nada gravado, duplicado → ignorado), login/CSRF/limites de tentativa/2FA/permissões do painel, cupons (validade, limite de usos, plano), recuperação de PIX, upsell, funil por origem, modelo do post (igual ao modelo de referência), retenção LGPD, aviso de erros e healthcheck do bot.

**Testes manuais:**
| Funcionalidade | Como testar |
|---|---|
| Página | Abra `/` → botão **Abrir no Telegram** leva para `t.me/<bot>?start=tiktok`; `/?start=tiktok-v12` repassa essa origem |
| Cupom | Painel → Cupons → crie `TESTE10`; no bot envie `/cupom TESTE10` (ou abra `t.me/<bot>?start=tiktok__TESTE10`) → planos mostram o preço com desconto |
| Recuperação de PIX | Gere um PIX e não pague: depois que ele expirar (+10 min), o bot manda "seu PIX expirou" com o botão **Gerar novo PIX** |
| 2FA | Minha conta → Verificação em duas etapas → escaneie o QR → saia e entre de novo: o painel pede o código |
| Cadastro | `/start` no bot → mensagem de boas-vindas; repita → "bem-vindo de volta"; veja em Painel → Usuários (um único registro) |
| Menus | Botões: Ver conteúdo, Conhecer planos, Comprar acesso, Meus acessos, Suporte |
| PIX | Comprar → QR Code + copia e cola + botões. Use credenciais de **teste** do Mercado Pago para pagar sem dinheiro real |
| Webhook | Painel do MP → Webhooks → "Simular notificação": deve aparecer em Painel → Webhooks (assinatura ✅; pagamento inexistente → `ignored`) |
| Liberação | Após o pagamento aprovado: mensagem "Pagamento aprovado" + conteúdo; Painel → Pedidos mostra pago/entregue |
| Verificar pagamento | "Já paguei — verificar" antes de pagar → "Ainda não recebemos" (nada liberado) |
| Grupo | Entre no grupo com o convite → permanece; entre com link comum sem ter pago → removido |
| Expiração | No psql: `UPDATE subscriptions SET starts_at=now()-interval '31 days', expires_at=now()-interval '1 minute' WHERE id=X;` → em até 5 min o bot expira, remove do grupo e avisa |
| Painel | Criar/editar plano e preço, ativar/desativar oferta, criar categorias/conteúdos, bloquear usuário, encerrar acesso, histórico e webhooks |
| Persistência | `docker compose down && docker compose up -d` → dados continuam |
| Backup/restore | `./scripts/backup.sh` e `./scripts/restore.sh <arquivo>` |

## 14. Segurança

- Segredos apenas no `.env` (ignorado pelo Git e pelo build do Docker). O token do bot nunca aparece no HTML/JS.
- Senhas de admin com **Argon2id**; tempo de resposta uniforme para usuário inexistente. Limites de tentativas: 20 erros por IP (qualquer usuário), 5 erros do mesmo usuário **no mesmo IP** (um atacante não consegue trancar o admin legítimo) e 50 erros na conta (ataque distribuído). Janela: `ADMIN_LOCKOUT_MINUTES`.
- **Verificação em duas etapas (TOTP)** opcional por administrador (Minha conta), com QR Code gerado no próprio servidor; o segredo fica criptografado e o mesmo código não pode ser usado duas vezes. Perdeu o celular: `make reset-2fa u=usuario`.
- O backend só confia no `X-Forwarded-For` de `FORWARDED_ALLOW_IPS` (padrão `127.0.0.1`) — ninguém consegue falsificar o IP para driblar os limites de login.
- Sessão em cookie assinado (`HttpOnly`, `SameSite=Lax`, `Secure` em produção), **CSRF** em todos os formulários, cabeçalhos CSP/HSTS/X-Frame-Options.
- Perfis: **Operador** (consulta, bloqueia usuários, encerra acessos) e **Superusuário** (planos, preços, conteúdos, administradores). Ações administrativas ficam registradas em `access_logs` com IP.
- Webhook com HMAC **validado antes de gravar qualquer coisa** + reconsulta na API + validação de valor/pedido + idempotência; operações financeiras em transações com bloqueio de linha.
- Erros (exceções dos jobs, do painel e do bot) são enviados aos administradores vinculados no Telegram, agrupados e sem repetir o mesmo erro por 30 min (`ERROR_REPORTS`). Tokens são removidos do texto.
- **LGPD:** página `/privacidade`; registros técnicos antigos apagados automaticamente (`LOG_RETENTION_DAYS`, `WEBHOOK_RETENTION_DAYS`); botão **Anonimizar** na página do usuário para pedidos de exclusão (pedidos e pagamentos são mantidos como registro financeiro).
- SQL somente via ORM/consultas parametrizadas (SQLAlchemy).
- PostgreSQL com autenticação `scram-sha-256`, porta publicada apenas em `127.0.0.1`; containers da aplicação rodam com usuário não-root.

## 15. Banco de dados

| Tabela | Conteúdo | Destaques |
|---|---|---|
| `users` | Usuários do Telegram | `telegram_id` UNIQUE, origem do acesso, bloqueio |
| `plans` | Planos/ofertas | `price NUMERIC(12,2)`, `access_type` (one_time/subscription), `duration_days`, ativo/inativo, CHECKs |
| `products` | Conteúdos/packs | tipo (grupo, link, arquivo, foto, vídeo, texto), categoria, ordem, ativo |
| `categories` | Categorias de conteúdo | ordem, ativo |
| `plan_products` | Conteúdos liberados por plano | PK composta, FKs com CASCADE |
| `orders` | Pedidos | `public_id` UUID UNIQUE, valor congelado, status, controle de entrega |
| `payments` | Pagamentos | UNIQUE `(provider, provider_payment_id)`, dados do PIX, erro de validação, resposta do provedor (JSONB) |
| `subscriptions` | Acessos e validade | `order_id` UNIQUE (sem liberação duplicada), início/fim, revogação, remoção dos chats |
| `access_logs` | Histórico de liberações/acessos/ações admin | JSONB, índices por usuário/ação/data |
| `webhook_events` | Eventos dos provedores | UNIQUE `(provider, event_key)`, assinatura, tentativas, resultado |
| `admin_users` | Administradores | username/e-mail UNIQUE, hash Argon2, bloqueio por tentativas |

Todas as tabelas têm PK, FKs com integridade referencial, índices para as consultas frequentes e `created_at`/`updated_at` (`TIMESTAMPTZ`, UTC).

## 16. Módulo de ofertas: bots de ofertas (Mercado Livre e Shopee → Telegram e WhatsApp)

Cada **bot de ofertas** alimenta os seus próprios canais/grupos. Exemplo: “Ofertas Tech” (@OfertasTechBot + 1 chip) nos grupos de tecnologia e “Ofertas Casa” (@OfertasCasaBot + outro chip) nos de casa. Tudo é feito no painel, sem editar arquivos nem reiniciar: o processo `offers_bot` lê os bots do banco a cada 10 s.

### Configuração do módulo (uma vez) — Painel → Ofertas · afiliados → **Lojas e APIs**
- **Mercado Livre:** em <https://developers.mercadolivre.com.br/devcenter> → **Criar aplicação** (qualquer URL de redirect com https). Copie o **App ID** e a **Secret Key**.
- **Shopee:** em <https://affiliate.shopee.com.br> → **Open API**, copie o **AppID** e a **Senha**. A conta de afiliado precisa ter o acesso à Open API liberado. A API devolve o **link de afiliado pronto** (`offerLink`, com a sua comissão), então não é preciso colar link à mão.
- Os segredos ficam **criptografados** e valem para todos os bots. Use **Testar** em cada loja.

### Criar um bot de ofertas
1. Painel → **Bots de ofertas** → **Criar bot** (dê um nome).
2. Aba **Telegram**: crie um bot novo no **@BotFather** (`/newbot`) **só para ofertas** e cole o token. O painel confere o token no Telegram, guarda criptografado e recusa o token do bot de vendas.
3. Adicione esse bot como **administrador** dos canais/grupos (permissão de publicar). Eles aparecem em **Encontrados pelo bot** para adicionar com 1 clique — ou digite `@canal` / `-100...`.
4. Aba **WhatsApp** (opcional): **Gerar QR Code** e leia com o **chip deste bot**, escolha os grupos e ative os envios.
5. Aba **Busca e filtros**: **lojas** (Mercado Livre e/ou Shopee), desconto mínimo, faixa de preço, palavras bloqueadas, horário, modo automático/manual; no Mercado Livre, categorias e palavras-chave; na Shopee, palavras-chave, IDs de categoria e ordenação (mais vendidos, maior comissão...).
6. Aba **Modelo do post**: texto das publicações (veja abaixo).
7. **Testar Mercado Livre** → **Buscar ofertas agora** → **Ligar bot**.

### Como funciona
- A cada *N* minutos (dentro do horário ativo do bot), ele busca nas **lojas ligadas naquele bot**: no Mercado Livre, os mais vendidos das categorias e/ou a busca por palavras-chave; na Shopee, a `productOfferV2` (API GraphQL assinada com SHA256) por palavra-chave/categoria. Calcula o desconto, aplica os filtros **daquele bot** e não repete o mesmo produto dentro do prazo.
- O produto é guardado uma vez (`promo_offers`); o que **cada bot** faz com ele (aguardando, fila, publicado, WhatsApp) fica em `offer_posts`. O mesmo produto pode sair em bots diferentes.
- **Automático:** as melhores (maior desconto) entram na fila do bot e saem respeitando o intervalo entre posts, em **todos os destinos do Telegram** do bot. **Manual:** ficam em **Ofertas → Aguardando** para você aprovar.
- **Pelo celular:** cole o link do Mercado Livre **ou da Shopee** (página do produto ou o seu link curto `s.shopee.com.br`) na conversa com o bot de ofertas (pode escrever `chamada: ...` e `cupom: ...` nas linhas de baixo). Ele responde com a prévia e os botões **Publicar (Telegram + WhatsApp)**, **Só no Telegram**, **Só no WhatsApp**, **Descartar**.
- **Alertas:** cada bot avisa os administradores pelo próprio Telegram (WhatsApp desconectado, falha na busca, canais sem token). Abra cada bot e envie `/start` para recebê-los.

### Modelo do post
Cada bot tem o seu modelo (aba **Modelo do post**), usado no Telegram **e** no WhatsApp. O padrão:

```
🆘 *PRECIN NA 43" DA PHILCO*

Smart TV Philco 43" Roku Tv Hdr10

Por R$1.095 pix 👑
⚠️ cupom: *DIADATECH*

Link: https://meli.la/33hYTSb
```

- Marcadores: `{chamada}` `{titulo}` `{preco}` `{de}` `{desconto}` `{cupom}` `{link}` `{frete}` `{rodape}` `{loja}` (Mercado Livre / Shopee).
- Formatação no padrão do WhatsApp: `*negrito*`, `_itálico_`, `~riscado~`. No Telegram vira HTML automaticamente.
- Linha com marcador vazio some (sem cupom → a linha do cupom não aparece). Sem chamada na oferta, usa a **chamada padrão** do bot.
- Na Shopee a API não informa o preço "de": ele é calculado a partir do percentual de desconto.

### Links de afiliado
**Shopee:** automático (vem da API). **Mercado Livre:** a loja **não oferece API** para gerar link de afiliado. Gere no **Linkbuilder** (<https://www.mercadolivre.com.br/afiliados/linkbuilder>) e cole na oferta (Painel → Ofertas), ou cole o link `meli.la` na conversa com o bot de ofertas. Link, chamada e cupom são da oferta (mesma conta de afiliado para todos os bots).

### WhatsApp (um número por bot)
Serviço `whatsapp` do compose (**Evolution API** v2.3.7, open source), que conecta cada número via QR Code como o WhatsApp Web. Cada bot de ofertas usa a instância `ofertas<ID>`.

> ⚠️ Não é a API oficial da Meta. Use **um chip dedicado por bot** e envie apenas para grupos que você administra. Se um número for bloqueado, os outros bots continuam.

- **Ritmo por número:** aquecimento nos primeiros dias, depois X ofertas a cada N horas, teto diário, pausa entre grupos, horário ativo e expiração de ofertas paradas na fila. O envio de um bot não consome o limite de outro.
- Envio somente para grupos (`@g.us`), nunca para contatos. O serviço não publica porta: só o backend e o `offers_bot` acessam, com a chave `EVOLUTION_API_KEY`.

```bash
make logs-offers                      # logs do módulo de ofertas
docker compose logs -f whatsapp       # logs do gateway do WhatsApp
```

## 17. Módulo de vendas: cupons, recuperação de PIX, upsell e origem

Vale só para o **bot de vendas** (seus produtos, pagos pelo gateway). Os bots de ofertas não passam por aqui.

**Gateway de pagamento:** escolhido por `PAYMENT_GATEWAY` no `.env` (hoje: `mercadopago`). O dinheiro cai direto na sua conta do gateway. Para usar outro gateway, ele é adicionado em `sales/payments.py` (classe com `create_pix_payment`/`get_payment`, registro em `GATEWAYS` e rota de webhook) — o resto do funil (pedidos, liberação, cupons) não muda.


- **Cupons** (Painel → Cupons): desconto de 1% a 90%, para todos os planos ou um só, com limite de usos (contam só pedidos pagos) e validade. O cliente usa com `/cupom CODIGO`, digitando o código na conversa, ou abrindo `t.me/<bot>?start=origem__CODIGO`. O valor com desconto é o valor do pedido — a liberação continua conferindo o valor pago.
- **Recuperação de PIX:** `RECOVERY_DELAY_MINUTES` (padrão 10) depois de um PIX expirar sem pagamento, o bot manda uma única mensagem com o botão **Gerar novo PIX**. Não manda se a pessoa já comprou/gerou outro PIX, está bloqueada, ou recebeu outra recuperação nas últimas 24 h. `0` desliga.
- **Renovar em 1 toque:** o lembrete de vencimento e o aviso de expiração têm o botão **Renovar agora (PIX)**.
- **Upsell:** depois de entregar o conteúdo, o bot oferece **uma vez** o plano mais barato entre os que custam mais que o comprado (prefere assinatura quando a compra foi um pack) e que a pessoa ainda não tem. `UPSELL_ENABLED=false` desliga.
- **Funil por origem** (Painel → Funil por origem): por origem do `/start`, quantos abriram o bot, geraram PIX e compraram, conversão, faturamento e ticket médio. Use um link por canal/vídeo: `t.me/<bot>?start=tiktok-v12` ou `https://seudominio/?start=tiktok-v12`.

## 18. Operação

- **Healthchecks:** o bot de vendas e o módulo de ofertas gravam, cada um, um sinal de vida por minuto; se um deles parar por mais de 3 min, o healthcheck encerra só aquele processo e o Docker o reinicia sozinho (`manage.py bot-health sales|offers`).
- **Aviso de erros:** os administradores vinculados recebem no Telegram os erros do sistema, agrupados (ex.: `×3`) e sem repetição por 30 min.
- **Retenção (LGPD):** a cada 6 h o bot apaga `access_logs` com mais de `LOG_RETENTION_DAYS` dias e `webhook_events` com mais de `WEBHOOK_RETENTION_DAYS`. Para rodar na hora: `python manage.py purge`.

