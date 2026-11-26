"""Frase curta e chamativa para o post, escolhida pelo TIPO do produto (pelo nome dele).

100% automático e sem custo: cada grupo tem palavras-chave e algumas frases. O primeiro
grupo cujas palavras aparecem no título ganha; a frase do grupo é escolhida pelo próprio
título (o mesmo produto sempre recebe a mesma frase, produtos diferentes variam).
Sem grupo, usa uma frase geral.

Para criar um grupo novo, basta acrescentar uma linha em GROUPS.
"""
from __future__ import annotations

import re
import unicodedata
import zlib

# (palavras-chave sem acento, frases). A ORDEM importa: grupos mais específicos primeiro.
GROUPS: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("auxiliar de partida", "partida auxiliar", "jump starter", "partida de bateria", "carregador de bateria automotiva"),
     ("Esse salva a sua vida quando você mais precisa.",
      "Bateria arriou? Com esse você não fica na mão.",
      "Todo porta-malas devia ter um desse.")),
    (("compressor de ar", "calibrador", "enchedor de pneu"),
     ("Pneu murcho nunca mais vai te pegar de surpresa.",
      "Calibra o pneu em qualquer lugar.")),
    (("aspirador automotivo", "aspirador de carro", "cera automotiva", "lava jato", "lavadora de alta pressao",
      "kit lavagem", "shampoo automotivo"),
     ("Carro limpinho sem sair de casa.",
      "Seu carro vai agradecer.")),
    (("dash cam", "camera veicular", "multimidia", "som automotivo", "suporte veicular", "carregador veicular"),
     ("Upgrade no carro por um precinho desses.",
      "Seu carro mais moderno sem gastar muito.")),
    (("air fryer", "airfryer", "fritadeira"),
     ("Fritura crocante sem óleo e sem sujeira.",
      "Depois que tem uma, não larga mais.",
      "A queridinha da cozinha baixou de preço.")),
    (("panela", "frigideira", "cacarola", "jogo de panelas"),
     ("Cozinha nova por um precinho desses.",
      "Nada gruda, tudo fica mais fácil.")),
    (("liquidificador", "batedeira", "mixer", "processador de alimentos", "cafeteira", "sanduicheira",
      "grill", "forno eletrico", "micro-ondas", "microondas", "chaleira"),
     ("Facilita a vida na cozinha todo dia.",
      "Praticidade que você vai usar todo dia.")),
    (("aspirador robo", "robo aspirador", "aspirador de po", "aspirador vertical", "mop", "vassoura eletrica"),
     ("A faxina fica bem mais leve.",
      "Casa limpa com o mínimo de esforço.")),
    (("ventilador", "ar condicionado", "climatizador", "umidificador", "aquecedor"),
     ("Clima perfeito em casa sem pesar no bolso.",
      "Seu conforto agradece.")),
    (("fone", "headphone", "earbuds", "airpods", "headset"),
     ("Som de qualidade por um preço que cabe no bolso.",
      "Pra curtir a música sem ninguém incomodar.")),
    (("caixa de som", "speaker", "soundbar", "echo dot", "alexa"),
     ("Pra deixar qualquer ambiente mais animado.",
      "Som que enche a casa toda.")),
    (("smartwatch", "relogio inteligente", "smartband", "mi band", "apple watch", "galaxy watch"),
     ("Saúde e notificações direto no pulso.",
      "Estilo e tecnologia no seu pulso.")),
    (("smart tv", "televisao", " tv "),
     ("Cinema em casa por um precinho desses.",
      "Maratona de série com outra qualidade.")),
    (("celular", "smartphone", "iphone", "galaxy", "redmi", "xiaomi", "motorola moto"),
     ("Celular novo sem pagar caro.",
      "Hora de trocar de celular pagando pouco.")),
    (("notebook", "laptop", "macbook", "chromebook"),
     ("Pra trabalhar e estudar sem travar.",
      "Desempenho de sobra por esse preço.")),
    (("monitor", "teclado", "mouse", "webcam", "ssd", "memoria ram", "pendrive", "hd externo", "roteador"),
     ("Seu setup agradece esse upgrade.",
      "Upgrade no PC por um precinho bom.")),
    (("playstation", "ps5", "ps4", "xbox", "nintendo", "switch", "controle", "joystick", "gamer"),
     ("A diversão está garantida.",
      "Partida boa começa com o equipamento certo.")),
    (("carregador", "power bank", "powerbank", "cabo usb", "cabo tipo c", "bateria portatil"),
     ("Bateria nunca mais vai te deixar na mão.",
      "Aquele item que salva o dia.")),
    (("furadeira", "parafusadeira", "serra", "esmerilhadeira", "jogo de ferramentas", "kit ferramentas",
      "chave de fenda", "maleta de ferramentas", "trena"),
     ("Faz você mesmo e economiza.",
      "Ferramenta boa resolve tudo em casa.")),
    (("tenis", "sapatenis", "bota", "sandalia", "chinelo", "slide", "rasteirinha"),
     ("Conforto e estilo pra qualquer rolê.",
      "Pra combinar com tudo.",
      "Esse vai virar o seu preferido.")),
    (("camiseta", "camisa", "moletom", "jaqueta", "calca", "bermuda", "vestido", "blusa", "short", "legging"),
     ("Look novo sem gastar muito.",
      "Estilo garantido por um precinho desses.")),
    (("mochila", "bolsa", "mala de viagem", "carteira", "necessaire"),
     ("Leva tudo com estilo.",
      "Pra todo dia e pra viagem.")),
    (("perfume", "colonia", "body splash", "desodorante"),
     ("Pra deixar aquele cheirinho por onde passa.",
      "Perfume bom por um preço desses é raro.")),
    (("secador", "chapinha", "prancha", "modelador", "escova secadora", "babyliss"),
     ("Cabelo de salão sem sair de casa.",
      "Seu cabelo do jeito que você gosta.")),
    (("maquiagem", "batom", "base liquida", "paleta", "rimel", "skincare", "serum", "hidratante",
      "protetor solar", "creme"),
     ("Autocuidado com preço camarada.",
      "Sua pele agradece.")),
    (("barbeador", "aparador", "maquina de cortar cabelo", "depilador"),
     ("Visual em dia sem ir ao salão.",
      "Praticidade pra se cuidar em casa.")),
    (("halter", "anilha", "esteira", "bicicleta ergometrica", "elastico", "tapete de yoga", "whey",
      "creatina", "suplemento"),
     ("Seu treino em casa começa aqui.",
      "Sem desculpa pra não treinar.")),
    (("bicicleta", "patinete", "skate", "bola", "chuteira", "raquete"),
     ("Bora se mexer pagando pouco.",
      "Diversão ao ar livre garantida.")),
    (("fralda", "carrinho de bebe", "bebe conforto", "mamadeira", "chupeta", "lenco umedecido", "berco"),
     ("Pros pais que adoram economizar.",
      "Ajuda que todo papai e mamãe precisa.")),
    (("brinquedo", "lego", "boneca", "pelucia", "quebra-cabeca", "jogo de tabuleiro", "carrinho de controle"),
     ("Diversão garantida pra criançada.",
      "Presente certeiro.")),
    (("racao", "petisco", "areia para gato", "arranhador", "caminha pet", "coleira", "cachorro", "gato"),
     ("Seu pet merece esse mimo.",
      "Pro seu melhor amigo de quatro patas.")),
    (("lampada", "luminaria", "fita led", "led", "abajur", "spot"),
     ("Muda o clima da casa inteira.",
      "Iluminação nova por pouco.")),
    (("colchao", "travesseiro", "jogo de cama", "edredom", "cobertor", "lencol"),
     ("Noite de sono de outro nível.",
      "Seu descanso agradece.")),
    (("livro", "kindle"),
     ("Leitura boa por um precinho desses.",
      "Pra quem ama ler.")),
    (("cafe", "capsula", "chocolate", "cerveja", "vinho", "whisky", "azeite"),
     ("Pra abastecer a despensa pagando menos.",
      "Aquele mimo que cabe no bolso.")),
]

GENERIC_WITH_DISCOUNT = (
    "Preço desses não dura muito.",
    "Corre que esse preço some rapidinho.",
    "Daqueles achados que dá gosto de compartilhar.",
    "Oportunidade boa demais pra deixar passar.",
)
GENERIC = (
    "Achado que vale a pena conferir.",
    "Vale a pena dar uma olhada.",
)

_SPACES = re.compile(r"\s+")


def normalize(text: str) -> str:
    """minúsculas, sem acento, com espaços nas pontas (para casar palavras como ' tv ')."""
    plain = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return f" {_SPACES.sub(' ', re.sub(r'[^a-z0-9-]+', ' ', plain.lower())).strip()} "


def phrase_for(title: str, discount_pct: int | None = None) -> str:
    text = normalize(title)
    pick = zlib.crc32((title or "").encode())
    for keywords, phrases in GROUPS:
        if any(k in text for k in keywords):
            return phrases[pick % len(phrases)]
    pool = GENERIC_WITH_DISCOUNT if discount_pct and discount_pct >= 15 else GENERIC
    return pool[pick % len(pool)]
