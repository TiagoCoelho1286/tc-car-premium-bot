import json
import os
import re
import secrets
import threading
import time
import unicodedata
import zlib
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, redirect, request, session
from supabase import create_client


app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))

OLX_CLIENT_ID = os.environ.get("OLX_CLIENT_ID")
OLX_CLIENT_SECRET = os.environ.get("OLX_CLIENT_SECRET")
OLX_REDIRECT_URI = os.environ.get("OLX_REDIRECT_URI")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY")

if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
    raise RuntimeError("Faltam SUPABASE_URL ou SUPABASE_SECRET_KEY.")

supabase = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)

OLX_AUTHORIZE_URL = "https://www.olx.pt/oauth/authorize/"
OLX_TOKEN_URL = "https://www.olx.pt/api/open/oauth/token"
OLX_API_BASE = "https://www.olx.pt/api/partner"

POLL_SECONDS = max(10, int(os.environ.get("POLL_SECONDS", "15")))
AUTO_POLL = os.environ.get("AUTO_POLL", "true").lower() in {"1", "true", "yes", "sim"}

OLX_DEBUG = os.environ.get("OLX_DEBUG", "true").lower() in {"1", "true", "yes", "sim"}

INITIALIZED_SENTINEL = "__TC_CAR_PREMIUM_BOT_INITIALIZED__"
BASELINE_PREFIX = "__TC_CAR_PREMIUM_BASELINE__|"
MAX_REPLIES_PER_RUN = max(1, int(os.environ.get("MAX_REPLIES_PER_RUN", "10")))
ADVERT_CACHE_SECONDS = 600
_REPLIED_IDS = set()
# Só relê as mensagens de uma conversa se ela mudou (total_count) ou tem não lidas.
# De FULL_SCAN_EVERY em FULL_SCAN_EVERY ciclos relê tudo, como rede de segurança.
FULL_SCAN_EVERY = max(1, int(os.environ.get("FULL_SCAN_EVERY", "20")))
# Conversas com atividade nas últimas HOT_THREAD_HOURS horas são lidas em TODOS os
# ciclos (não dependem dos contadores da lista /threads, que podem estar desatualizados).
# As restantes são lidas à vez: cada uma pelo menos de FULL_SCAN_EVERY em FULL_SCAN_EVERY ciclos.
HOT_THREAD_HOURS = max(1, int(os.environ.get("HOT_THREAD_HOURS", "48")))
OLX_DEBUG_VERBOSE = os.environ.get("OLX_DEBUG_VERBOSE", "false").lower() in {"1", "true", "yes", "sim"}
_THREAD_SEEN = {}      # conversa -> total_count da última vez que ficou TRATADA
_THREAD_ACTIVITY = {}  # conversa -> data da mensagem mais recente (naive, UTC)
_LAST_CYCLE = {}
_CYCLE_COUNT = 0
process_lock = threading.Lock()


def api_items(response):
    """Aceita tanto respostas OLX em lista como {'data': [...]}."""
    payload = response.json()
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        data = payload.get("data", [])
        return data if isinstance(data, list) else []
    return []


