"""Modelo (template) dos posts de ofertas — o mesmo texto vai para o Telegram e o WhatsApp.

O modelo é editado no painel (Ofertas → Bots de ofertas → bot → Modelo do post) e usa:

  Marcadores      {chamada} {titulo} {preco} {de} {desconto} {cupom} {link} {frete} {rodape} {loja}
  Formatação      *negrito*   _itálico_   ~riscado~   (padrão do WhatsApp)

Regras:
- Uma linha que usa um marcador VAZIO é removida (ex.: sem cupom, some a linha do cupom).
- No WhatsApp a formatação é mantida como está; no Telegram vira HTML (<b>, <i>, <s>).
- Os valores (título, cupom...) nunca são interpretados como formatação.
"""
from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, replace
from decimal import Decimal
from html import escape

DEFAULT_TEMPLATE = (
    "🆘 *{chamada}*\n"
    "\n"
    "{titulo}\n"
    "\n"
    "Por {preco} pix 👑\n"
    "⚠️ cupom: *{cupom}*\n"
    "\n"
    "Link: {link}"
)
# Vazio = chamada AUTOMÁTICA (montada com o nome do produto, o desconto e a loja)
DEFAULT_HEADLINE = ""

PLACEHOLDERS: dict[str, str] = {
    "chamada": "Frase de impacto: automática (produto + desconto), a fixa do bot ou a definida na oferta",
    "titulo": "Nome do produto",
    "preco": "Preço atual (ex.: R$1.095)",
    "de": "Preço antigo, se houver (ex.: R$1.499)",
    "desconto": "Percentual de desconto, se houver (ex.: 27)",
    "cupom": "Cupom da oferta, se houver",
    "link": "Link de afiliado (ou do produto)",
    "frete": "“Frete grátis” quando houver",
    "rodape": "Rodapé configurado no painel",
    "loja": "Loja da oferta (Mercado Livre, Shopee ou Amazon)",
}

_TOKEN = re.compile(r"\{(\w+)\}")
_WA_MARKUP = re.compile(r"[*_~`]")
# *negrito*, _itálico_, ~riscado~ — o conteúdo não pode começar/terminar com espaço
_TG_RULES = [
    (re.compile(r"\*(?=\S)([^*\n]*?\S)\*|\*(\S)\*"), "b"),
    (re.compile(r"(?<![\w/])_(?=\S)([^_\n]*?\S)_(?!\w)|(?<![\w/])_(\S)_(?!\w)"), "i"),
    (re.compile(r"~(?=\S)([^~\n]*?\S)~|~(\S)~"), "s"),
]


def short_brl(value: Decimal | None) -> str:
    """R$1.095 (sem centavos quando inteiro) ou R$1.095,90."""
    if value is None:
        return ""
    value = Decimal(value)
    if value == value.to_integral_value():
        number = f"{int(value):,}".replace(",", ".")
    else:
        number = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R${number}"


@dataclass(frozen=True)
class PostData:
    title: str
    price: Decimal
    link: str
    original_price: Decimal | None = None
    discount_pct: int | None = None
    free_shipping: bool = False
    headline: str | None = None
    coupon: str | None = None
    store: str = ""


# --------------------------------------------------------------------------- #
# Chamada automática
# --------------------------------------------------------------------------- #
STORE_PREP = {"Mercado Livre": "NO MERCADO LIVRE", "Shopee": "NA SHOPEE", "Amazon": "NA AMAZON"}
# Formatos com desconto ({n} = nome curto do produto, {d} = desconto). Sem artigo antes do
# produto (no/na), que dependeria do gênero da palavra.
WITH_DISCOUNT = (
    "{n} COM {d}% OFF",
    "{d}% OFF EM {n}",
    "BAIXOU {d}%: {n}",
    "PRECIN: {n} COM {d}% OFF",
    "DESPENCOU: {n} {d}% OFF",
)
WITHOUT_DISCOUNT = ("PRECIN {loja}", "OFERTA {loja}: {n}", "ACHADO {loja}")
MIN_DISCOUNT_IN_HEADLINE = 5
NAME_MAX_CHARS = 28
_CUT = re.compile(r"\s[|\-–—/]\s|[,(\[;:|]|\s(?:com|para|c/|p/|ideal|kit de)\s", re.I)
_TRAILING = {"de", "da", "do", "das", "dos", "e", "com", "para", "em", "a", "o", "-", "+"}


