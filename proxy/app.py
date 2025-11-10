from flask import Flask, request, session, redirect, url_for, render_template, Response
import requests
from urllib.parse import urljoin, quote
import os

app = Flask(__name__, template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", "change-me")

# 固定ユーザー認証情報
VALID_USER = "adminuser"
VALID_PASS = "adminpass"

# Grafana接続設定
GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://grafana:3000")
PROXY_HEADER = os.environ.get("PROXY_HEADER", "X-WEBAUTH-USER")

# ログイン後のリダイレクト先
DEFAULT_REDIRECT = "/d/my-ap-dashboard/extreme-ap-dashboard?orgId=1&refresh=5m&kiosk&theme=light"

# --- 認証関数 ---
def verify_user(username: str, password: str) -> bool:
    return username == VALID_USER and password == VALID_PASS

# --- ログイン画面 ---
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if verify_user(username, password):
            session["username"] = username
            # ログイン前にアクセスしようとしていたURLがあればそこへ、なければデフォルトのダッシュボードへ
            next_url = request.args.get("next")
            
            # ★ 修正点: next_url が "/" の場合、DEFAULT_REDIRECT を使うようにする
            if next_url and next_url != "/login" and next_url != "/":
                return redirect(next_url)
            else:
                return redirect(DEFAULT_REDIRECT)
        else:
            return render_template("login.html", error="ユーザー名またはパスワードが違います。")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")

# --- Grafanaへ中継 ---
def make_proxy_response(grafana_resp: requests.Response):
    headers = [(name, value) for name, value in grafana_resp.raw.headers.items()
               if name.lower() not in ('transfer-encoding', 'content-encoding', 'content-length', 'connection')]
    def generate():
        for chunk in grafana_resp.iter_content(chunk_size=8192):
            if chunk:
                yield chunk
    return Response(generate(), status=grafana_resp.status_code, headers=headers)

@app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
@app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def proxy(path):
    username = session.get("username")
    if not username:
        # 現在のURLをnextパラメータとして保存（ログインページ以外）
        current_url = request.full_path.rstrip('?')
        if current_url and current_url != '/login':
            return redirect(url_for("login", next=quote(current_url, safe='/?&=')))
        else:
            return redirect(url_for("login"))

    upstream = urljoin(GRAFANA_URL.rstrip("/") + "/", path)
    if request.query_string:
        upstream += "?" + request.query_string.decode()

    headers = {k: v for k, v in request.headers if k.lower() not in ("host", "content-length")}
    headers[PROXY_HEADER] = username  # 認証済みユーザーをGrafanaに渡す

    data = request.get_data() or None
    try:
        upstream_resp = requests.request(
            method=request.method,
            url=upstream,
            headers=headers,
            data=data,
            stream=True,
            allow_redirects=False,
            timeout=30
        )
    except requests.RequestException as e:
        return Response(f"Upstream request failed: {e}", status=502)

    return make_proxy_response(upstream_resp)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
