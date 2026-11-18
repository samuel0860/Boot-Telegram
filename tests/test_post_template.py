"""Modelo do post de ofertas (Telegram + WhatsApp)."""
from decimal import Decimal

import pytest

from offers import converter, post_template
from offers.post_template import DEFAULT_TEMPLATE, PostData, render_telegram, render_whatsapp, short_brl

TV = PostData(title='Smart TV Philco 43" Roku Tv Hdr10', price=Decimal("1095"), link="https://meli.la/33hYTSb",
              original_price=Decimal("1499"), discount_pct=27, headline='PRECIN NA 43" DA PHILCO',
              coupon="DIADATECH")


def test_whatsapp_matches_reference_model():
    assert render_whatsapp(TV, DEFAULT_TEMPLATE) == (
        '🆘 *PRECIN NA 43" DA PHILCO*\n'
        "\n"
        'Smart TV Philco 43" Roku Tv Hdr10\n'
        "\n"
        "Por R$1.095 pix 👑\n"
        "⚠️ cupom: *DIADATECH*\n"
        "\n"
        "Link: https://meli.la/33hYTSb"
    )


def test_telegram_html():
    html = render_telegram(TV, DEFAULT_TEMPLATE)
    assert html.startswith("🆘 <b>PRECIN NA 43&quot; DA PHILCO</b>")
    assert "⚠️ cupom: <b>DIADATECH</b>" in html
    assert "Link: https://meli.la/33hYTSb" in html


def test_empty_placeholder_removes_line_and_default_headline():
    data = PostData(title="Air Fryer", price=Decimal("299.90"), link="https://meli.la/x")
    text = render_whatsapp(data, DEFAULT_TEMPLATE, default_headline="OFERTA DO DIA")
    assert "cupom" not in text
    assert text.startswith("🆘 *OFERTA DO DIA*")
    assert "Por R$299,90 pix" in text
    assert "\n\n\n" not in text


def test_values_are_never_markup_or_html():
    data = PostData(title="<b>x</b> *y* _z_", price=Decimal("10"), link="https://ml.com/a_b_c",
                    headline="A & B", coupon="<i>")
    html = render_telegram(data, "*{chamada}*\n{titulo}\n{cupom}\n{link}")
    assert "<b>A &amp; B</b>" in html
    assert "&lt;b&gt;x&lt;/b&gt; *y* _z_" in html     # título escapado e sem virar negrito
    assert "&lt;i&gt;" in html
    wa = render_whatsapp(data, "{titulo}\n{link}")
    assert "<b>x</b> y z" in wa and "https://ml.com/a_b_c" in wa   # link preservado


def test_custom_template_with_old_price_and_discount():
    tpl = "🔥 {titulo}\n~De {de}~\n*Por {preco}* (-{desconto}%)\n{frete}\n{link}"
    wa = render_whatsapp(TV, tpl)
    assert "~De R$1.499~" in wa and "*Por R$1.095* (-27%)" in wa and "Frete" not in wa
    tg = render_telegram(TV, tpl)
    assert "<s>De R$1.499</s>" in tg and "<b>Por R$1.095</b>" in tg


def test_telegram_caption_limit():
    long = PostData(title="X" * 2000, price=Decimal("1"), link="https://meli.la/y")
    assert len(render_telegram(long, DEFAULT_TEMPLATE)) <= 1024


def test_short_brl():
    assert short_brl(Decimal("1095")) == "R$1.095"
    assert short_brl(Decimal("1095.9")) == "R$1.095,90"
    assert short_brl(Decimal("12345.67")) == "R$12.345,67"


def test_validate():
    assert post_template.validate(DEFAULT_TEMPLATE) == []
    errors = post_template.validate("{titulo} {preçoo}")
    assert any("{link}" in e for e in errors)
    assert post_template.validate("{nada} {link}")[0].startswith("Marcador desconhecido")


def test_converter_reads_headline_and_coupon():
    text = 'https://meli.la/33hYTSb\nchamada: PRECIN NA 43" DA PHILCO\n⚠️ cupom: DIADATECH'
    assert converter.parse_extras(text) == ('PRECIN NA 43" DA PHILCO', "DIADATECH")
    assert converter.parse_extras("https://meli.la/abc") == (None, None)


# --------------------------------------------------------------------------- #
# Chamada automática
# --------------------------------------------------------------------------- #
from offers.post_template import WITH_DISCOUNT, auto_headline, short_name  # noqa: E402


@pytest.mark.parametrize("title,expected", [
    ("Echo Dot 5ª geração | Smart speaker com Alexa", "ECHO DOT 5ª GERAÇÃO"),
    ('Smart TV Philco 43" Roku Tv Hdr10', 'SMART TV PHILCO 43" ROKU'),
    ("Air Fryer Mondial 4L, Preta, 1500W", "AIR FRYER MONDIAL 4L"),
    ("Fone de Ouvido Bluetooth JBL Tune 520BT - Preto", "FONE DE OUVIDO BLUETOOTH JBL"),
    ("Kit 3 Panelas Tramontina com tampa de vidro", "KIT 3 PANELAS TRAMONTINA"),
    ("*Promo* Notebook_Gamer", "PROMO NOTEBOOKGAMER"),
])
def test_short_name(title, expected):
    assert short_name(title) == expected


def test_auto_headline_uses_name_and_discount_and_is_stable():
    data = PostData(title="Echo Dot 5ª geração | Smart speaker", price=Decimal("299"), link="https://x",
                    discount_pct=40, store="Amazon")
    headline = auto_headline(data)
    assert "ECHO DOT 5ª GERAÇÃO" in headline and "40%" in headline
    assert headline == auto_headline(data)                         # mesmo produto, mesma chamada
    titles = [f"Produto número {i}" for i in range(40)]
    used = {auto_headline(PostData(title=t, price=Decimal(1), link="x", discount_pct=30)) .replace(
        t.upper(), "{n}").replace("30", "{d}") for t in titles}
    assert used == set(WITH_DISCOUNT)                              # os formatos variam entre produtos


def test_auto_headline_without_discount_names_the_store():
    data = PostData(title="Capinha", price=Decimal("10"), link="x", discount_pct=None, store="Shopee")
    assert "SHOPEE" in auto_headline(data)
    ml = PostData(title="Capinha", price=Decimal("10"), link="x", discount_pct=2, store="Mercado Livre")
    assert "MERCADO LIVRE" in auto_headline(ml)


def test_rendered_post_uses_auto_headline_unless_fixed_or_per_offer():
    data = PostData(title="Echo Dot 5ª geração", price=Decimal("299"), link="https://x", discount_pct=40,
                    store="Amazon")
    auto = render_whatsapp(data, DEFAULT_TEMPLATE)
    assert auto.startswith(f"🆘 *{auto_headline(data)}*")
    assert render_whatsapp(data, DEFAULT_TEMPLATE, default_headline="OFERTA DO DIA").startswith("🆘 *OFERTA DO DIA*")
    own = PostData(**{**data.__dict__, "headline": "SÓ HOJE"})
    assert render_whatsapp(own, DEFAULT_TEMPLATE, default_headline="OFERTA DO DIA").startswith("🆘 *SÓ HOJE*")