def short_name(title: str, max_chars: int = NAME_MAX_CHARS) -> str:
    """Nome curto do produto: o começo do título, até o primeiro separador, em poucas palavras."""
    text = _WA_MARKUP.sub("", " ".join((title or "").split()))
    head = _CUT.split(text, maxsplit=1)[0].strip() or text
    words: list[str] = []
    for word in head.split():
        if len(" ".join([*words, word])) > max_chars or len(words) == 5:
            break
        words.append(word)
    while words and words[-1].lower() in _TRAILING:
        words.pop()
    return " ".join(words or head.split()[:1]).upper()


def auto_headline(data: PostData) -> str:
    """Chamada montada sozinha. O formato varia entre produtos, mas é sempre o mesmo para o mesmo produto."""
    name = short_name(data.title)
    store = STORE_PREP.get(data.store, f"NA {data.store.upper()}" if data.store else "DO DIA")
    pick = zlib.crc32(data.title.encode())
    if data.discount_pct and data.discount_pct >= MIN_DISCOUNT_IN_HEADLINE:
        pattern = WITH_DISCOUNT[pick % len(WITH_DISCOUNT)]
        return pattern.format(n=name, d=data.discount_pct)
    return WITHOUT_DISCOUNT[pick % len(WITHOUT_DISCOUNT)].format(n=name, loja=store)


def _values(data: PostData, default_headline: str, footer: str) -> dict[str, str]:
    old = data.original_price if data.original_price and data.original_price > data.price else None
    return {
        "chamada": (data.headline or default_headline or "").strip() or auto_headline(data),
        "titulo": data.title.strip()[:200],
        "preco": short_brl(data.price),
        "de": short_brl(old) if old else "",
        "desconto": str(data.discount_pct) if data.discount_pct else "",
        "cupom": (data.coupon or "").strip(),
        "link": data.link.strip(),
        "frete": "Frete grátis" if data.free_shipping else "",
        "rodape": (footer or "").strip(),
        "loja": (data.store or "").strip(),
    }


def _drop_empty_lines(template: str, values: dict[str, str]) -> list[str]:
    lines = []
    for line in template.replace("\r\n", "\n").split("\n"):
        names = _TOKEN.findall(line)
        if any(n in values and not values[n] for n in names):
            continue
        lines.append(line)
    # junta linhas em branco repetidas e tira as das pontas
    out: list[str] = []
    for line in lines:
        if not line.strip() and (not out or not out[-1].strip()):
            continue
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return out


def _fill(line: str, values: dict[str, str]) -> str:
    return _TOKEN.sub(lambda m: values.get(m.group(1), m.group(0)), line)


def render_whatsapp(data: PostData, template: str, default_headline: str = "", footer: str = "") -> str:
    values = _values(data, default_headline, footer)
    # valores não podem "abrir" formatação — exceto o link (URLs podem ter _)
    safe = {k: (v if k == "link" else _WA_MARKUP.sub("", v)) for k, v in values.items()}
    return "\n".join(_fill(line, safe) for line in _drop_empty_lines(template or DEFAULT_TEMPLATE, values))


def _tg_markup(line: str) -> str:
    for rx, tag in _TG_RULES:
        line = rx.sub(lambda m, t=tag: f"<{t}>{m.group(1) or m.group(2)}</{t}>", line)
    return line


def render_telegram(data: PostData, template: str, default_headline: str = "", footer: str = "",
                    limit: int = 1024) -> str:
    """HTML do Telegram. limit=1024 é o máximo de uma legenda de foto."""
    values = _values(data, default_headline, footer)
    safe = {k: escape(v) for k, v in values.items()}
    out = []
    for line in _drop_empty_lines(template or DEFAULT_TEMPLATE, values):
        # formatação aplicada só ao texto do modelo; os valores entram depois
        out.append(_fill(_tg_markup(escape(line, quote=False)), safe))
    text = "\n".join(out)
    if len(text) > limit:  # título muito grande: encurta o título e tenta de novo
        short = replace(data, title=data.title[: max(20, len(data.title) - (len(text) - limit) - 3)] + "...")
        if short.title != data.title:
            return render_telegram(short, template, default_headline, footer, limit)
    return text


def validate(template: str) -> list[str]:
    """Problemas no modelo (para mostrar no painel)."""
    errors = []
    unknown = sorted({n for n in _TOKEN.findall(template or "") if n not in PLACEHOLDERS})
    if unknown:
        errors.append("Marcador desconhecido: " + ", ".join("{" + n + "}" for n in unknown))
    if "{link}" not in (template or ""):
        errors.append("O modelo precisa ter o marcador {link}.")
    if len(template or "") > 1500:
        errors.append("Modelo muito longo (máx. 1500 caracteres).")
    return errors
