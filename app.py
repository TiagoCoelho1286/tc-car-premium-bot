import os
import secrets
import requests

from flask import Flask, request, redirect, session

app = Flask(__name__)

# Chave usada apenas para proteger a sessão do browser.
# Para já é gerada quando o servidor arranca.
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))

OLX_CLIENT_ID = os.environ.get("OLX_CLIENT_ID")
OLX_CLIENT_SECRET = os.environ.get("OLX_CLIENT_SECRET")
OLX_REDIRECT_URI = os.environ.get("OLX_REDIRECT_URI")

OLX_AUTHORIZE_URL = "https://www.olx.pt/oauth/authorize/"
OLX_TOKEN_URL = "https://www.olx.pt/api/open/oauth/token"


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

    # NÃO mostramos os tokens no browser.
    # Nesta primeira fase apenas confirmamos que a autenticação funcionou.

    if tokens.get("access_token"):
        return """
        <h1>TC Car Premium Bot</h1>
        <h2>Conta OLX ligada com sucesso! ✅</h2>
        <p>A autenticação com a API do OLX funcionou.</p>
        """

    return "O OLX não devolveu um access token.", 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
