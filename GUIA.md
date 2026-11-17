# Guia do sistema — Telegram Vendas + Bot de Ofertas

Manual de uso do dia a dia, em linguagem simples. A parte técnica (instalação, Docker, banco de dados) está no [README.md](README.md).

> 🔑 **Senhas e chaves** estão no arquivo **`ACESSOS.md`**, que fica **só no servidor** (não vai para o GitHub).

---

## Sumário
1. [O que o sistema faz](#1-o-que-o-sistema-faz)
2. [Como acessar](#2-como-acessar)
3. [Primeiros passos (checklist)](#3-primeiros-passos-checklist)
4. [O painel, tela por tela](#4-o-painel-tela-por-tela)
5. [Bot de ofertas: Mercado Livre → Telegram](#5-bot-de-ofertas-mercado-livre--telegram)
6. [Ofertas no WhatsApp](#6-ofertas-no-whatsapp)
7. [Postar uma oferta pelo celular (modo conversor)](#7-postar-uma-oferta-pelo-celular-modo-conversor)
8. [Vendas pelo bot (funil + PIX)](#8-vendas-pelo-bot-funil--pix)
9. [Alertas e resumo diário](#9-alertas-e-resumo-diário)
10. [Comandos do bot](#10-comandos-do-bot)
11. [Backup](#11-backup)
12. [Problemas comuns](#12-problemas-comuns)
13. [Para quem vai cuidar do servidor](#13-para-quem-vai-cuidar-do-servidor)

---

## 1. O que o sistema faz

| Parte | O que faz |
|---|---|
| **Página inicial** | Página "Verificação de acesso" para quem vem do TikTok. Leva a pessoa ao bot do Telegram. |
| **Bot do Telegram** (@lgr_ebac_bot) | Atende quem manda `/start`: mostra planos, gera PIX, libera o conteúdo depois do pagamento e controla a validade das assinaturas. |
| **Bot de ofertas** | Busca promoções no Mercado Livre e publica sozinho no seu **canal do Telegram**. |
| **WhatsApp** | Envia as mesmas ofertas para os seus **grupos do WhatsApp**, num ritmo seguro. |
| **Painel** | Onde você configura e acompanha tudo. |

Tudo roda junto em containers Docker: `postgres` (banco), `backend` (painel e página), `telegram_bot`, `whatsapp` e `backup`.

## 2. Como acessar

| O quê | Endereço |
|---|---|
| Painel administrativo | `http://<servidor>:8765/admin/` (hoje: http://10.10.0.111:8765/admin/) |
| Página inicial (TikTok) | `http://<servidor>:8765/` |
| Bot | https://t.me/lgr_ebac_bot |

Usuário e senha do painel: veja o `ACESSOS.md`. Para trocar a senha: **Minha conta → Trocar senha**.

> Com o servidor definitivo e um domínio, os endereços mudam para `https://seudominio.com.br/admin/`.

## 3. Primeiros passos (checklist)

O **Dashboard** mostra o checklist "Primeiros passos" com o que ainda falta:

1. ✅ **Bot do Telegram no ar**: já está.
2. **Vincular o seu Telegram**: Minha conta → *Gerar código de vínculo* → mande `/vincular CÓDIGO` ao bot. Libera alertas, resumo diário e o modo conversor.
3. ✅ **Planos e conteúdos**: já existem 3 planos de exemplo. Ajuste preços e conteúdos reais.
4. **Pagamentos PIX**: coloque o `MP_ACCESS_TOKEN` no `.env` (seção 8).
5. **Bot de ofertas** *(opcional)*: App ID + Secret Key do Mercado Livre e o canal (seção 5).
6. **WhatsApp** *(opcional)*: conectar o número e escolher os grupos (seção 6).

## 4. O painel, tela por tela

**Visão geral**
- **Dashboard**: status dos serviços (bolinha verde = funcionando), checklist e números de vendas e ofertas.

**Vendas**
- **Pedidos**: cada PIX gerado no bot e se foi pago e entregue.
- **Transações**: os pagamentos recebidos do Mercado Pago.
- **Assinaturas**: quem tem acesso ativo, vencido ou encerrado. Dá para **encerrar** um acesso (o bot remove a pessoa do grupo).
- **Usuários**: todos que deram `/start`. Dá para **bloquear** alguém.

**Catálogo**
- **Planos**: o que aparece para compra no bot (preço, pack ou assinatura, duração, o que libera). Planos não são apagados: use **Ocultar**.
- **Conteúdos**: o que é entregue após o pagamento: grupo/canal do Telegram, link, arquivo, foto, vídeo ou texto.
- **Categorias**: organizam os conteúdos na lista "Ver conteúdo disponível" do bot.

**Promoções**
- **Bot de ofertas**: credenciais do Mercado Livre, canal, filtros e categorias.
- **Ofertas**: tudo o que foi encontrado. Dá para colar o seu link de afiliado, publicar, mandar no WhatsApp ou descartar.
- **WhatsApp**: passo a passo para conectar o número, escolher grupos e ativar.

**Sistema**
- **Histórico**: registro de tudo (liberações, logins, alterações). Útil para saber "quem mudou o quê".
- **Webhooks**: avisos de pagamento recebidos do Mercado Pago.
- **Administradores**: criar outros acessos. *Operador* só consulta e bloqueia; *Superusuário* altera tudo.

## 5. Bot de ofertas: Mercado Livre → Telegram

**Configurar (uma vez):**
1. Em https://developers.mercadolivre.com.br/devcenter, clique em **Criar aplicação**. Para a URL de redirect, qualquer endereço https serve (ex.: `https://seudominio.com.br`).
2. Copie o **App ID** e a **Secret Key**.
3. Crie um **canal** no Telegram e adicione o **@lgr_ebac_bot como administrador** com permissão de **Publicar mensagens**.
4. No painel → **Bot de ofertas**:
   - **Passo 1:** App ID e Secret Key.
   - **Passo 2:** o canal (`@nomedocanal`). Se o canal for privado, encaminhe um post dele para o bot e ele responde com o ID.
   - **Passos 3 e 4:** desconto mínimo, faixa de preço, categorias, palavras-chave e horário.
5. Clique em **Testar conexão**. Todas as etapas devem ficar ✅.
6. Clique em **Buscar ofertas agora** e confira em **Ofertas**.
7. Clique em **Ligar bot**.

**Como funciona:** a cada X minutos (padrão 45), dentro do horário ativo, o bot pega os mais vendidos das categorias escolhidas, calcula o desconto, aplica os filtros e publica as melhores. Ele não repete o mesmo produto por 7 dias.

- **Modo automático:** publica sozinho.
- **Modo manual:** as ofertas ficam em **Ofertas → Aguardando** para você aprovar.

**Link de afiliado:** o Mercado Livre **não tem API** para gerar link de afiliado. Para ganhar comissão:
- gere o link no **Linkbuilder** do Programa de Afiliados e cole no campo da oferta, **ou**
- use o **modo conversor** (seção 7), que é o jeito mais rápido.

## 6. Ofertas no WhatsApp

Painel → **WhatsApp**, em 3 passos:
1. **Conectar o número**: clique em *Gerar QR Code*. No celular do **chip dedicado**: WhatsApp → ⋮ → *Aparelhos conectados* → *Conectar um aparelho*.
2. **Escolher os grupos** que recebem as ofertas.
3. **Ativar envios automáticos**. Antes, mande uma *mensagem de teste*.

**Ritmo de envio (anti-bloqueio):**
- **Primeiros 7 dias (aquecimento):** até **5 ofertas por dia**, espalhadas.
- **Depois:** **5 ofertas a cada 4 horas**, no máximo 20 por dia.
- **Horário:** só dentro do horário ativo (padrão 08:00–23:00).
- **Prioridade:** as ofertas com maior desconto saem primeiro.

Dá para mudar em *Ajustar ritmo (avançado)*. Trocou de chip? Use *Recomeçar aquecimento*.

> ⚠️ A conexão funciona como o WhatsApp Web (Evolution API), não é a API oficial da Meta. Use **um chip só para isso** e envie só para **grupos seus**. O risco de bloqueio é baixo com esse ritmo, mas não é zero.

## 7. Postar uma oferta pelo celular (modo conversor)

Depois de vincular o seu Telegram (Minha conta):
1. No app do Mercado Livre, gere o seu link de afiliado (`meli.la/...`) ou copie o link do produto.
2. **Cole o link na conversa com o @lgr_ebac_bot.**
3. O bot responde com a **prévia do post** (foto, preço de/por, desconto) e três botões:
   - **Publicar (Telegram + WhatsApp)**
   - **Só no Telegram**
   - **Descartar**

O post sai com o **seu link de afiliado** no botão "Ver oferta". No WhatsApp, ele segue o ritmo de envio.

## 8. Vendas pelo bot (funil + PIX)

**Configurar o PIX:**
1. Em https://www.mercadopago.com.br/developers/panel/app, crie uma aplicação e copie o **Access Token**.
2. Coloque no `.env`: `MP_ACCESS_TOKEN=...`.
3. Em **Webhooks** da aplicação, cadastre `https://seudominio.com.br/webhooks/mercadopago` (evento *Pagamentos*) e copie a **assinatura secreta** para `MP_WEBHOOK_SECRET=...`. Precisa de domínio com HTTPS.
4. Reinicie: `docker compose up -d`.

**O que acontece numa venda:**
1. A pessoa dá `/start`, escolhe o plano e recebe o QR Code PIX e o "copia e cola".
2. Pagou: o Mercado Pago avisa o sistema, que **confere o valor e o pedido direto na API** e libera o acesso.
3. O bot envia o conteúdo. Para grupos, manda um convite individual que vale uma vez.
4. Quando a assinatura vence, o bot avisa 3 dias antes e, no vencimento, remove a pessoa do grupo.

O acesso **nunca** é liberado só porque a pessoa clicou em "Já paguei". O sistema sempre consulta o pagamento real.

## 9. Alertas e resumo diário

Com o Telegram vinculado, o bot te avisa quando:
- o WhatsApp desconectar;
- a busca de ofertas falhar (ex.: credencial errada);
- um pedido pago ficar sem entrega por mais de 10 minutos;
- webhooks de pagamento falharem.

O mesmo alerta não se repete por 6 horas. Todo dia (padrão: 21h) chega o **resumo**: novos usuários, vendas e ofertas publicadas. Para configurar: **Minha conta → Alertas e resumo diário**.

## 10. Comandos do bot

**Para clientes:**

| Comando | O que faz |
|---|---|
| `/start` | Menu principal |
| `/planos` | Lista de planos |
| `/meusacessos` | Acessos ativos e reenvio do conteúdo |
| `/suporte` | Contato do suporte |

**Para você (Telegram vinculado):**

| Comando / ação | O que faz |
|---|---|
| `/status` | Saúde do sistema |
| `/resumo` | Números de hoje |
| colar link do Mercado Livre | Monta o post da oferta |
| encaminhar post de um canal | Mostra o ID do canal |
| enviar arquivo, foto ou vídeo | Mostra o `file_id` para cadastrar como conteúdo |
| `/chatid` (dentro de um grupo) | Mostra o ID do grupo |
| `/id` | Mostra o seu ID do Telegram |

## 11. Backup

- **Automático:** todo dia às 03h, o serviço `backup` salva o banco em `backups/auto_AAAAMMDD_HHMMSS.dump` e mantém os últimos 14 dias.
- **Manual:** `./scripts/backup.sh`
- **Restaurar:** `./scripts/restore.sh backups/arquivo.dump`. Ele pede confirmação e faz um backup do estado atual antes.

> Copie os backups para **fora do servidor** de vez em quando (Google Drive, outro computador). Se o disco do servidor falhar, os backups locais vão junto.

## 12. Problemas comuns

| Problema | O que fazer |
|---|---|
| Painel não abre | `docker compose ps` (todos devem estar "Up"); `docker compose up -d` |
| Bot não responde | Dashboard: "Bot do Telegram" deve estar verde. Ver logs: `docker compose logs --tail 50 telegram_bot` |
| "Pagamentos indisponíveis" no bot | Falta `MP_ACCESS_TOKEN` no `.env` |
| Bot de ofertas não posta | *Testar conexão* no painel; confira se o bot é admin do canal e se está dentro do horário ativo |
| Ofertas sem comissão | Cole o link `meli.la` na oferta ou use o modo conversor |
| WhatsApp desconectou | Painel → WhatsApp → *Gerar QR Code* e ler de novo |
| Esqueci a senha do painel | No servidor: `docker compose exec backend python manage.py set-password --username admin` |
| Conta do painel bloqueada | Espere 15 minutos ou redefina a senha (comando acima) |

## 13. Para quem vai cuidar do servidor

Resumo técnico (detalhes no [README.md](README.md)):

```bash
# subir / atualizar tudo (migrations rodam sozinhas)
docker compose up -d --build

# status e logs
docker compose ps
docker compose logs -f telegram_bot      # ou backend, whatsapp, backup, postgres

# reiniciar / parar (dados preservados)
docker compose restart backend telegram_bot
docker compose down                      # NUNCA use "down -v": apaga o banco

# criar admin / trocar senha / verificar configuração
docker compose exec backend python manage.py create-admin --username NOME --superuser
docker compose exec backend python manage.py set-password --username admin
docker compose exec backend python manage.py check

# testes automatizados
docker compose run --rm -T -v "$PWD:/app" migrate sh -c \
  "pip install -q --target /tmp/pt pytest && PYTHONPATH=/tmp/pt python -m pytest -q -p no:cacheprovider"
```

**Para colocar na internet:**
1. Aponte o domínio para o servidor e preencha `DOMAIN`, `PUBLIC_BASE_URL=https://...` e `APP_ENV=production` no `.env`.
2. Suba com HTTPS automático: `docker compose --profile proxy up -d --build`.
3. Libere só as portas 80 e 443 no firewall. Deixe `BACKEND_BIND_ADDRESS=127.0.0.1`: o proxy é que fica exposto.
4. Para migrar de servidor, faça um backup, copie o projeto, o `.env` e o `.dump`, suba o novo servidor e use `./scripts/restore.sh`. O WhatsApp precisa ler o QR Code de novo.
