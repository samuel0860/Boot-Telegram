"""Modelo do post de ofertas (Telegram + WhatsApp)."""
from decimal import Decimal

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
