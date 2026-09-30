import os
import secrets
import threading
import time
from datetime import datetime
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

POLL_SECONDS = max(30, int(os.environ.get("POLL_SECONDS", "60")))
AUTO_POLL = os.environ.get("AUTO_POLL", "true").lower() in {"1", "true", "yes", "sim"}

INITIALIZED_SENTINEL = "__TC_CAR_PREMIUM_BOT_INITIALIZED__"
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


def build_reply(text):
    texto = (text or "").lower()
    saudacao = greeting()

    if any(p in texto for p in [
        "disponível", "disponivel", "ainda está disponível",
        "ainda esta disponivel", "ainda tem o carro",
        "ainda tem a viatura", "já vendeu", "ja vendeu",
    ]):
        return f"{saudacao} Sim, a viatura continua disponível. Onde podemos ajudar?"

    if any(p in texto for p in [
        "negociável", "negociavel", "preço negociável", "preco negociavel",
        "faz desconto", "baixa o preço", "baixa o preco", "melhor preço",
        "melhor preco", "mínimo", "minimo", "último preço", "ultimo preco",
    ]):
        return (
            f"{saudacao} Existe alguma margem para negociação, "
            "mas preferimos falar sobre valores depois de ver a viatura. "
            "Onde podemos ajudar?"
        )

    if any(p in texto for p in [
        "retoma", "aceitam retoma", "aceita retoma", "troca",
        "dar o meu carro", "dar a minha viatura",
    ]):
        return (
            f"{saudacao} Sim, podemos avaliar uma possível retoma. "
            "Envie-nos, por favor, algumas fotografias da viatura, marca, modelo, "
            "ano, quilometragem e motorização para o WhatsApp 962 148 367 "
            "e fazemos uma avaliação."
        )

    if any(p in texto for p in [
        "financiamento", "financiam", "financiar", "crédito", "credito",
        "prestações", "prestacoes", "mensalidade",
    ]):
        return (
            f"{saudacao} De momento estamos a atualizar as nossas soluções de "
            "financiamento, pelo que temporariamente não estamos a realizar novos "
            "processos. Prevemos voltar a disponibilizar esta opção em breve."
        )

    if any(p in texto for p in [
        "onde estão", "onde estao", "onde fica", "localização", "localizacao",
        "morada", "onde posso ver", "onde ver", "onde têm os carros",
        "onde tem os carros",
    ]):
        return (
            f"{saudacao} Pode ver a viatura mediante marcação na Ruela da Cavada "
            "Nova, n.º 74, 4585-053 Baltar, Paredes. Se pretender, podemos combinar "
            "um dia e horário."
        )

    if any(p in texto for p in [
        "garantia", "tem garantia", "quanto tempo de garantia",
        "quantos meses de garantia",
    ]):
        return (
            f"{saudacao} As condições de garantia dependem da viatura e das "
            "condições da venda. Quando aplicável, trabalhamos com garantia até "
            "18 meses. Podemos confirmar as condições específicas desta viatura."
        )

    if any(p in texto for p in [
        "posso ir ver", "quero ver", "marcar", "marcação", "marcacao",
        "visitar", "visita", "test drive", "experimentar", "posso experimentar",
    ]):
        return (
            f"{saudacao} Claro. Podemos combinar uma visita para ver a viatura "
            "e esclarecer todas as questões. Indique-nos, por favor, o dia e "
            "horário que lhe dão mais jeito."
        )

    if any(p in texto for p in [
        "mais fotos", "mais fotografias", "fotos", "fotografias", "vídeo", "video",
    ]):
        return (
            f"{saudacao} Claro. Podemos enviar mais fotografias ou vídeos da "
            "viatura. Diga-nos que detalhes pretende ver ou contacte-nos pelo "
            "WhatsApp 962 148 367."
        )

    if any(p in texto for p in [
        "quilómetros", "quilometros", "km", "quantos kms", "quantos km",
    ]):
        return (
            f"{saudacao} A quilometragem encontra-se indicada no anúncio. "
            "Se tiver alguma questão específica sobre o histórico da viatura, "
            "podemos esclarecer."
        )

    if any(p in texto for p in [
        "histórico", "historico", "revisões", "revisoes", "manutenção",
        "manutencao", "livro de revisões", "livro de revisoes",
    ]):
        return (
            f"{saudacao} Podemos esclarecer toda a informação disponível sobre "
            "o histórico e manutenção desta viatura. Diga-nos concretamente o "
            "que pretende saber."
        )

    if any(p in texto for p in [
        "contacto", "telefone", "telemóvel", "telemovel", "whatsapp",
        "número", "numero",
    ]):
        return (
            f"{saudacao} Pode contactar-nos através do WhatsApp pelo número "
            "962 148 367. Onde podemos ajudar?"
        )

    return (
        f"{saudacao} Obrigado pelo seu contacto com a TC Car Premium. "
        "Onde podemos ajudar?"
    )


