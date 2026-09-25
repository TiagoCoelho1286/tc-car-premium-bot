import os
import secrets
import requests
from supabase import create_client
from flask import Flask, request, redirect, session
from datetime import datetime
from zoneinfo import ZoneInfo
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
        .select("access_token, refresh_token")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if not token_data.data:
        return "Não existe nenhum token OLX guardado.", 500

    access_token = token_data.data[0]["access_token"]
    refresh_token = token_data.data[0]["refresh_token"]

    novas_response = requests.get(
        "https://www.olx.pt/api/partner/threads",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Version": "2.0",
        },
        timeout=20,
    )

    if novas_response.status_code == 401:
        access_token = refresh_olx_token(refresh_token)

        if not access_token:
            return "Não foi possível renovar o token OLX.", 500

        novas_response = requests.get(
            "https://www.olx.pt/api/partner/threads",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Version": "2.0",
            },
            timeout=20,
        )

    if not novas_response.ok:
        return {
            "erro": "Erro ao obter conversas do OLX",
            "codigo_olx": novas_response.status_code
        }, 500
return {
    "status_code": novas_response.status_code,
    "resposta_completa_olx": novas_response.json()
}
    threads = novas_response.json().get("data", [])
    mensagens_encontradas = []

    for thread in threads[:5]:
       
        thread_uuid = thread.get("uuid")
        advert_id = thread.get("advert_id")

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

            for message in messages:
               if message.get("type") == "received":
                    mensagens_encontradas.append({
                        "message_id": message.get("id"),
                        "thread_uuid": thread_uuid,
                        "advert_id": advert_id,
                        "texto": message.get("text"),
                    })
            hora = datetime.now(ZoneInfo("Europe/Lisbon")).hour

    if hora < 12:
        saudacao = "Bom dia."
    elif hora < 20:
        saudacao = "Boa tarde."
    else:
        saudacao = "Boa noite."

    for mensagem in mensagens_encontradas:
        texto = (mensagem.get("texto") or "").lower()

        # DISPONIBILIDADE
        if any(p in texto for p in [
            "disponível", "disponivel", "ainda está disponível",
            "ainda esta disponivel", "ainda tem o carro",
            "ainda tem a viatura", "já vendeu", "ja vendeu"
        ]):
            resposta = (
                f"{saudacao} Sim, a viatura continua disponível. "
                "Onde podemos ajudar?"
            )

        # PREÇO NEGOCIÁVEL
        elif any(p in texto for p in [
            "negociável", "negociavel", "preço negociável",
            "preco negociavel", "faz desconto", "baixa o preço",
            "baixa o preco", "melhor preço", "melhor preco",
            "mínimo", "minimo", "último preço", "ultimo preco"
        ]):
            resposta = (
                f"{saudacao} Existe alguma margem para negociação, "
                "mas preferimos falar sobre valores depois de ver a viatura. "
                "Onde podemos ajudar?"
            )

        # RETOMA
        elif any(p in texto for p in [
            "retoma", "aceitam retoma", "aceita retoma",
            "troca", "dar o meu carro", "dar a minha viatura"
        ]):
            resposta = (
                f"{saudacao} Sim, podemos avaliar uma possível retoma. "
                "Envie-nos, por favor, algumas fotografias da viatura, "
                "marca, modelo, ano, quilometragem e motorização para o "
                "WhatsApp 962 148 367 e fazemos uma avaliação."
            )

        # FINANCIAMENTO
        elif any(p in texto for p in [
            "financiamento", "financiam", "financiar",
            "crédito", "credito", "prestações", "prestacoes",
            "mensalidade"
        ]):
            resposta = (
                f"{saudacao} De momento estamos a atualizar as nossas "
                "soluções de financiamento, pelo que temporariamente não "
                "estamos a realizar novos processos. Prevemos voltar a "
                "disponibilizar esta opção em breve."
            )

        # LOCALIZAÇÃO / ONDE VER A VIATURA
        elif any(p in texto for p in [
            "onde estão", "onde estao", "onde fica",
            "localização", "localizacao", "morada",
            "onde posso ver", "onde ver", "onde têm os carros",
            "onde tem os carros"
        ]):
            resposta = (
                f"{saudacao} Pode ver a viatura mediante marcação na "
                "Ruela da Cavada Nova, n.º 74, 4585-053 Baltar, Paredes. "
                "Se pretender, podemos combinar um dia e horário."
            )

        # GARANTIA
        elif any(p in texto for p in [
            "garantia", "tem garantia", "quanto tempo de garantia",
            "quantos meses de garantia"
        ]):
            resposta = (
                f"{saudacao} As condições de garantia dependem da viatura "
                "e das condições da venda. Quando aplicável, trabalhamos "
                "com garantia até 18 meses. Podemos confirmar as condições "
                "específicas desta viatura."
            )

        # MARCAÇÃO / VISITA / TEST-DRIVE
        elif any(p in texto for p in [
            "posso ir ver", "quero ver", "marcar",
            "marcação", "marcacao", "visitar",
            "visita", "test drive", "experimentar",
            "posso experimentar"
        ]):
            resposta = (
                f"{saudacao} Claro. Podemos combinar uma visita para ver "
                "a viatura e esclarecer todas as questões. "
                "Indique-nos, por favor, o dia e horário que lhe dão mais jeito."
            )

        # MAIS FOTOS / VÍDEO
        elif any(p in texto for p in [
            "mais fotos", "mais fotografias", "fotos",
            "fotografias", "vídeo", "video"
        ]):
            resposta = (
                f"{saudacao} Claro. Podemos enviar mais fotografias ou vídeos "
                "da viatura. Diga-nos que detalhes pretende ver ou contacte-nos "
                "pelo WhatsApp 962 148 367."
            )

        # QUILÓMETROS
        elif any(p in texto for p in [
            "quilómetros", "quilometros", "km",
            "quantos kms", "quantos km"
        ]):
            resposta = (
                f"{saudacao} A quilometragem encontra-se indicada no anúncio. "
                "Se tiver alguma questão específica sobre o histórico da viatura, "
                "podemos esclarecer."
            )

        # HISTÓRICO / MANUTENÇÃO
        elif any(p in texto for p in [
            "histórico", "historico", "revisões", "revisoes",
            "manutenção", "manutencao", "livro de revisões",
            "livro de revisoes"
        ]):
            resposta = (
                f"{saudacao} Podemos esclarecer toda a informação disponível "
                "sobre o histórico e manutenção desta viatura. "
                "Diga-nos concretamente o que pretende saber."
            )

        # CONTACTO / TELEFONE / WHATSAPP
        elif any(p in texto for p in [
            "contacto", "telefone", "telemóvel", "telemovel",
            "whatsapp", "número", "numero"
        ]):
            resposta = (
                f"{saudacao} Pode contactar-nos através do WhatsApp "
                "pelo número 962 148 367. Onde podemos ajudar?"
            )

        # RESPOSTA GENÉRICA
        else:
            resposta = (
                f"{saudacao} Obrigado pelo seu contacto com a TC Car Premium. "
                "Onde podemos ajudar?"
            )

        mensagem["resposta"] = resposta

    return {
        "estado": "mensagens verificadas",
        "threads_encontradas": len(threads),
        "mensagens_encontradas": len(mensagens_encontradas),
        "mensagens": mensagens_encontradas
    }
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
