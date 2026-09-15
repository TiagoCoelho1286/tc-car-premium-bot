from flask import Flask, request

app = Flask(__name__)


@app.route("/")
def home():
    return "TC Car Premium Bot está online!"


@app.route("/olx/callback")
def olx_callback():
    code = request.args.get("code")

    if code:
        return "Autorização OLX recebida com sucesso."

    return "Callback OLX está a funcionar."


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
