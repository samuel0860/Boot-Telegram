# Guia do sistema — Telegram Vendas + Bot de Ofertas

Manual de uso do dia a dia, em linguagem simples. A parte técnica (instalação, Docker, banco de dados) está no [README.md](README.md).

> 🔑 **Senhas e chaves** estão no arquivo **`ACESSOS.md`**, que fica **só no servidor** (não vai para o GitHub).

---

## Sumário
1. [O que o sistema faz](#1-o-que-o-sistema-faz)
2. [Como acessar](#2-como-acessar)
3. [Primeiros passos (checklist)](#3-primeiros-passos-checklist)
4. [O painel, tela por tela](#4-o-painel-tela-por-tela)
5. [Bots de ofertas (Mercado Livre, Shopee e Amazon → Telegram)](#5-bots-de-ofertas-mercado-livre-shopee-e-amazon--telegram)
6. [Ofertas no WhatsApp (um número por bot)](#6-ofertas-no-whatsapp-um-número-por-bot)
7. [Postar uma oferta pelo celular (modo conversor)](#7-postar-uma-oferta-pelo-celular-modo-conversor)
8. [Vendas pelo bot próprio (funil + PIX)](#8-vendas-pelo-bot-próprio-funil--pix)
9. [Alertas e resumo diário](#9-alertas-e-resumo-diário)
10. [Comandos](#10-comandos)
11. [Backup](#11-backup)
12. [Problemas comuns](#12-problemas-comuns)
13. [Para quem vai cuidar do servidor](#13-para-quem-vai-cuidar-do-servidor)

---

## 1. O que o sistema faz

O sistema tem **dois módulos separados**, cada um com seus bots e suas configurações:

| Módulo | O que faz |
|---|---|
| **Vendas** (bot próprio, @lgr_ebac_bot) | Vende os **seus** produtos/serviços: mostra os planos, gera o PIX, libera o conteúdo depois do pagamento e controla a validade das assinaturas. O dinheiro cai direto na **sua conta** do gateway (hoje Mercado Pago). |
| **Ofertas** (afiliados) | **Vários bots de ofertas**, cada um com o **próprio bot do Telegram** e o **próprio número de WhatsApp**. Cada um busca promoções no **Mercado Livre**, na **Shopee** e/ou na **Amazon** com os seus filtros e publica nos **seus** canais e grupos. |

Também fazem parte: a **página inicial** (link da bio do TikTok, que leva ao bot de vendas) e o **painel**, onde você configura e acompanha os dois módulos.

Tudo roda em containers Docker: `postgres` (banco), `backend` (painel e página), `telegram_bot` (vendas), `offers_bot` (ofertas), `whatsapp` e `backup`.

## 2. Como acessar

| O quê | Endereço |
|---|---|
| Painel administrativo | `http://<servidor>:8765/admin/` (hoje: http://10.10.0.111:8765/admin/) |
| Página inicial (TikTok) | `http://<servidor>:8765/` |
| Bot | https://t.me/lgr_ebac_bot |

Usuário e senha do painel: veja o `ACESSOS.md`. Para trocar a senha: **Minha conta → Trocar senha**.

**Recomendado:** ative a **verificação em duas etapas** em *Minha conta*. Instale o Google Authenticator (ou Microsoft Authenticator/Authy) no celular, escaneie o QR Code e digite o código. Depois disso o login pede senha **e** o código do app. Perdeu o celular? No servidor: `make reset-2fa u=admin`.

> Com o servidor definitivo e um domínio, os endereços mudam para `https://seudominio.com.br/admin/`.

### Instalar o painel como app (celular e computador)

O painel pode ser instalado como um aplicativo: ícone na tela inicial, abre em tela cheia, sem a barra do navegador.

- **Android (Chrome):** abra o painel e toque em **Instalar app** no topo (ou no menu ⋮ → **Instalar app** / **Adicionar à tela inicial**).
- **iPhone (Safari):** abra o painel, toque em **Compartilhar** (o quadrado com a seta) → **Adicionar à Tela de Início**. O painel mostra essa dica uma vez.
- **Computador (Chrome/Edge):** clique em **Instalar app** no topo, ou no ícone de instalar na barra de endereço.

Segurando o ícone do app no Android, aparecem atalhos para **Central dos bots**, **Ofertas encontradas** e **Pedidos**. O app usa o mesmo login do painel e se atualiza sozinho quando o sistema é atualizado.

> ⚠️ O navegador só instala como app quando o painel é aberto por **https** (com domínio). Pelo endereço `http://IP:8765` o Android só cria um atalho comum, e o iPhone abre como site. Configure o domínio no `.env` (`DOMAIN=`) e o Caddy cuida do certificado.

## 3. Primeiros passos (checklist)

O **Dashboard** mostra o checklist "Primeiros passos" com o que ainda falta (cada item diz se é de vendas ou de ofertas):

1. **Vincular o seu Telegram**: Minha conta → *Gerar código de vínculo* → mande `/vincular CÓDIGO` ao bot. Libera alertas e resumo diário.
2. **Vendas — bot no ar** e **planos e conteúdos**: já existem 3 planos de exemplo. Ajuste preços e conteúdos reais.
3. **Vendas — pagamentos**: coloque as credenciais do gateway no `.env` (seção 8).
4. **Ofertas — conta do Mercado Livre** *(opcional)*: App ID + Secret Key (seção 5).
5. **Ofertas — primeiro bot** *(opcional)*: crie o bot, cadastre o token e/ou o WhatsApp e os destinos (seção 5).

## 4. O painel, tela por tela

**Visão geral**
- **Dashboard**: status dos serviços (bolinha verde = funcionando), checklist e números de vendas e ofertas.

**Vendas · bot próprio**
- **Pedidos**: cada PIX gerado no bot e se foi pago e entregue.
- **Transações**: os pagamentos recebidos pelo gateway (hoje Mercado Pago).
- **Assinaturas**: quem tem acesso ativo, vencido ou encerrado. Dá para **encerrar** um acesso (o bot remove a pessoa do grupo).
- **Usuários**: todos que deram `/start`. Dá para **bloquear** alguém e, se o cliente pedir exclusão dos dados (LGPD), **anonimizar**.
- **Funil por origem**: de onde vem quem abre o bot (TikTok, Instagram...), quantos geram PIX e quantos compram.
- **Cupons**: crie códigos de desconto para os planos do bot (ex.: `TIKTOK10` = 10% off).

- **Planos**: o que aparece para compra no bot (preço, pack ou assinatura, duração, o que libera). Planos não são apagados: use **Ocultar**.
- **Conteúdos**: o que é entregue após o pagamento: grupo/canal do Telegram, link, arquivo, foto, vídeo ou texto.
- **Categorias**: organizam os conteúdos na lista "Ver conteúdo disponível" do bot.

**Grupos de promoção · afiliados**
- **Central dos bots**: a situação de todos os bots numa tela só. Cada bot aparece num cartão com uma etiqueta colorida:
  - 🟢 **Funcionando** · 🟡 **Precisa de atenção** (os avisos aparecem no cartão) · 🔵 **Configuração incompleta** (mostra o que falta, com link direto para resolver) · ⚪ **Desligado**.
  - Em cada cartão: posts nas últimas 24h, fila, quando foi o último post e quando é a próxima busca, e os botões **Ofertas**, **Configurar**, **Buscar agora** e **Ligar/Desligar**.
  - **Pausar todos** desliga todos os bots de uma vez (manutenção, feriado). **Retomar os que eu pausei** religa só os que estavam ligados antes.
- **Bots de promoção**: cada bot tem as abas *Geral*, *Telegram* (token e canais), *WhatsApp* (número e grupos), *Busca e filtros* e *Modelo do post*. Na aba *Geral* dá para **copiar a configuração de outro bot** (busca e filtros, modelo do post e/ou ritmo do WhatsApp); token, canais e número não são copiados.
- **Ofertas encontradas**: o que cada bot encontrou (escolha o bot no topo). Em cada oferta:
  - **Publicar** (Telegram + WhatsApp, só um deles) ou **Descartar**;
  - **Chamada, cupom e link** abre os campos para editar antes de publicar;
  - **Ver como fica no grupo** mostra o post com foto e formatação, como o grupo vai ver;
  - o quadro **Postar produto pelo link**, no topo, publica um produto que você escolheu (seção 7).
- **Estatísticas**: posts por dia (por loja), por bot, os **tipos de produto que mais saem** e os maiores descontos publicados, nos últimos 7, 30 ou 90 dias. “Ver em tabela” mostra os números do gráfico.
- **Lojas e APIs**: as contas do Mercado Livre, da Shopee e da Amazon, usadas por todos os bots.

Nos campos mais difíceis há um **?**: passe o mouse (ou toque) para ver a explicação.

**Sistema**
- **Histórico**: registro de tudo (liberações, logins, alterações). Útil para saber "quem mudou o quê".
- **Webhooks**: avisos de pagamento recebidos do Mercado Pago.
- **Administradores**: criar outros acessos. *Operador* só consulta e bloqueia; *Superusuário* altera tudo.

## 5. Bots de ofertas (Mercado Livre, Shopee e Amazon → Telegram)

Você pode ter **quantos bots de ofertas quiser**. Cada um tem o próprio bot do Telegram, o próprio número de WhatsApp, os próprios canais/grupos e as próprias configurações. Exemplo: *Ofertas Tech* nos grupos de tecnologia e *Ofertas Casa* nos de casa.

**Uma vez só — contas das lojas** (Painel → **Lojas e APIs**):
- **Mercado Livre:** em https://developers.mercadolivre.com.br/devcenter, clique em **Criar aplicação** (para a URL de redirect, qualquer endereço https serve). Cole o **App ID** e a **Secret Key** → **Salvar** → **Testar**.
- **Shopee:** em https://affiliate.shopee.com.br → **Open API**, copie o **AppID** e a **Senha** → cole no painel → **Salvar** → **Testar**. Na Shopee o **link de afiliado vem pronto** da API, já com a sua comissão.
- **Amazon:** em https://associados.amazon.com.br → **Ferramentas → Creators API → Criar credencial**, copie o **Credential ID**, o **Credential Secret** e a **versão**; o **Partner Tag** é o seu ID de associado (termina em `-20`) → **Salvar** → **Testar**. O link de afiliado também vem pronto. A Amazon só libera a API para associados com vendas qualificadas recentes.

**Para cada bot de ofertas:**
1. Painel → **Central dos bots** → digite o nome em **Novo bot de promoção** → **Criar bot**. (Dica: na aba *Geral* do bot novo, use **Copiar configuração** de um bot que já funciona.)
2. Aba **Telegram**:
   - No Telegram, abra o **@BotFather**, mande `/newbot` e crie um bot **só para ofertas** (não use o @lgr_ebac_bot, que é o de vendas).
   - Cole o token no painel → **Salvar token**.
   - Adicione esse bot como **administrador** dos seus canais/grupos de ofertas (com permissão de publicar). Eles aparecem em **Encontrados pelo bot** → **Adicionar**. Também dá para digitar `@nomedocanal`.
3. Aba **Busca e filtros**: marque as **lojas** (Mercado Livre, Shopee e/ou Amazon), desconto mínimo, faixa de preço, palavras-chave, horário e modo (automático ou manual).
4. Aba **Modelo do post**: o texto das publicações (veja abaixo).
5. Na lateral: **Testar lojas** → **Buscar ofertas agora** → confira em **Ofertas** → **Ligar bot**.
6. Abra o bot no Telegram e mande `/start` para receber os alertas dele.

**Como funciona:** a cada X minutos (padrão 45), dentro do horário do bot, ele pega os mais vendidos das categorias escolhidas, calcula o desconto, aplica os filtros e publica as melhores em **todos os canais/grupos** daquele bot. Não repete o mesmo produto por 7 dias.

- **Modo automático:** publica sozinho.
- **Modo manual:** as ofertas ficam em **Ofertas → Aguardando** para você aprovar.

**Detector de desconto falso** (aba *Busca e filtros*, ligado por padrão): o sistema guarda o preço de cada produto todo dia. Se a loja anuncia “50% OFF”, mas o preço “com desconto” é o mesmo que ela já cobrava nos últimos dias (o “De” foi inflado), a oferta ganha o aviso **Desconto suspeito** e o bot **não a publica sozinho**. Ela continua na lista e você pode publicar manualmente se quiser. O detector precisa de alguns dias de histórico de cada produto para começar a julgar.

**Modelo do post:** cada bot tem o seu (aba *Modelo do post*), usado no Telegram e no WhatsApp. O padrão é:

```
🆘 *PRECIN NA 43" DA PHILCO*

Smart TV Philco 43" Roku Tv Hdr10

Por R$1.095 pix 👑
⚠️ cupom: *DIADATECH*

Link: https://meli.la/33hYTSb
```

Dá para mudar o texto usando os marcadores (`{chamada}`, `{titulo}`, `{preco}`, `{de}`, `{desconto}`, `{cupom}`, `{link}`, `{frete}`, `{rodape}`) e `*negrito*`. A prévia aparece ao lado. Sem cupom, a linha do cupom some sozinha.

**Link de afiliado:** na **Shopee** e na **Amazon** é automático. O **Mercado Livre** não tem API para gerar link de afiliado; para ganhar comissão:
- gere o link no **Linkbuilder** do Programa de Afiliados e cole no campo da oferta, **ou**
- use o **modo conversor** (seção 7), que é o jeito mais rápido.

## 6. Ofertas no WhatsApp (um número por bot)

Na página do bot de ofertas → aba **WhatsApp**, em 3 passos:
1. **Conectar o número**: *Gerar QR Code*. No celular do **chip deste bot**: WhatsApp → ⋮ → *Aparelhos conectados* → *Conectar um aparelho*.
2. **Escolher os grupos** que recebem as ofertas **deste bot**.
3. **Ativar envios**. Antes, mande uma *mensagem de teste*.

Cada bot usa **o próprio chip**: se um número for bloqueado, os outros bots continuam funcionando.

**Ritmo de envio (anti-bloqueio), por número:**
- **Primeiros 7 dias (aquecimento):** até **5 ofertas por dia**, espalhadas.
- **Depois:** **5 ofertas a cada 4 horas**, no máximo 20 por dia.
- **Horário:** só dentro do horário do bot (padrão 08:00–23:00).
- **Prioridade:** as ofertas com maior desconto saem primeiro.

Dá para mudar em *Ajustar ritmo (avançado)*. Trocou de chip? Use *Recomeçar aquecimento*.

> ⚠️ A conexão funciona como o WhatsApp Web (Evolution API), não é a API oficial da Meta. Use **um chip só para cada bot** e envie só para **grupos seus**. O risco de bloqueio é baixo com esse ritmo, mas não é zero.

## 7. Postar uma oferta pelo celular (modo conversor)

Depois de vincular o seu Telegram (Minha conta):
1. No app do Mercado Livre, gere o seu link de afiliado (`meli.la/...`), na Shopee copie o link do produto ou o seu link curto (`s.shopee.com.br/...`), ou na Amazon o link do produto ou o seu `amzn.to`.
2. **Cole o link na conversa com o bot de ofertas** que deve publicar (ex.: @OfertasTechBot). Se quiser, na mesma mensagem, escreva a chamada e o cupom em linhas separadas:
   ```
   https://meli.la/33hYTSb
   chamada: PRECIN NA 43" DA PHILCO
   cupom: DIADATECH
   ```
3. O bot responde com a **prévia do post** e os botões:
   - **Publicar (Telegram + WhatsApp)** · **Só no Telegram** · **Só no WhatsApp**
   - **Descartar**

O post sai nos canais/grupos **daquele bot**, com o **seu link de afiliado** no texto e no botão "Ver oferta". No WhatsApp, ele segue o ritmo de envio do número do bot.

### Pelo painel (computador)

Em **Ofertas encontradas**, escolha o bot e use o quadro **Postar produto pelo link** no topo:
1. Cole o link (o seu link curto de afiliado da Shopee `s.shopee.com.br/...`, `meli.la/...` ou `amzn.to/...`, ou o link da página do produto).
2. Chamada e cupom são opcionais (chamada vazia = automática).
3. Clique em **Publicar** (Telegram + WhatsApp, só Telegram ou só WhatsApp) ou em **Só importar** para conferir a prévia antes.

Foto, título e preço vêm da API da loja. O post manual **passa na frente** das ofertas automáticas que estão na fila do Telegram (respeitando o intervalo entre posts).

## 8. Vendas pelo bot próprio (funil + PIX)

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

**Recursos que ajudam a vender mais:**
- **Cupons:** crie em *Cupons*. O cliente usa mandando `/cupom CODIGO`, digitando o código, ou pelo link `t.me/SeuBot?start=tiktok__CODIGO`.
- **PIX expirou?** 10 minutos depois o bot manda uma mensagem com o botão **Gerar novo PIX** (uma vez só).
- **Renovação em 1 toque:** o aviso de vencimento já tem o botão **Renovar agora**.
- **Oferta após a compra:** depois de entregar, o bot oferece uma vez o próximo plano (ex.: quem comprou um pack recebe a oferta da assinatura).
- **Saber o que vende:** use um link por vídeo/canal (`t.me/SeuBot?start=tiktok-v12`) e veja os números em *Funil por origem*.

## 9. Alertas e resumo diário

Com o Telegram vinculado, cada módulo te avisa **pelo próprio bot**:

- **Bot de vendas:** pedido pago sem entrega por mais de 10 minutos, webhooks de pagamento falhando, e o **resumo diário das vendas**.
- **Cada bot de ofertas** (mande `/start` para ele uma vez): WhatsApp desconectado, falha na busca de ofertas, canais sem token, e o **resumo diário daquele bot**.
- Qualquer **erro** no sistema (o aviso diz onde foi; o mesmo erro não se repete por 30 minutos).

O mesmo alerta não se repete por 6 horas. O resumo chega no horário configurado (padrão: 21h) em **Minha conta → Alertas e resumo diário**.

## 10. Comandos

**Bot de vendas — para clientes:**

| Comando | O que faz |
|---|---|
| `/start` | Menu principal |
| `/planos` | Lista de planos |
| `/cupom CODIGO` | Aplica um cupom de desconto |
| `/meusacessos` | Acessos ativos e reenvio do conteúdo |
| `/suporte` | Contato do suporte |

**Bot de vendas — para você:**

| Comando / ação | O que faz |
|---|---|
| `/status` | Situação das vendas |
| `/resumo` | Vendas de hoje |
| enviar arquivo, foto ou vídeo | Mostra o `file_id` para cadastrar como conteúdo |
| `/chatid` (dentro de um grupo) | Mostra o ID do grupo |
| `/id` | Mostra o seu ID do Telegram |

**Bots de ofertas — para você:**

| Comando / ação | O que faz |
|---|---|
| `/status` | Situação daquele bot (destinos, fila, última busca) |
| colar link do Mercado Livre, da Shopee ou da Amazon | Monta o post da oferta (aceita `chamada:` e `cupom:` em linhas abaixo do link) |
| encaminhar post de um canal | Mostra o ID do canal |
| `/vincular CÓDIGO` | Vincula o seu Telegram ao painel (funciona em qualquer bot) |

## 11. Backup

- **Automático:** todo dia às 03h, o serviço `backup` salva o banco em `backups/auto_AAAAMMDD_HHMMSS.dump` e mantém os últimos 14 dias.
- **Manual:** `./scripts/backup.sh`
- **Restaurar:** `./scripts/restore.sh backups/arquivo.dump`. Ele pede confirmação e faz um backup do estado atual antes.

> Copie os backups para **fora do servidor** de vez em quando (Google Drive, outro computador). Se o disco do servidor falhar, os backups locais vão junto.

## 12. Problemas comuns

| Problema | O que fazer |
|---|---|
| Painel não abre | `docker compose ps` (todos devem estar "Up"); `docker compose up -d` |
| Bot de vendas não responde | Dashboard: "Bot de vendas" deve estar verde. Logs: `docker compose logs --tail 50 telegram_bot` |
| "Pagamentos indisponíveis" no bot de vendas | Falta a credencial do gateway no `.env` (hoje `MP_ACCESS_TOKEN`) |
| Bot de ofertas não posta | Dashboard: "Módulo de ofertas" verde? Na página do bot: está *ligado*, tem *token* e *destinos*, *Testar Mercado Livre* ok, dentro do horário? O bot é admin do canal? Logs: `make logs-offers` |
| Ofertas sem comissão | Cole o link `meli.la` na oferta ou use o modo conversor |
| WhatsApp desconectou | Painel → Bots de ofertas → o bot → aba *WhatsApp* → *Gerar QR Code* e ler de novo |
| Esqueci a senha do painel | No servidor: `docker compose exec backend python manage.py set-password --username admin` |
| Conta do painel bloqueada | Espere 15 minutos ou redefina a senha (comando acima) |

## 13. Para quem vai cuidar do servidor

Resumo técnico (detalhes no [README.md](README.md)):

```bash
# subir / atualizar tudo (migrations rodam sozinhas)
docker compose up -d --build

# status e logs
docker compose ps
docker compose logs -f telegram_bot      # vendas (ou: offers_bot, backend, whatsapp, backup, postgres)

# reiniciar / parar (dados preservados)
docker compose restart backend telegram_bot offers_bot
docker compose down                      # NUNCA use "down -v": apaga o banco

# criar admin / trocar senha / verificar configuração
docker compose exec backend python manage.py create-admin --username NOME --superuser
docker compose exec backend python manage.py set-password --username admin
docker compose exec backend python manage.py check

# testes automatizados (usam um banco separado de teste)
make test-docker
```

**Para colocar na internet:**
1. Aponte o domínio para o servidor e preencha `DOMAIN`, `PUBLIC_BASE_URL=https://...` e `APP_ENV=production` no `.env`.
2. Suba com HTTPS automático: `docker compose --profile proxy up -d --build`.
3. Libere só as portas 80 e 443 no firewall. Deixe `BACKEND_BIND_ADDRESS=127.0.0.1`: o proxy é que fica exposto.
4. Para migrar de servidor, faça um backup, copie o projeto, o `.env` e o `.dump`, suba o novo servidor e use `./scripts/restore.sh`. O WhatsApp de cada bot de ofertas precisa ler o QR Code de novo.