def api_items_from(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        data = payload.get("data", [])
        return data if isinstance(data, list) else []
    return []


def get_latest_tokens():
    token_data = (
        supabase.table("olx_tokens")
        .select("access_token, refresh_token")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not token_data.data:
        return None, None
    row = token_data.data[0]
    return row.get("access_token"), row.get("refresh_token")


def refresh_olx_token(refresh_token):
    if not refresh_token:
        return None, None

    response = requests.post(
        OLX_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": OLX_CLIENT_ID,
            "client_secret": OLX_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "scope": "v2 read write",
        },
        timeout=20,
    )

    if not response.ok:
        return None, None

    tokens = response.json()
    new_access_token = tokens.get("access_token")
    new_refresh_token = tokens.get("refresh_token", refresh_token)

    if not new_access_token:
        return None, None

    supabase.table("olx_tokens").insert(
        {
            "access_token": new_access_token,
            "refresh_token": new_refresh_token,
        }
    ).execute()

    return new_access_token, new_refresh_token


def olx_request(method, path, access_token, refresh_token, **kwargs):
    url = path if path.startswith("http") else f"{OLX_API_BASE}{path}"

    headers = kwargs.pop("headers", {})
    headers.update(
        {
            "Authorization": f"Bearer {access_token}",
            "Version": "2.0",
        }
    )

    response = requests.request(
        method,
        url,
        headers=headers,
        timeout=20,
        **kwargs,
    )

    if response.status_code != 401:
        return response, access_token, refresh_token

    new_access_token, new_refresh_token = refresh_olx_token(refresh_token)
    if not new_access_token:
        return response, access_token, refresh_token

    headers["Authorization"] = f"Bearer {new_access_token}"
    response = requests.request(
        method,
        url,
        headers=headers,
        timeout=20,
        **kwargs,
    )

    return response, new_access_token, new_refresh_token


def message_key(message):
    # A documentação atual recomenda UUID. Mantemos fallback para ID antigo.
    value = message.get("uuid") or message.get("id")
    return str(value) if value is not None else None


def is_processed(message_id):
    if not message_id:
        return True
    result = (
        supabase.table("olx_mensagens_processadas")
        .select("message_id")
        .eq("message_id", message_id)
        .limit(1)
        .execute()
    )
    return bool(result.data)


def mark_processed(message_id, thread_uuid="", advert_id=""):
    if not message_id:
        return
    if is_processed(message_id):
        return

    supabase.table("olx_mensagens_processadas").insert(
        {
            "message_id": str(message_id),
            "thread_uuid": str(thread_uuid or ""),
            "advert_id": str(advert_id or ""),
        }
    ).execute()


def greeting():
    hour = datetime.now(ZoneInfo("Europe/Lisbon")).hour
    if hour < 12:
        return "Bom dia."
    if hour < 20:
        return "Boa tarde."
    return "Boa noite."


# ---------------------------------------------------------------------------
# Datas (o OLX devolve created_at como texto, ex.: "2026-10-06 21:24:54")
# ---------------------------------------------------------------------------
def parse_ts(value):
    """Converte created_at em datetime 'naive'. Devolve None se não perceber."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text or text.upper() == "NONE":
        return None
    text = text.replace("Z", "+00:00")

    dt = None
    try:
        dt = datetime.fromisoformat(text.replace(" ", "T", 1))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# ---------------------------------------------------------------------------
# Dados do anúncio
#
# Arquitetura: o anúncio inteiro é convertido numa lista de "factos"
# (nome + valor). Cada pergunta do cliente corresponde a um TEMA (km, ano,
# combustível...) e cada tema sabe que nomes de campo procurar nesses factos.
# Para responder a uma pergunta nova basta acrescentar uma linha em
# TOPIC_FIELDS (nomes do campo) e em INTENT_PATTERNS (como o cliente pergunta).
# ---------------------------------------------------------------------------
_ADVERT_CACHE = {}
_ADVERT_LIST_CACHE = {"ts": 0.0, "by_id": {}}
ADVERT_LIST_CACHE_SECONDS = 120


def _to_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d,.\-]", "", value)
        if not cleaned:
            return None
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif re.fullmatch(r"\d{1,3}(?:,\d{3})+", cleaned):
            cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        elif cleaned.count(".") == 1 and len(cleaned.split(".")[1]) == 3:
            cleaned = cleaned.replace(".", "")
        elif cleaned.count(".") > 1:
            cleaned = cleaned.replace(".", "")
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _attribute_items(advert):
    items = []
    for field in ("attributes", "params"):
        value = advert.get(field) if isinstance(advert, dict) else None
        if isinstance(value, list):
            items.extend(i for i in value if isinstance(i, dict))
    return items


def _item_ident(item):
    parts = [item.get(k) for k in ("code", "key", "urn", "name")]
    return " ".join(str(p) for p in parts if p).lower()


def _item_value(item):
    value = item.get("value")
    if value is None and isinstance(item.get("values"), list) and item["values"]:
        value = item["values"][0]
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, dict):
        value = value.get("value") if value.get("value") is not None else value.get("key")
    return value


def _group_digits(number):
    """Milhares com ponto, como em Portugal: 154500 -> '154.500'."""
    return f"{int(round(number)):,}".replace(",", ".")


def extract_price(advert):
    """Preço do anúncio como texto (ex.: '18.500 €') ou None se não existir."""
    if not isinstance(advert, dict):
        return None
    price = advert.get("price")
    currency = ""
    if isinstance(price, dict):
        value = _to_number(price.get("value"))
        currency = str(price.get("currency") or "").upper()
    else:
        value = _to_number(price)

    # Valores simbólicos (ex.: 1 €) não são preços reais.
    if value is None or value < 100:
        value = None
        for item in _attribute_items(advert):
            ident = _item_ident(item)
            if re.search(r"price|preco", ident) and not re.search(r"negoci|trade|troca|unit", ident):
                candidate = _to_number(_item_value(item))
                if candidate and candidate >= 100:
                    value = candidate
                    break
    if value is None:
        return None
    if currency and currency != "EUR":
        return f"{_group_digits(value)} {currency}"
    return f"{_group_digits(value)} €"


# ---- 1) anúncio -> factos ---------------------------------------------------
_SKIP_KEYS = {
    "contact", "phone", "email", "user", "images", "photos", "courier",
    "external_url", "external_id", "url", "map", "salary", "delivery",
}
_EMPTY_VALUES = {"", "none", "null", "nan", "-", "n/a", "na", "undefined"}
_MAX_FACT_VALUE = 300


def _norm_ident(*parts):
    text = " ".join(str(p) for p in parts if p not in (None, ""))
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _value_text(value):
    """Valor legível: aceita texto, número, bool, listas e {label/name/value...}."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "sim" if value else "não"
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("label", "name", "title", "value", "key", "code"):
            if key in value:
                text = _value_text(value[key])
                if text:
                    return text
        return None
    if isinstance(value, list):
        parts = [t for t in (_value_text(v) for v in value) if t]
        return ", ".join(parts) if parts else None
    return None


_DESC_LINE = re.compile(r"^\s*(?:[-•*·▪►✔✅]\s*)?([^\W\d_][\w ./ºª()\-]{1,30}?)\s*:\s*(.{1,80}?)\s*$")


def _description_lines(description):
    text = str(description or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return text.splitlines()[:150]


def extract_facts(advert):
    """Todos os dados do anúncio como lista de factos {ident, label, value, source}."""
    facts = []
    if not isinstance(advert, dict):
        return facts

    def add(ident, label, value, source):
        text = _value_text(value)
        if text is None or text.lower() in _EMPTY_VALUES:
            return
        facts.append(
            {"ident": ident, "label": label or "", "value": text[:_MAX_FACT_VALUE], "source": source}
        )

    # Atributos estruturados (matrícula, km, combustível...)
    for item in _attribute_items(advert):
        value = item.get("value")
        if value in (None, "", []):
            value = item.get("values")
        label = _value_text(item.get("label") or item.get("name") or item.get("title"))
        code = _norm_ident(
            item.get("code"), item.get("key"), item.get("urn"), item.get("slug"),
            item.get("id") if isinstance(item.get("id"), str) else None,
        )
        add(_norm_ident(code, label), label, value, "atributo")

    # Restantes campos do anúncio (preço, localização, título...)
    def walk(node, path):
        if isinstance(node, dict):
            for key, val in node.items():
                if str(key).lower() in _SKIP_KEYS or key in ("attributes", "params"):
                    continue
                walk(val, path + [str(key)])
        elif isinstance(node, list):
            if node and all(not isinstance(x, (dict, list)) for x in node):
                add(_norm_ident(*path), "", node, "campo")
            else:
                for x in node:
                    walk(x, path)
        else:
            add(_norm_ident(*path), "", node, "campo")

    walk(advert, [])

    # Linhas "Etiqueta: valor" escritas na descrição (ex.: "Km: 154.500")
    count = 0
    for line in _description_lines(advert.get("description")):
        match = _DESC_LINE.match(line)
        if not match:
            continue
        label, value = match.group(1).strip(), match.group(2).strip()
        add(_norm_ident(label), label, value, "descricao")
        count += 1
        if count >= 60:
            break
    return facts


_SOURCE_ORDER = ("atributo", "campo", "descricao")


def find_facts(facts, include, exclude=None):
    """Todos os factos cujo nome corresponde a 'include' (atributos primeiro)."""
    inc = re.compile(include)
    exc = re.compile(exclude) if exclude else None
    out = []
    for source in _SOURCE_ORDER:
        for fact in facts:
            if fact["source"] != source:
                continue
            if inc.search(fact["ident"]) and not (exc and exc.search(fact["ident"])):
                out.append(fact)
    return out


def find_fact(facts, include, exclude=None):
    """Primeiro facto cujo nome corresponde a 'include' (atributos primeiro)."""
    found = find_facts(facts, include, exclude)
    return found[0] if found else None


# ---- 2) temas: que campos procurar ------------------------------------------
_DEFAULT_EXCLUDE = r"\b(?:unit|units|unidade|range)\b"
TOPIC_EXCLUDE = {
    "cilindrada": _DEFAULT_EXCLUDE + r"|\b(?:battery|bateria|tank|deposito|trunk|mala|bagageira|load|carga)\b",
    "potencia": _DEFAULT_EXCLUDE + r"|\b(?:steering|direcao|windows?|vidros|mirrors?|espelhos|seats?|bancos)\b",
    "combustivel": _DEFAULT_EXCLUDE + r"|\b(?:consum\w*|consumption|emission\w*|emissoes|tank|deposito|capacity)\b",
    "modelo": _DEFAULT_EXCLUDE + r"|\b(?:year|ano|version|versao)\b",
}

TOPIC_FIELDS = {
    "km": r"\b(?:mileage|milage|milleage|kilomet\w*|odometer|quilomet\w*|quilometragem|kms?)\b",
    "ano": r"\b(?:year|ano|anos? de fabrico|first registration|registration year|registo|matricula)\b",
    "cilindrada": r"\b(?:enginesize|engine size|engine capacity|capacity|cilindrada|displacement|cubic capacity|cc|cm3)\b",
    "potencia": r"\b(?:enginepower|engine power|power|potencia|horsepower|hp|bhp|cv|kw|cavalos)\b",
    "combustivel": r"\b(?:petrol|fuel|fueltype|fuel type|combustivel|carburante)\b",
    "caixa": r"\b(?:gearbox|transmission|caixa|cambio|caixa de velocidades)\b",
    "donos": r"\b(?:owners?|donos?|proprietarios?|number of owners|numero de donos)\b",
    "cor": r"\b(?:colou?r|cor|cores)\b",
    "portas": r"\b(?:doors?|portas?|number of doors|numero de portas)\b",
    "lugares": r"\b(?:seats?|lugares|assentos|number of seats|numero de lugares)\b",
    "carroceria": r"\b(?:car body|carbody|body type|bodytype|body|carroceria|segmento)\b",
    "marca": r"\b(?:make|brand|marca|manufacturer)\b",
    "modelo": r"\b(?:model|modelo)\b",
    "garantia": r"\b(?:warranty|garantia|guarantee)\b",
}

# Um tema "dado" tem sempre resposta concreta quando o campo existe.
DATA_TOPICS = (
    "preco", "km", "ano", "motor", "cilindrada", "potencia", "combustivel",
    "caixa", "donos", "cor", "portas", "lugares", "carroceria", "modelo", "garantia",
)

TOPIC_NOUN = {
    "preco": "o preço",
    "km": "a quilometragem",
    "ano": "o ano",
    "motor": "os dados do motor",
    "cilindrada": "a cilindrada",
    "potencia": "a potência",
    "combustivel": "o combustível",
    "caixa": "o tipo de caixa",
    "donos": "o número de donos",
    "cor": "a cor",
    "portas": "o número de portas",
    "lugares": "o número de lugares",
    "carroceria": "o tipo de carroçaria",
    "modelo": "a marca e o modelo",
}

_FUEL_MAP = {
    "diesel": "diesel", "gasoleo": "diesel", "petrol": "gasolina", "gasoline": "gasolina",
    "gasolina": "gasolina", "lpg": "GPL", "gpl": "GPL", "cng": "GNC", "gnc": "GNC",
    "hybrid": "híbrido", "hibrido": "híbrido", "electric": "elétrico",
    "eletrico": "elétrico", "electrico": "elétrico", "ethanol": "etanol",
}
_GEARBOX_MAP = {
    "manual": "manual", "automatic": "automática", "automatica": "automática",
    "automatico": "automática", "semi automatic": "semiautomática",
    "semiautomatica": "semiautomática", "semi automatica": "semiautomática",
}
_YES = {"sim", "yes", "true", "1", "y", "s"}
_NO = {"nao", "no", "false", "0", "n"}


def _mapped(value, mapping):
    key = normalize_text(value).replace("-", " ")
    return mapping.get(key, value.strip().lower())


def _fact_value(facts, topic):
    fact = find_fact(facts, TOPIC_FIELDS[topic], TOPIC_EXCLUDE.get(topic, _DEFAULT_EXCLUDE))
    return (fact["value"] if fact else None), fact


def _title(advert):
    return str(advert.get("title") or "") if isinstance(advert, dict) else ""


def _km_value(facts, advert):
    value, _ = _fact_value(facts, "km")
    number = _to_number(value) if value is not None else None
    if number is None:
        m = re.search(r"(?<![\w.])(\d{1,3}(?:[. ]\d{3})+|\d{4,7})\s?km\b", _title(advert), re.I)
        number = _to_number(m.group(1)) if m else None
    if number is None or not (0 <= number < 2_000_000):
        return None
    return number


def _year_value(facts):
    # Percorre todos os candidatos: 'matrícula' pode ser a chapa e não o ano.
    for fact in find_facts(facts, TOPIC_FIELDS["ano"], _DEFAULT_EXCLUDE):
        m = re.search(r"(?<!\d)(19[5-9]\d|20[0-4]\d)(?!\d)", fact["value"])
        if m:
            return m.group(1)
    return None


def _cilindrada_text(facts):
    value, _ = _fact_value(facts, "cilindrada")
    number = _to_number(value) if value is not None else None
    if not number or number <= 0:
        return None
    if number < 20:
        return f"{number:.1f} L"
    return f"{_group_digits(number)} cm³"


def _potencia_text(facts, advert):
    value, fact = _fact_value(facts, "potencia")
    number = _to_number(value) if value is not None else None
    unit = "kW" if fact and re.search(r"\bkw\b", fact["ident"] + " " + normalize_text(value or "")) else "cv"
    if number is None:
        m = re.search(r"(?<!\d)(\d{2,3})\s?cv\b", _title(advert), re.I)
        number = float(m.group(1)) if m else None
        unit = "cv"
    if not number or number <= 0 or number > 2000:
        return None
    return f"{int(round(number))} {unit}"


def _fuel_text(facts):
    value, _ = _fact_value(facts, "combustivel")
    return _mapped(value, _FUEL_MAP) if value else None


def _gearbox_text(facts):
    value, _ = _fact_value(facts, "caixa")
    return _mapped(value, _GEARBOX_MAP) if value else None


def _fact_sentence(topic, facts, advert, text=""):
    """Frase com o VALOR CONCRETO do anúncio, ou None se o dado não existir."""
    qn = normalize_text(text)

    if topic == "preco":
        price = extract_price(advert)
        return f"O valor da viatura é {price}." if price else None

    if topic == "km":
        km = _km_value(facts, advert)
        return f"Tem {_group_digits(km)} km." if km is not None else None

    if topic == "ano":
        year = _year_value(facts)
        return f"É de {year}." if year else None

    if topic == "cilindrada":
        c = _cilindrada_text(facts)
        return f"A cilindrada é {c}." if c else None

    if topic == "potencia":
        p = _potencia_text(facts, advert)
        return f"Tem {p}." if p else None

    if topic == "combustivel":
        f = _fuel_text(facts)
        return f"O combustível é {f}." if f else None

    if topic == "motor":
        parts = [p for p in (_cilindrada_text(facts), _fuel_text(facts), _potencia_text(facts, advert)) if p]
        return ("Tem motor " + ", ".join(parts) + ".") if parts else None

    if topic == "caixa":
        g = _gearbox_text(facts)
        if not g:
            return None
        asked = None
        if re.search(r"automatic", qn):
            asked = "automática"
        elif re.search(r"\bmanual\b", qn):
            asked = "manual"
        if asked:
            if g.startswith(asked[:6]):
                return f"Sim, a caixa é {g}."
            return f"Não, a caixa é {g}."
        return f"A caixa é {g}."

    if topic == "donos":
        value, _ = _fact_value(facts, "donos")
        if not value:
            return None
        if re.search(r"unico|primeiro", normalize_text(value)):
            return "Tem 1 dono."
        m = re.search(r"\d+", value)
        if m:
            n = int(m.group(0))
            return f"Tem {n} dono." if n == 1 else f"Tem {n} donos."
        return f"Número de donos: {value}."

    if topic == "cor":
        value, _ = _fact_value(facts, "cor")
        return f"A cor é {value.lower()}." if value else None

    if topic == "portas":
        value, _ = _fact_value(facts, "portas")
        number = _to_number(value) if value else None
        return f"Tem {int(number)} portas." if number and 0 < number < 10 else None

    if topic == "lugares":
        value, _ = _fact_value(facts, "lugares")
        number = _to_number(value) if value else None
        return f"Tem {int(number)} lugares." if number and 0 < number < 20 else None

    if topic == "carroceria":
        value, _ = _fact_value(facts, "carroceria")
        return f"A carroçaria é {value}." if value else None

    if topic == "modelo":
        make, _ = _fact_value(facts, "marca")
        model, _ = _fact_value(facts, "modelo")
        name = " ".join(x for x in (make, model) if x)
        if name:
            return f"É um {name}."
        title = _title(advert).strip()
        return f"O anúncio refere: {title}." if title else None

    if topic == "garantia":
        value, _ = _fact_value(facts, "garantia")
        if not value:
            return None
        norm = normalize_text(value)
        if norm in _YES:
            return "Sim, a viatura tem garantia."
        if norm in _NO or re.fullmatch(r"\d+", norm):
            return None  # sem dado fiável: usa o texto padrão da TC Car Premium
        if re.match(r"\d", norm):
            return f"Tem garantia de {value}."
        return f"Garantia: {value}."

    return None


_GENERIC_SKIP_LABELS = {"title", "description", "status", "id", "url", "price", "name"}


def generic_fact_answers(text, facts, limit=2):
    """
    Pergunta sem tema conhecido: se o cliente escreveu o nome de um campo que
    existe no anúncio (ex.: 'ar condicionado'), responde com o valor desse campo.
    """
    qn = normalize_text(text)
    qn = re.sub(r"[^a-z0-9 ]+", " ", qn)
    answers, seen = [], set()
    for fact in facts:
        label = fact["label"]
        if not label or fact["source"] == "campo":
            continue
        ln = re.sub(r"[^a-z0-9 ]+", " ", normalize_text(label)).strip()
        if len(ln) < 3 or ln in _GENERIC_SKIP_LABELS or ln in seen:
            continue
        if re.search(rf"\b{re.escape(ln)}\b", qn):
            seen.add(ln)
            answers.append(f"{label}: {fact['value']}.")
            if len(answers) >= limit:
                break
    return answers


# ---- 3) ler o anúncio na API ------------------------------------------------
def _advert_from_listing(key, access_token, refresh_token):
    """Plano B: procura o anúncio na lista de anúncios da conta."""
    cache = _ADVERT_LIST_CACHE
    if time.time() - cache["ts"] > ADVERT_LIST_CACHE_SECONDS or key not in cache["by_id"]:
        items, access_token, refresh_token = fetch_all(
            "/adverts", "anúncios", access_token, refresh_token
        )
        cache["by_id"] = {str(i.get("id")): i for i in items if isinstance(i, dict) and i.get("id") is not None}
        cache["ts"] = time.time()
    return cache["by_id"].get(key) or {}, access_token, refresh_token


def fetch_advert(advert_id, access_token, refresh_token):
    """Lê o anúncio (com cache curta). Nunca levanta erro: sem dados devolve {}."""
    if not advert_id:
        return {}, access_token, refresh_token
    key = str(advert_id)
    cached = _ADVERT_CACHE.get(key)
    if cached and time.time() - cached[0] < ADVERT_CACHE_SECONDS:
        return cached[1], access_token, refresh_token

    advert = {}
    try:
        response, access_token, refresh_token = olx_request(
            "GET", f"/adverts/{key}", access_token, refresh_token
        )
        if response.ok:
            payload = response.json()
            data = payload.get("data", payload) if isinstance(payload, dict) else {}
            advert = data if isinstance(data, dict) else {}
        else:
            debug_log(f"anúncio {key}: GET /adverts/{key} devolveu HTTP {response.status_code}")
    except Exception as exc:
        debug_log(f"anúncio {key}: erro a ler ({exc})")

    if not advert:
        try:
            advert, access_token, refresh_token = _advert_from_listing(key, access_token, refresh_token)
            if advert:
                debug_log(f"anúncio {key}: obtido pela lista de anúncios")
        except Exception as exc:
            debug_log(f"anúncio {key}: erro a ler a lista de anúncios ({exc})")

    if not advert:
        debug_log(f"anúncio {key}: SEM DADOS (as respostas não vão ter valores do anúncio)")
        return {}, access_token, refresh_token

    _ADVERT_CACHE[key] = (time.time(), advert)
    if OLX_DEBUG:
        facts = extract_facts(advert)
        debug_log(f"anúncio {key}: campos={sorted(advert.keys())}")
        debug_log(
            f"anúncio {key}: factos="
            + json.dumps(
                [{"nome": f["ident"], "valor": f["value"][:50], "origem": f["source"]} for f in facts[:80]],
                ensure_ascii=False,
            )
        )
        debug_log(
            f"anúncio {key}: resolução="
            + json.dumps(
                {t: _fact_sentence(t, facts, advert) for t in DATA_TOPICS}, ensure_ascii=False
            )
        )
    return advert, access_token, refresh_token


# ---------------------------------------------------------------------------
# Intenções
# ---------------------------------------------------------------------------
def normalize_text(text):
    """Minúsculas, sem acentos e com espaços normalizados."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower()).strip()


INTENT_PATTERNS = {
    "disponibilidade": re.compile(
        r"disponivel|disponibilidade|ainda esta a venda|ainda a venda|ainda existe"
        r"|ainda (?:tem|tens|existe) (?:o|a|esse|essa) (?:carro|viatura|anuncio|automovel|veiculo)"
        r"|ja (?:foi )?vendid[oa]|ja vendeu|ja vendeste|ja venderam|esta vendid[oa]|foi vendid[oa]"
    ),
    "negociacao": re.compile(
        r"negociavel|negociar|faz(?:em)? desconto|desconto|baixa(?:m)? (?:o )?preco"
        r"|melhor preco|preco minimo|ultimo preco|valor minimo|ultimo valor|\bminimo\b|margem"
    ),
    "preco": re.compile(
        r"preco|valor|quanto (?:e|fica|custa|pedem|pede|querem|quer|levam|leva)|custa|custo|por quanto"
    ),
    "retoma": re.compile(
        r"retoma"
        r"|\btroca\b(?!\s+de\s+(?:oleo|correia|distribuicao|pneus|pastilhas|travoes|embraiagem|filtros))"
        r"|\btrocar\b|dar o meu|dar a minha|entregar o meu|entregar a minha"
    ),
    "financiamento": re.compile(
        r"financiamento|financiam\w*|financiar|credito|prestac\w*|mensalidades?"
    ),
    "localizacao": re.compile(
        r"onde (?:estao|fica|ficam|sao|se encontra|se situa|vendem|moram)"
        r"|onde esta (?:o|a) (?:carro|viatura|veiculo)|localizacao|localidade|morada"
        r"|onde posso ver|onde (?:e )?ver|onde tem (?:os )?carros|onde tem o carro"
        r"|em que (?:zona|cidade|localidade|concelho)|de onde (?:e|sao|esta|estao)|\bstand\b"
    ),
    "garantia": re.compile(r"garantia"),
    "visita": re.compile(
        r"posso ir ver|ir ver (?:o|a)|ver (?:o|a) (?:carro|viatura|veiculo|automovel)"
        r"|ver pessoalmente|quero ver(?! (?:mais )?(?:fot|imag|video))"
        r"|marcar|marcacao|agendar|visitar|visita|test[- ]?drive|teste de conducao"
        r"|experimentar|conduzir|passar por ai|passar ai|ir ai"
    ),
    "fotos": re.compile(r"\bfotos?\b|fotografias?|imagens|\bvideos?\b"),
    "km": re.compile(r"(?<![a-z])kms?\b|quilomet\w*|kilomet\w*"),
    "ano": re.compile(
        r"(?:que|qual|de que|em que|do que) ano\b|qual (?:e )?o ano\b"
        r"|\bano (?:do|da|dele|dela|matricula|registo|fabrico|modelo)\b"
        r"|\bano de (?:matricula|registo|fabrico|construcao)\b"
        r"|primeira matricula|data de (?:matricula|registo)"
    ),
    "motor": re.compile(r"\bmotor\b|motorizacao|motorizado"),
    "cilindrada": re.compile(r"cilindrada|\bcc\b|\bcm3\b"),
    "potencia": re.compile(r"cavalos|\bcv\b|\bhp\b|\bbhp\b|potencia|\bkw\b"),
    "combustivel": re.compile(
        r"combustivel|\bdiesel\b|gasoleo|gasolina|\bgpl\b|\bgnc\b|hibrido|eletrico|electrico"
    ),
    "caixa": re.compile(
        r"\bcaixa\b|\bautomatic[ao]s?\b|\bmanual\b(?! de)|transmissao|\bdsg\b|\bcambio\b"
    ),
    "donos": re.compile(
        r"\bdonos\b|proprietarios|(?:primeiro|unico|segundo|1o|2o)\s+dono|\bdono\s+(?:anterior|unico)"
    ),
    "cor": re.compile(r"\bcor\b|\bcores\b"),
    "portas": re.compile(r"\bportas\b"),
    "lugares": re.compile(r"\blugares\b|\bassentos\b|quantas pessoas"),
    "carroceria": re.compile(
        r"carroceria|\bsuv\b|\bsedan\b|\bberlina\b|\bstation\b|\bcarrinha\b|\bmonovolume\b"
        r"|\bhatchback\b|\bcabrio\w*|que tipo de (?:carro|viatura)"
    ),
    "modelo": re.compile(r"\bmodelo\b|\bmarca\b|que carro e\b|que viatura e\b"),
    "historico": re.compile(
        r"historico|revisoes|revisao|manutencao|livro de revisoes|correia|distribuicao|oleo"
        r"|acidente|sinistro"
    ),
    "contacto": re.compile(
        r"contacto|contato|telefone|telemovel|whatsapp|whats app|\bzap\b|\bligar\b|\bligo\b"
        r"|numero(?: de)? (?:telefone|telemovel|contacto|whatsapp|zap)"
        r"|(?:vosso|seu|teu|o|um) numero\b(?!\s+d)"
    ),
}

# Perguntas explícitas sobre o preço do carro (não confundir com retoma/mensalidade).
PRICE_EXPLICIT = re.compile(
    r"quanto (?:custa|pedem|pede|querem|quer)|por quanto"
    r"|qual (?:e )?o (?:preco|valor)(?!\s+(?:da|de|das|do)\s+(?:retoma|entrada|troca|prestac\w*|mensalidade))"
    r"|(?:preco|valor) (?:do|da) (?:carro|veiculo|viatura|anuncio)"
)
VISIT_STRONG = re.compile(r"marcar|marcacao|agendar|test[- ]?drive|teste de conducao|experimentar|conduzir")

MAX_INTENTS_PER_REPLY = 5


def detect_intents(text):
    """Intenções reconhecidas, pela ordem em que aparecem na mensagem."""
    t = normalize_text(text)
    found = {}
    for name, pattern in INTENT_PATTERNS.items():
        match = pattern.search(t)
        if match:
            found[name] = match.start()

    # "preço" solto, junto de negociação/retoma/financiamento, não é pergunta de preço.
    if "preco" in found and not PRICE_EXPLICIT.search(t):
        if any(k in found for k in ("negociacao", "retoma", "financiamento")):
            del found["preco"]
    # "Onde posso ver o carro?" é localização, não pedido de marcação.
    if "visita" in found and "localizacao" in found and not VISIT_STRONG.search(t):
        del found["visita"]
    # A resposta sobre o motor já inclui cilindrada, combustível e potência.
    if "motor" in found:
        for sub in ("cilindrada", "potencia", "combustivel"):
            found.pop(sub, None)

    return sorted(found, key=lambda name: found[name])[:MAX_INTENTS_PER_REPLY]


HELP_CLOSING = "Onde podemos ajudar?"
VISIT_CLOSING = "Se pretender, podemos combinar uma visita para a ver."
CONTACT_CLOSING = "Para confirmarmos, pode contactar-nos pelo WhatsApp 962 148 367."

# (corpo, fecho). O fecho só é usado quando há uma única intenção.
STATIC_REPLIES = {
    "disponibilidade": ("Sim, a viatura continua disponível.", HELP_CLOSING),
    "negociacao": (
        "Existe alguma margem para negociação, mas preferimos falar sobre valores "
        "depois de ver a viatura.",
        HELP_CLOSING,
    ),
    "retoma": (
        "Sim, podemos avaliar uma possível retoma. Envie-nos, por favor, algumas "
        "fotografias da viatura, marca, modelo, ano, quilometragem e motorização "
        "para o WhatsApp 962 148 367 e fazemos uma avaliação.",
        "",
    ),
    "financiamento": (
        "De momento estamos a atualizar as nossas soluções de financiamento, pelo "
        "que temporariamente não estamos a realizar novos processos. Prevemos voltar "
        "a disponibilizar esta opção em breve.",
        "",
    ),
    "localizacao": (
        "Pode ver a viatura mediante marcação na Ruela da Cavada Nova, n.º 74, "
        "4585-053 Baltar, Paredes. Se pretender, podemos combinar um dia e horário.",
        "",
    ),
    "garantia": (
        "As condições de garantia dependem da viatura e das condições da venda. "
        "Quando aplicável, trabalhamos com garantia até 18 meses. Podemos confirmar "
        "as condições específicas desta viatura.",
        "",
    ),
    "visita": (
        "Claro. Podemos combinar uma visita para ver a viatura e esclarecer todas "
        "as questões. Indique-nos, por favor, o dia e horário que lhe dão mais jeito.",
        "",
    ),
    "fotos": (
        "Claro. Podemos enviar mais fotografias ou vídeos da viatura. Diga-nos que "
        "detalhes pretende ver ou contacte-nos pelo WhatsApp 962 148 367.",
        "",
    ),
    "historico": (
        "Podemos esclarecer toda a informação disponível sobre o histórico e "
        "manutenção desta viatura. Diga-nos concretamente o que pretende saber.",
        "",
    ),
    "contacto": ("Pode contactar-nos através do WhatsApp pelo número 962 148 367.", HELP_CLOSING),
}

# Só o preço mantém um fecho depois do valor; os restantes dados respondem direto.
DATA_CLOSINGS = {"preco": VISIT_CLOSING}


def needs_advert(intents):
    """Precisa de ler o anúncio quando há um tema de dados, ou quando não há tema nenhum."""
    return (not intents) or any(i in DATA_TOPICS for i in intents)


def _intent_text(name, facts, advert, text):
    """(corpo, fecho, faltou_dado)"""
    if name in DATA_TOPICS:
        sentence = _fact_sentence(name, facts, advert, text)
        if sentence:
            return sentence, DATA_CLOSINGS.get(name, ""), False
        if name == "garantia":  # sem dado no anúncio: texto padrão da TC Car Premium
            body, closing = STATIC_REPLIES["garantia"]
            return body, closing, False
        return f"De momento não tenho {TOPIC_NOUN[name]} disponível.", "", True
    body, closing = STATIC_REPLIES[name]
    return body, closing, False


def compose_reply(intents, advert=None, text=""):
    saudacao = greeting()
    advert = advert if isinstance(advert, dict) else {}
    facts = extract_facts(advert)

    if not intents:
        generic = generic_fact_answers(text, facts)
        if generic:
            return " ".join([saudacao] + generic)
        return f"{saudacao} Obrigado pelo seu contacto com a TC Car Premium. Onde podemos ajudar?"

    parts = [_intent_text(name, facts, advert, text) for name in intents]
    missing = any(p[2] for p in parts)
    first_missing = True
    for i, (body, closing, lacked) in enumerate(parts):
        if lacked:
            if not first_missing:  # evita repetir "De momento não tenho" várias vezes
                parts[i] = (body.replace("De momento não tenho", "Também não tenho", 1), closing, lacked)
            first_missing = False
    if len(parts) == 1:
        body, closing, _ = parts[0]
        closing = CONTACT_CLOSING if missing else closing
        return " ".join(x for x in (saudacao, body, closing) if x)
    bodies = [p[0] for p in parts]
    if missing:
        bodies.append(CONTACT_CLOSING)
    return " ".join([saudacao] + bodies)


def build_reply(text, advert=None):
    return compose_reply(detect_intents(text), advert, text)


def debug_log(*parts):
    if OLX_DEBUG:
        print("[DEBUG]", *parts, flush=True)


_SAFE_MESSAGE_FIELDS = ("id", "uuid", "thread_id", "type", "is_read", "created_at")


def debug_shape(label, response, batch):
    """Mostra o formato real da resposta (sem conteúdo pessoal)."""
    try:
        payload = response.json()
    except Exception:
        debug_log(f"{label}: HTTP {response.status_code} resposta NÃO é JSON: {response.text[:150]!r}")
        return
    if isinstance(payload, list):
        formato = "lista"
    elif isinstance(payload, dict):
        formato = "dict com chaves " + str(sorted(payload.keys()))
        if not isinstance(payload.get("data"), list):
            formato += " (ATENÇÃO: sem lista em 'data' -> o bot vê 0 itens)"
    else:
        formato = type(payload).__name__
    debug_log(f"{label}: HTTP {response.status_code} formato={formato} itens={len(batch)}")


def debug_thread(thread, messages, stats, pending):
    pending_ids = {message_key(m) for m in pending}
    debug_log(
        f"thread={item_key(thread)} advert={thread.get('advert_id')} "
        f"unread_count={thread.get('unread_count')} total_count={thread.get('total_count')} "
        f"campos_thread={sorted(thread.keys())}"
    )
    debug_log(f"  contagem={json.dumps(stats, ensure_ascii=False)}")
    recentes = sorted(messages, key=lambda m: str(m.get("created_at")))[-3:]
    for m in recentes:
        info = {k: m.get(k) for k in _SAFE_MESSAGE_FIELDS if k in m}
        info["text_len"] = len(m.get("text") or "")
        info["outros_campos"] = sorted(k for k in m.keys() if k not in _SAFE_MESSAGE_FIELDS)
        info["chave_usada"] = message_key(m)
        info["pendente_de_resposta"] = message_key(m) in pending_ids
        debug_log("  msg " + json.dumps(info, ensure_ascii=False, default=str))


def item_key(item):
    for field in ("uuid", "id"):
        if item.get(field) is not None:
            return str(item[field])
    return None


def fetch_all(path, label, access_token, refresh_token, limit=100,
              stop_on_short_page=False, max_pages=50):
    """
    Pagina com offset/limit. O offset avança pelo número de itens realmente
    recebidos (e não por 'limit'), por isso funciona mesmo que o servidor
    limite o tamanho da página. Pára quando a página vem vazia, quando não traz
    nada de novo, ou (opcional) quando vem mais curta que 'limit'.
    """
    items, seen, offset = [], set(), 0

    for _ in range(max_pages):
        response, access_token, refresh_token = olx_request(
            "GET",
            path,
            access_token,
            refresh_token,
            params={"offset": offset, "limit": limit},
        )
        if not response.ok:
            raise RuntimeError(
                f"Erro OLX ao obter {label}: HTTP {response.status_code} - {response.text[:300]}"
            )

        batch = api_items(response)
        if offset == 0:
            debug_shape(label, response, batch)
        if not batch:
            break

        new_items = 0
        for item in batch:
            key = item_key(item)
            if key is not None and key in seen:
                continue
            if key is not None:
                seen.add(key)
            items.append(item)
            new_items += 1

        if new_items == 0:
            break
        if stop_on_short_page and len(batch) < limit:
            break
        offset += len(batch)

    return items, access_token, refresh_token


def get_threads(access_token, refresh_token):
    return fetch_all("/threads", "conversas", access_token, refresh_token)


def get_messages(thread_uuid, access_token, refresh_token):
    return fetch_all(
        f"/threads/{thread_uuid}/messages",
        "mensagens",
        access_token,
        refresh_token,
        stop_on_short_page=True,
    )


def send_message(thread_uuid, text, access_token, refresh_token):
    response, access_token, refresh_token = olx_request(
        "POST",
        f"/threads/{thread_uuid}/messages",
        access_token,
        refresh_token,
        headers={"Content-Type": "application/json"},
        json={"text": text},
    )
    return response, access_token, refresh_token


def mark_thread_read(thread_uuid, access_token, refresh_token):
    response, access_token, refresh_token = olx_request(
        "POST",
        f"/threads/{thread_uuid}/commands",
        access_token,
        refresh_token,
        headers={"Content-Type": "application/json"},
        json={"command": "mark-as-read"},
    )
    return response, access_token, refresh_token


def get_account_id(access_token, refresh_token):
    """Identifica a conta do token (só leitura). Sem id, nada é enviado."""
    response, access_token, refresh_token = olx_request(
        "GET", "/users/me", access_token, refresh_token
    )
    if not response.ok:
        raise RuntimeError(
            f"Não foi possível identificar a conta OLX (HTTP {response.status_code})."
        )
    payload = response.json()
    me = payload.get("data", payload) if isinstance(payload, dict) else {}
    account_id = me.get("id") if isinstance(me, dict) else None
    if account_id is None:
        raise RuntimeError("A resposta de /users/me não trouxe o id da conta.")
    return str(account_id), access_token, refresh_token


def baseline_key(account_id):
    return f"{BASELINE_PREFIX}{account_id}"


def get_baseline(account_id):
    """Marco da conta: o created_at mais recente que já existia na inicialização."""
    result = (
        supabase.table("olx_mensagens_processadas")
        .select("message_id,thread_uuid")
        .eq("message_id", baseline_key(account_id))
        .limit(1)
        .execute()
    )
    if not result.data:
        return None
    return {"cutoff_text": result.data[0].get("thread_uuid") or "NONE"}


def save_baseline(account_id, cutoff_text):
    supabase.table("olx_mensagens_processadas").insert(
        {
            "message_id": baseline_key(account_id),
            "thread_uuid": cutoff_text or "NONE",  # guarda aqui o created_at de corte
            "advert_id": "SYSTEM",
        }
    ).execute()


def initialize_without_replying(account_id, access_token, refresh_token):
    """
    Inicialização POR CONTA. Nunca envia nada:
      1. marca como processadas todas as mensagens recebidas que já existem;
      2. guarda o created_at mais recente visto (o 'corte').
    Só depois de tudo lido é que o marco é gravado; se falhar a meio, não fica
    marco e o bot continua sem responder até a inicialização terminar.
    """
    threads, access_token, refresh_token = get_threads(access_token, refresh_token)
    marked = 0
    newest_dt, newest_text = None, None

    for thread in threads:
        thread_uuid = thread.get("uuid") or thread.get("id")
        advert_id = thread.get("advert_id")
        if not thread_uuid:
            continue

        messages, access_token, refresh_token = get_messages(
            thread_uuid, access_token, refresh_token
        )
        for message in messages:
            ts = parse_ts(message.get("created_at"))
            if ts is not None and (newest_dt is None or ts > newest_dt):
                newest_dt, newest_text = ts, str(message.get("created_at"))

            if message.get("type") != "received":
                continue
            mid = message_key(message)
            if mid and not is_processed(mid):
                mark_processed(mid, thread_uuid, advert_id)
                marked += 1

    save_baseline(account_id, newest_text or "NONE")
    mark_processed(INITIALIZED_SENTINEL, "SYSTEM", "SYSTEM")
    print(
        f"[BOT] Inicialização da conta concluída: {marked} mensagens antigas ignoradas, "
        f"corte={newest_text or 'NONE'}, threads={len(threads)}",
        flush=True,
    )
    return {
        "estado": "inicializado",
        "mensagens_antigas_ignoradas": marked,
        "threads_verificadas": len(threads),
        "corte": newest_text or "NONE",
    }


def thread_read_reason(thread_key, thread, force_full, hot_after):
    """
    Porque é que esta conversa tem de ser lida neste ciclo (ou None para saltar).
    A lista /threads NÃO é fiável para detetar mensagens novas, por isso os
    contadores dela só aceleram; a garantia vem das conversas "quentes" e da rotação.
    """
    if force_full:
        return "completa"
    if thread_key not in _THREAD_SEEN:
        return "nova"  # conversa nunca tratada (ou a anterior falhou)
    if int(thread.get("unread_count") or 0) > 0:
        return "nao_lida"
    total = thread.get("total_count")
    if total is not None and total != _THREAD_SEEN[thread_key]:
        return "alterada"
    activity = _THREAD_ACTIVITY.get(thread_key)
    if activity is None or activity >= hot_after:
        return "quente"
    if zlib.crc32(thread_key.encode()) % FULL_SCAN_EVERY == _CYCLE_COUNT % FULL_SCAN_EVERY:
        return "rotacao"
    return None


def process_new_messages(force_full=False):
    global _CYCLE_COUNT
    if not process_lock.acquire(blocking=False):
        return {"estado": "já existe um processamento em curso"}

    try:
        access_token, refresh_token = get_latest_tokens()
        if not access_token:
            return {"erro": "Não existe nenhum token OLX guardado."}

        # Segurança principal: cada conta tem de ser inicializada antes de responder.
        account_id, access_token, refresh_token = get_account_id(access_token, refresh_token)
        baseline = get_baseline(account_id)
        if baseline is None:
            return initialize_without_replying(account_id, access_token, refresh_token)
        cutoff = parse_ts(baseline["cutoff_text"])

        _CYCLE_COUNT += 1
        full_scan = force_full
        skipped = 0
        read_counts = {}
        hot_after = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=HOT_THREAD_HOURS)
        job_totals = {}
        threads, access_token, refresh_token = get_threads(access_token, refresh_token)
        replies_sent = 0
        messages_marked = 0
        errors = []
        thread_debug = []
        unread_total = sum(int(t.get("unread_count") or 0) for t in threads)
        print(
            f"[BOT] threads={len(threads)} nao_lidas={unread_total} "
            f"ids={[item_key(t) for t in threads][:20]}",
            flush=True,
        )

        # 1.ª fase: só LER e decidir. Nada é enviado aqui.
        jobs = []
        for thread in threads:
            thread_uuid = thread.get("uuid") or thread.get("id")
            advert_id = thread.get("advert_id")

            if not thread_uuid:
                continue

            total = thread.get("total_count")
            thread_key = str(thread_uuid)
            reason = thread_read_reason(thread_key, thread, full_scan, hot_after)
            if reason is None:
                skipped += 1
                continue
            read_counts[reason] = read_counts.get(reason, 0) + 1

            try:
                messages, access_token, refresh_token = get_messages(
                    thread_uuid, access_token, refresh_token
                )
            except Exception as exc:
                errors.append(f"{thread_uuid}: {exc}")
                continue

            # Atividade mais recente da conversa (decide se é "quente").
            stamps = [t for t in (parse_ts(m.get("created_at")) for m in messages) if t is not None]
            if stamps:
                _THREAD_ACTIVITY[thread_key] = max(stamps)
            job_totals[thread_key] = total

            pending = []
            stats = {
                "mensagens": len(messages),
                "recebidas": 0,
                "enviadas": 0,
                "outros_tipos": {},
                "sem_id": 0,
                "sem_data": 0,
                "anteriores_ao_marco": 0,
                "ja_processadas": 0,
            }
            for message in messages:
                mtype = message.get("type")
                if mtype == "received":
                    stats["recebidas"] += 1
                elif mtype == "sent":
                    stats["enviadas"] += 1
                else:
                    key = str(mtype)
                    stats["outros_tipos"][key] = stats["outros_tipos"].get(key, 0) + 1
                if mtype != "received":
                    continue

                mid = message_key(message)
                if not mid:
                    stats["sem_id"] += 1
                    continue

                # Regra de ouro: o que já existia na inicialização nunca é "novo",
                # esteja ou não marcado como não lido.
                if cutoff is not None:
                    ts = parse_ts(message.get("created_at"))
                    if ts is None:
                        stats["sem_data"] += 1
                        continue
                    if ts <= cutoff:
                        stats["anteriores_ao_marco"] += 1
                        continue

                if mid in _REPLIED_IDS or is_processed(mid):
                    stats["ja_processadas"] += 1
                    continue
                pending.append(message)

            stats["pendentes"] = len(pending)
            entry = {
                "thread": str(thread_uuid),
                "unread_count": thread.get("unread_count"),
                **stats,
            }
            entry["motivo_leitura"] = reason
            thread_debug.append(entry)
            if pending or reason not in ("quente", "rotacao") or OLX_DEBUG_VERBOSE:
                debug_thread(thread, messages, stats, pending)

            if pending:
                jobs.append((thread_uuid, advert_id, pending, entry))
            else:
                _THREAD_SEEN[thread_key] = total  # nada a fazer: já está tratada

        print(
            f"[BOT] leitura: lidas={sum(read_counts.values())} {read_counts} "
            f"puladas={skipped} pendentes={len(jobs)}",
            flush=True,
        )

        # Travão de segurança: muitas conversas "novas" ao mesmo tempo não é normal.
        if len(jobs) > MAX_REPLIES_PER_RUN:
            for thread_uuid, advert_id, pending, _entry in jobs:
                for message in pending:
                    mark_processed(message_key(message), thread_uuid, advert_id)
                    messages_marked += 1
                _THREAD_SEEN[str(thread_uuid)] = job_totals.get(str(thread_uuid))
            print(
                f"[BOT] TRAVÃO: {len(jobs)} conversas pendentes (máx. {MAX_REPLIES_PER_RUN}). "
                "Nada foi enviado; as mensagens foram marcadas como processadas.",
                flush=True,
            )
            return {
                "estado": "travao_seguranca",
                "conversas_pendentes": len(jobs),
                "limite": MAX_REPLIES_PER_RUN,
                "respostas_enviadas": 0,
                "mensagens_processadas": messages_marked,
                "threads_verificadas": len(threads),
            }

        # 2.ª fase: responder. Várias mensagens seguidas => UMA resposta.
        for thread_uuid, advert_id, pending, entry in jobs:
            try:
                combined_text = " ".join((m.get("text") or "") for m in pending).strip()
                intents = detect_intents(combined_text)
                entry["intencoes"] = intents or ["generica"]

                advert = {}
                if needs_advert(intents):
                    advert, access_token, refresh_token = fetch_advert(
                        advert_id, access_token, refresh_token
                    )
                reply = compose_reply(intents, advert, combined_text)
                debug_log(f"thread={thread_uuid} intencoes={entry['intencoes']}")

                send_response, access_token, refresh_token = send_message(
                    thread_uuid,
                    reply,
                    access_token,
                    refresh_token,
                )

                if not send_response.ok:
                    errors.append(
                        f"{thread_uuid}: falha ao responder HTTP "
                        f"{send_response.status_code} - {send_response.text[:200]}"
                    )
                    continue  # continua fora de _THREAD_SEEN: volta a ser lida no ciclo seguinte

                # Só marcamos como processadas DEPOIS de o OLX confirmar o envio.
                for message in pending:
                    mid = message_key(message)
                    _REPLIED_IDS.add(mid)  # evita repetir mesmo que a base de dados falhe
                    try:
                        mark_processed(mid, thread_uuid, advert_id)
                        messages_marked += 1
                    except Exception as exc:
                        errors.append(f"{thread_uuid}: resposta enviada mas não ficou registada ({exc})")

                _THREAD_SEEN[str(thread_uuid)] = job_totals.get(str(thread_uuid))
                replies_sent += 1

                # Não é crítico para o envio; apenas tentamos limpar o não-lido.
                try:
                    mark_thread_read(thread_uuid, access_token, refresh_token)
                except Exception:
                    pass
            except Exception as exc:
                errors.append(f"{thread_uuid}: erro a responder ({type(exc).__name__}: {exc})")

        for problem in errors:
            print(f"[BOT] AVISO: {problem}", flush=True)
        _LAST_CYCLE.clear()
        _LAST_CYCLE.update(
            {
                "quando_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "conversas": len(threads),
                "lidas": sum(read_counts.values()),
                "motivos": dict(read_counts),
                "puladas": skipped,
                "pendentes": len(jobs),
                "respostas": replies_sent,
                "erros": len(errors),
            }
        )

        return {
            "estado": "ok",
            "threads_verificadas": len(threads),
            "threads_sem_alteracoes": skipped,
            "threads_lidas": sum(read_counts.values()),
            "motivos_leitura": read_counts,
            "leitura_completa": full_scan,
            "mensagens_nao_lidas_api": unread_total,
            "respostas_enviadas": replies_sent,
            "mensagens_processadas": messages_marked,
            "erros": errors,
            **({"debug_threads": thread_debug} if OLX_DEBUG else {}),
        }

    except Exception as exc:
        print(f"[BOT] ERRO no ciclo: {type(exc).__name__}: {exc}", flush=True)
        return {"erro": str(exc)}
    finally:
        process_lock.release()


def polling_loop():
    # Pequeno atraso para o servidor arrancar antes da primeira consulta.
    time.sleep(10)
    while True:
        started = time.monotonic()
        try:
            process_new_messages()
        except Exception as exc:
            print(f"[BOT] Erro no ciclo automático: {exc}", flush=True)
        elapsed = time.monotonic() - started
        if OLX_DEBUG or elapsed > POLL_SECONDS:
            print(f"[BOT] ciclo concluído em {elapsed:.1f}s (intervalo alvo {POLL_SECONDS}s)", flush=True)
        time.sleep(max(1.0, POLL_SECONDS - elapsed))


@app.route("/")
def home():
    return """
    <h1>TC Car Premium Bot</h1>
    <p>Servidor online.</p>
    <p>Bot OLX configurado para responder apenas a mensagens novas.</p>
    <a href="/olx/login">Ligar/renovar conta OLX</a>
    """


@app.route("/health")
def health():
    return jsonify({"status": "ok", "bot": "TC Car Premium"})


@app.route("/olx/login")
def olx_login():
    state = secrets.token_urlsafe(32)
    session["olx_state"] = state

    params = {
        "client_id": OLX_CLIENT_ID,
        "response_type": "code",
        "state": state,
        "scope": "read write v2",
        "redirect_uri": OLX_REDIRECT_URI,
    }

    return redirect(OLX_AUTHORIZE_URL + "?" + urlencode(params))


@app.route("/olx/callback")
def olx_callback():
    error = request.args.get("error")
    if error:
        return f"OLX devolveu um erro: {error}", 400

    code = request.args.get("code")
    returned_state = request.args.get("state")
    expected_state = session.pop("olx_state", None)

    if not code:
        return "Não foi recebido o código de autorização do OLX.", 400

    if not expected_state or returned_state != expected_state:
        return "Erro de segurança: state inválido.", 400

    token_response = requests.post(
        OLX_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": OLX_CLIENT_ID,
            "client_secret": OLX_CLIENT_SECRET,
            "code": code,
            "scope": "v2 read write",
            "redirect_uri": OLX_REDIRECT_URI,
        },
        timeout=20,
    )

    if not token_response.ok:
        return (
            "Não foi possível obter o token do OLX. "
            f"Erro HTTP: {token_response.status_code}"
        ), 500

    tokens = token_response.json()
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")

    if not access_token:
        return "O OLX não devolveu um access token.", 500

    supabase.table("olx_tokens").insert(
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
        }
    ).execute()

    return """
    <h1>TC Car Premium Bot</h1>
    <h2>Conta OLX ligada com sucesso.</h2>
    <p>O bot pode agora consultar e responder às mensagens.</p>
    """


@app.route("/olx/processar")
def olx_processar():
    # Rota útil para teste e também para um monitor externo chamar periodicamente.
    return jsonify(process_new_messages(force_full=True))


def _mask(value):
    value = str(value or "")
    if "@" in value:
        name, domain = value.split("@", 1)
        return (name[:2] + "***@" + domain) if name else "***@" + domain
    return (value[:2] + "***") if value else ""


def _probe(path, access_token, refresh_token, **kwargs):
    """GET só de leitura. Devolve (resumo, payload, access_token, refresh_token)."""
    response, access_token, refresh_token = olx_request(
        "GET", path, access_token, refresh_token, **kwargs
    )
    info = {"http": response.status_code}
    payload = None
    try:
        payload = response.json()
    except Exception:
        info["corpo_nao_json"] = response.text[:200]
    if isinstance(payload, dict):
        info["formato"] = "dict com chaves: " + ", ".join(sorted(payload.keys()))
    elif isinstance(payload, list):
        info["formato"] = "lista"
    if not response.ok:
        info["erro"] = response.text[:300]
    return info, payload, access_token, refresh_token


@app.route("/olx/diagnostico")
def olx_diagnostico():
    """
    Diagnóstico só de leitura (não envia mensagens, não escreve na BD).
    Mostra a que conta pertence o token e exatamente o que o OLX devolve.
    Remover/proteger depois de resolvido.
    """
    out = {}
    try:
        rows = (
            supabase.table("olx_tokens")
            .select("created_at")
            .order("created_at", desc=True)
            .limit(5)
            .execute()
        )
        out["tokens_guardados_ultimos_5"] = [r.get("created_at") for r in rows.data]

        access_token, refresh_token = get_latest_tokens()
        if not access_token:
            return jsonify({"erro": "Não existe nenhum token OLX guardado."})

        # 1) A que conta pertence o token?
        info, payload, access_token, refresh_token = _probe(
            "/users/me", access_token, refresh_token
        )
        me = payload.get("data", payload) if isinstance(payload, dict) else {}
        info["id"] = me.get("id")
        info["nome"] = me.get("name")
        info["email_mascarado"] = _mask(me.get("email"))
        out["conta_do_token"] = info

        # 2) Anúncios visíveis para este token
        info, payload, access_token, refresh_token = _probe(
            "/adverts", access_token, refresh_token, params={"offset": 0, "limit": 100}
        )
        adverts = api_items_from(payload)
        info["total_devolvido"] = len(adverts)
        info["amostra"] = [
            {"id": a.get("id"), "titulo": (a.get("title") or "")[:40], "estado": a.get("status")}
            for a in adverts[:5]
        ]
        out["anuncios"] = info

        detalhes = []
        for a in adverts[:5]:
            aid = a.get("id")
            info_a, payload_a, access_token, refresh_token = _probe(
                f"/adverts/{aid}", access_token, refresh_token
            )
            adv = payload_a.get("data", payload_a) if isinstance(payload_a, dict) else {}
            fonte = "detalhe"
            if not isinstance(adv, dict) or not adv:
                adv, fonte = a, "lista"
            facts = extract_facts(adv)
            sentences = {t: _fact_sentence(t, facts, adv) for t in DATA_TOPICS}
            detalhes.append(
                {
                    "advert_id": aid,
                    "http_detalhe": info_a["http"],
                    "fonte_usada": fonte,
                    "campos": sorted(adv.keys()) if isinstance(adv, dict) else [],
                    "price_bruto": adv.get("price") if isinstance(adv, dict) else None,
                    "preco_que_o_bot_usa": extract_price(adv),
                    "km_que_o_bot_usa": sentences.get("km"),
                    "respostas_por_tema": sentences,
                    "factos": [
                        {"nome": f["ident"], "valor": f["value"][:60], "origem": f["source"]}
                        for f in facts[:80]
                    ],
                }
            )
        out["anuncios_detalhe_para_respostas"] = detalhes

        # 3) /threads em 3 variantes, para detetar filtragem no servidor
        variantes = {
            "sem_parametros": None,
            "offset0_limit100": {"offset": 0, "limit": 100},
            "offset0_limit10": {"offset": 0, "limit": 10},
        }
        threads_ref = []
        out["threads"] = {}
        for nome, params in variantes.items():
            kw = {"params": params} if params else {}
            info, payload, access_token, refresh_token = _probe(
                "/threads", access_token, refresh_token, **kw
            )
            lst = api_items_from(payload)
            info["total_devolvido"] = len(lst)
            info["ids"] = [item_key(t) for t in lst][:30]
            info["nao_lidas"] = sum(int(t.get("unread_count") or 0) for t in lst)
            out["threads"][nome] = info
            if nome == "offset0_limit100":
                threads_ref = lst

        # 4) Estrutura real de um thread + última mensagem de cada um (sem texto)
        if threads_ref:
            out["campos_de_um_thread"] = sorted(threads_ref[0].keys())
        detalhe = []
        for t in threads_ref[:10]:
            tid = item_key(t)
            info, payload, access_token, refresh_token = _probe(
                f"/threads/{tid}/messages", access_token, refresh_token,
                params={"offset": 0, "limit": 100},
            )
            msgs = api_items_from(payload)
            datas = sorted(str(m.get("created_at")) for m in msgs if m.get("created_at"))
            detalhe.append(
                {
                    "thread": tid,
                    "advert_id": t.get("advert_id"),
                    "interlocutor_id": t.get("interlocutor_id"),
                    "criado_em": t.get("created_at"),
                    "total_count": t.get("total_count"),
                    "unread_count": t.get("unread_count"),
                    "http_mensagens": info["http"],
                    "mensagens_devolvidas": len(msgs),
                    "recebidas": sum(1 for m in msgs if m.get("type") == "received"),
                    "mais_antiga": datas[0] if datas else None,
                    "mais_recente": datas[-1] if datas else None,
                }
            )
        out["detalhe_threads"] = detalhe
        return jsonify(out)
    except Exception as exc:
        out["erro"] = str(exc)
        return jsonify(out), 500


@app.route("/olx/estado")
def olx_estado():
    access_token, refresh_token = get_latest_tokens()
    info = {
        "servidor": "online",
        "conta_olx_ligada": bool(access_token),
        "historico_inicial_ignorado": False,
        "auto_poll": AUTO_POLL,
        "intervalo_segundos": POLL_SECONDS,
    }
    if access_token:
        try:
            account_id, access_token, refresh_token = get_account_id(access_token, refresh_token)
            baseline = get_baseline(account_id)
            info["historico_inicial_ignorado"] = baseline is not None
            info["marco_created_at"] = baseline["cutoff_text"] if baseline else None
            info["ultimo_ciclo"] = dict(_LAST_CYCLE) or None
            info["conversas_quentes"] = sum(
                1 for v in _THREAD_ACTIVITY.values()
                if v >= datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=HOT_THREAD_HOURS)
            )
        except Exception as exc:
            info["erro"] = str(exc)
    return jsonify(info)


# Inicia o ciclo automático apenas uma vez por processo.
if AUTO_POLL:
    threading.Thread(target=polling_loop, daemon=True, name="olx-poller").start()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
        use_reloader=False,
    )