def get_threads(access_token, refresh_token):
    # Paginação para não ficarmos limitados apenas às primeiras conversas.
    all_threads = []
    offset = 0
    limit = 100

    while True:
        response, access_token, refresh_token = olx_request(
            "GET",
            f"/threads?offset={offset}&limit={limit}",
            access_token,
            refresh_token,
        )
        if not response.ok:
            raise RuntimeError(
                f"Erro OLX ao obter conversas: HTTP {response.status_code} - {response.text[:300]}"
            )

        batch = api_items(response)
        all_threads.extend(batch)

        if len(batch) < limit:
            break
        offset += limit

    return all_threads, access_token, refresh_token


def get_messages(thread_uuid, access_token, refresh_token):
    response, access_token, refresh_token = olx_request(
        "GET",
        f"/threads/{thread_uuid}/messages",
        access_token,
        refresh_token,
    )
    if not response.ok:
        raise RuntimeError(
            f"Erro OLX ao obter mensagens: HTTP {response.status_code} - {response.text[:300]}"
        )
        print("DEBUG OLX MESSAGES:", response.status_code, response.text[:5000], flush=True)
    return api_items(response), access_token, refresh_token


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


def initialize_without_replying(access_token, refresh_token):
    """
    Primeira execução:
    regista TODAS as mensagens recebidas que já existem como processadas.
    Assim as mais de 100 mensagens antigas nunca recebem resposta automática.
    """
    threads, access_token, refresh_token = get_threads(access_token, refresh_token)
    marked = 0

    for thread in threads:
        thread_uuid = thread.get("uuid") or thread.get("id")
        advert_id = thread.get("advert_id")
        if not thread_uuid:
            continue

        messages, access_token, refresh_token = get_messages(
            thread_uuid, access_token, refresh_token
        )

        for message in messages:
            if message.get("type") != "received":
                continue
            mid = message_key(message)
            if mid and not is_processed(mid):
                mark_processed(mid, thread_uuid, advert_id)
                marked += 1

    mark_processed(INITIALIZED_SENTINEL, "SYSTEM", "SYSTEM")

    return {
        "estado": "inicializado",
        "mensagens_antigas_ignoradas": marked,
        "threads_verificadas": len(threads),
    }


def process_new_messages():
    if not process_lock.acquire(blocking=False):
        return {"estado": "já existe um processamento em curso"}

    try:
        access_token, refresh_token = get_latest_tokens()
        if not access_token:
            return {"erro": "Não existe nenhum token OLX guardado."}

        # Segurança principal: na primeira execução nunca responde ao histórico.
        if not is_processed(INITIALIZED_SENTINEL):
            return initialize_without_replying(access_token, refresh_token)

        threads, access_token, refresh_token = get_threads(access_token, refresh_token)
        replies_sent = 0
        messages_marked = 0
        errors = []

        for thread in threads:
            thread_uuid = thread.get("uuid") or thread.get("id")
            advert_id = thread.get("advert_id")

            if not thread_uuid:
                continue

            try:
                messages, access_token, refresh_token = get_messages(
                    thread_uuid, access_token, refresh_token
                )
            except Exception as exc:
                errors.append(f"{thread_uuid}: {exc}")
                continue

            pending = []
            for message in messages:
                if message.get("type") != "received":
                    continue
                mid = message_key(message)
                if mid and not is_processed(mid):
                    pending.append(message)

            if not pending:
                continue

            # Se o cliente enviar várias mensagens seguidas, damos UMA resposta,
            # usando o conjunto das mensagens, em vez de bombardear o cliente.
            combined_text = " ".join((m.get("text") or "") for m in pending).strip()
            reply = build_reply(combined_text)

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
                continue

            # Só marcamos como processadas DEPOIS de o OLX confirmar o envio.
            for message in pending:
                mid = message_key(message)
                mark_processed(mid, thread_uuid, advert_id)
                messages_marked += 1

            replies_sent += 1

            # Não é crítico para o envio; apenas tentamos limpar o não-lido.
            try:
                mark_thread_read(thread_uuid, access_token, refresh_token)
            except Exception:
                pass

        return {
            "estado": "ok",
            "threads_verificadas": len(threads),
            "respostas_enviadas": replies_sent,
            "mensagens_processadas": messages_marked,
            "erros": errors,
        }

    except Exception as exc:
        return {"erro": str(exc)}
    finally:
        process_lock.release()


def polling_loop():
    # Pequeno atraso para o servidor arrancar antes da primeira consulta.
    time.sleep(10)
    while True:
        try:
            process_new_messages()
        except Exception as exc:
            print(f"[BOT] Erro no ciclo automático: {exc}", flush=True)
        time.sleep(POLL_SECONDS)


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
    return jsonify(process_new_messages())


@app.route("/olx/estado")
def olx_estado():
    initialized = is_processed(INITIALIZED_SENTINEL)
    access_token, _ = get_latest_tokens()
    return jsonify(
        {
            "servidor": "online",
            "conta_olx_ligada": bool(access_token),
            "historico_inicial_ignorado": initialized,
            "auto_poll": AUTO_POLL,
            "intervalo_segundos": POLL_SECONDS,
        }
    )


# Inicia o ciclo automático apenas uma vez por processo.
if AUTO_POLL:
    threading.Thread(target=polling_loop, daemon=True, name="olx-poller").start()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
        use_reloader=False,
    )
