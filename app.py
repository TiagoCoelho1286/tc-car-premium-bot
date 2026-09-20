import os
import secrets
import requests
from supabase import create_client
from flask import Flask, request, redirect, session

app = Flask(__name__)

# Chave usada apenas para proteger a sessão do browser.
# Para já é gerada quando o servidor arranca.
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))

OLX_CLIENT_ID = os.environ.get("OLX_CLIENT_ID")
OLX_CLIENT_SECRET = os.environ.get("OLX_CLIENT_SECRET")
OLX_REDIRECT_URI = os.environ.get("OLX_REDIRECT_URI")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)
OLX_AUTHORIZE_URL = "https://www.olx.pt/oauth/authorize/"
OLX_TOKEN_URL = "https://www.olx.pt/api/open/oauth/token"

def refresh_olx_token(refresh_token):
    response = requests.post(
        OLX_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": OLX_CLIENT_ID,
            "client_secret": OLX_CLIENT_SECRET,
            "refresh_token": refresh_token,
        },
        timeout=20,
    )

    if not response.ok:
        return None

    tokens = response.json()

    new_access_token = tokens.get("access_token")
    new_refresh_token = tokens.get("refresh_token", refresh_token)

    if not new_access_token:
        return None

    supabase.table("olx_tokens").insert({
        "access_token": new_access_token,
        "refresh_token": new_refresh_token
    }).execute()

    return new_access_token
@app.route("/")
def home():
    return """
    <h1>TC Car Premium Bot</h1>
    <p>Servidor online.</p>
    <a href="/olx/login">Ligar conta OLX</a>
    """


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

    from urllib.parse import urlencode

    authorization_url = (
        OLX_AUTHORIZE_URL
        + "?"
        + urlencode(params)
    )

    return redirect(authorization_url)


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
    supabase.table("olx_tokens").insert({
    "access_token": access_token,
    "refresh_token": refresh_token
    }).execute()
    # NÃO mostramos os tokens no browser.
    # Nesta primeira fase apenas confirmamos que a autenticação funcionou.

    if tokens.get("access_token"):
        return """
        <h1>TC Car Premium Bot</h1>
        <h2>Conta OLX ligada com sucesso! ✅</h2>
        <p>A autenticação com a API do OLX funcionou.</p>
        """

    return "O OLX não devolveu um access token.", 500

@app.route("/olx/mensagens")
def olx_mensagens():
    token_data = (
        supabase.table("olx_tokens")
        .select("access_token")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if not token_data.data:
        return "Não existe nenhum token OLX guardado.", 500

    access_token = token_data.data[0]["access_token"]

    response = requests.get(
        "https://www.olx.pt/api/partner/threads",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Version": "2.0",
        },
        timeout=20,
    )

    return response.text, response.status_code
@app.route("/olx/conversa/<thread_uuid>")
def olx_conversa(thread_uuid):
    token_data = (
        supabase.table("olx_tokens")
        .select("access_token")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if not token_data.data:
        return "Não existe nenhum token OLX guardado.", 500

    access_token = token_data.data[0]["access_token"]

    response = requests.get(
        f"https://www.olx.pt/api/partner/threads/{thread_uuid}/messages",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Version": "2.0",
        },
        timeout=20,
    )

    return response.text, response.status_code
@app.route("/olx/responder/<thread_uuid>")
def olx_responder(thread_uuid):
    token_data = (
        supabase.table("olx_tokens")
        .select("access_token")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if not token_data.data:
        return "Não existe nenhum token OLX guardado.", 500

    access_token = token_data.data[0]["access_token"]

    response = requests.post(
        f"https://www.olx.pt/api/partner/threads/{thread_uuid}/messages",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Version": "2.0",
            "Content-Type": "application/json",
        },
        json={
            "text": "Olá! Esta é uma mensagem de teste do TC Car Premium Bot."
        },
        timeout=20,
    )

    return response.text, response.status_code
@app.route("/olx/novas")
def olx_novas():
    token_data = (
        supabase.table("olx_tokens")
        .select("access_token")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if not token_data.data:
        return "Não existe nenhum token OLX guardado.", 500

    access_token = token_data.data[0]["access_token"]

    response = requests.get(
        "https://www.olx.pt/api/partner/threads",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Version": "2.0",
        },
        timeout=20,
    )

    if not response.ok:
        return response.text, response.status_code

    threads = response.json().get("data", [])

    novas = []

    for thread in threads[:5]:
        if thread.get("unread_count", 0) > 0:
            thread_uuid = thread.get("uuid")

            messages_response = requests.get(
                f"https://www.olx.pt/api/partner/threads/{thread_uuid}/messages",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Version": "2.0",
                },
                timeout=20,
            )

            if messages_response.ok:
                messages = messages_response.json().get("data", [])

                recebidas = [
                    message for message in messages
                    if message.get("type") == "received"
                    and not message.get("is_read", False)
                ]

                for message in recebidas:
                    novas.append({
                        "thread_uuid": thread_uuid,
                        "advert_id": thread.get("advert_id"),
                        "mensagem": message.get("text"),
                        "data": message.get("created_at"),
                    })

    return {"total": len(novas), "mensagens": novas}

@app.route("/olx/teste-auto")
def olx_teste_auto():
    token_data = (
        supabase.table("olx_tokens")
        .select("access_token")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if not token_data.data:
        return "Não existe nenhum token OLX guardado.", 500

    access_token = token_data.data[0]["access_token"]

    novas_response = requests.get(
        "https://www.olx.pt/api/partner/threads",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Version": "2.0",
        },
        timeout=20,
    )

    return {
        "estado": "teste automático preparado"
    }
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
