# Telegram Vendas — Funil de vendas automatizado (Python + Telegram + PostgreSQL)

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

---

## 1. Arquitetura

```
 TikTok ──► Página (/)  ──► t.me/SeuBot?start=tiktok
                               │
                    ┌──────────▼──────────┐       ┌──────────────────┐
                    │ telegram_bot        │──────►│  Mercado Pago    │
                    │ (python-telegram-bot│ cria  │  (PIX)           │
                    │  polling + jobs)    │ PIX   └───────┬──────────┘
                    └──────────┬──────────┘               │ webhook (assinado)
                               │                          ▼
                    ┌──────────▼──────────┐       ┌──────────────────┐
                    │ PostgreSQL 16       │◄──────│ backend (FastAPI)│
                    │ telegram_postgres   │       │ página, webhook, │
                    └─────────────────────┘       │ painel /admin    │
                                                  └──────────────────┘
```

| Serviço (compose) | Função |
|---|---|
| `postgres` | Banco (container `telegram_postgres`), volume persistente, healthcheck |
| `migrate` | Executa `alembic upgrade head` e encerra (backend e bot só iniciam depois dele) |
| `backend` | FastAPI: página inicial, `/webhooks/mercadopago`, painel `/admin`, `/health` |
| `telegram_bot` | Bot: atendimento, compra, entrega, expirações, controle de grupos |
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
├── bot.py                  # Bot do Telegram (processo separado)
├── config.py               # Configuração via .env (python-dotenv)
├── database.py             # SQLAlchemy: engine, sessões, Base
├── models.py               # Models de todas as tabelas
├── payments.py             # Cliente Mercado Pago (PIX) + validação de assinatura
├── manage.py               # CLI: create-admin, seed, check, reconcile...
├── seed_data.json          # Planos/categorias/conteúdos iniciais
├── admin/
│   ├── auth.py             # Argon2, sessão, CSRF, bloqueio por tentativas
│   └── routes.py           # Rotas do painel
├── services/
│   ├── access_service.py   # Liberação, validade, expiração, revogação
│   ├── payment_service.py  # Checkout, webhook, reconciliação (transacional)
│   ├── telegram_service.py # Entrega, convites, remoção de grupos, jobs
│   ├── catalog_service.py  # Planos e catálogo para o bot
│   ├── user_service.py     # Cadastro de usuários (UPSERT pelo Telegram ID)
│   └── texts.py            # Todas as mensagens do bot
├── templates/              # index.html (página) + admin/*.html
├── static/                 # css, js, images
├── migrations/             # Alembic (env.py + versions/)
├── scripts/                # backup.sh, restore.sh
├── tests/                  # Testes automatizados (PostgreSQL real)
├── backups/                # Destino dos backups (ignorado no Git)
├── Dockerfile, docker-compose.yml, Caddyfile, Makefile
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

1. No Telegram, abra **@BotFather** → `/newbot`.
2. Escolha o nome e o username (precisa terminar em `bot`, ex.: `MinhaLojaBot`).
3. Copie o token para `TELEGRAM_BOT_TOKEN` e o username (sem @) para `TELEGRAM_BOT_USERNAME`.
4. Recomendado no BotFather: `/setdescription`, `/setabouttext`, `/setuserpic`.
5. Descubra seu ID numérico (ex.: com **@userinfobot**) e coloque em `ADMIN_TELEGRAM_IDS` — libera os comandos de administrador (`/chatid` e captura de `file_id`).

Os comandos do menu (`/start`, `/planos`, `/meusacessos`, `/suporte`) são registrados automaticamente quando o bot inicia.

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
python bot.py                         # terminal 2: bot
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

**Testes automatizados** (usam o banco `telegram_vendas_test`, criado automaticamente; o cliente de pagamento falso existe apenas nos testes):
```bash
docker compose up -d postgres
docker compose run --rm -T -v "$PWD:/app" migrate sh -c \
  "pip install -q --target /tmp/pt pytest && PYTHONPATH=/tmp/pt python -m pytest -q -p no:cacheprovider"
# ou, com .venv local:  pytest -q
```
Cobrem: cadastro sem duplicidade, checkout e reaproveitamento de PIX, clique em "verificar" sem pagamento não libera, aprovação libera uma única vez (inclusive com 8 processamentos concorrentes), divergência de valor bloqueia, pagamento tardio é honrado, estorno revoga, renovação soma período, expiração e remoção de grupos, webhook (assinatura inválida → 401, duplicado → ignorado), login/CSRF/bloqueio/permissões do painel, edição de preço.

**Testes manuais:**
| Funcionalidade | Como testar |
|---|---|
| Página | Abra `/`, marque "Não sou um robô" → animação → botão leva para `t.me/<bot>?start=tiktok` |
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
- Senhas de admin com **Argon2id**; bloqueio temporário após 5 tentativas; tempo de resposta uniforme para usuário inexistente.
- Sessão em cookie assinado (`HttpOnly`, `SameSite=Lax`, `Secure` em produção), **CSRF** em todos os formulários, cabeçalhos CSP/HSTS/X-Frame-Options.
- Perfis: **Operador** (consulta, bloqueia usuários, encerra acessos) e **Superusuário** (planos, preços, conteúdos, administradores). Ações administrativas ficam registradas em `access_logs` com IP.
- Webhook com HMAC + reconsulta na API + validação de valor/pedido + idempotência; operações financeiras em transações com bloqueio de linha.
- SQL somente via ORM/consultas parametrizadas (SQLAlchemy).
- PostgreSQL com autenticação `scram-sha-256`, porta publicada apenas em `127.0.0.1`; containers da aplicação rodam com usuário não-root.
- A "verificação" da página é **apenas uma etapa visual de interação** — não é CAPTCHA e não protege contra bots.

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
# Boot-Telegram
