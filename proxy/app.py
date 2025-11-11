from flask import Flask, request, session, redirect, url_for, render_template, Response
import requests
# (urllib.parse から parse_qsl, urlencode をインポート)
from urllib.parse import urljoin, quote, parse_qsl, urlencode
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
            
            # (next_url が "/" の場合、DEFAULT_REDIRECT を使う)
            if next_url and next_url != "/login" and next_url != "/":
                return redirect(next_url)
            else:
                return redirect(DEFAULT_REDIRECT)
        else:
            return render_template("login.html", error="Wrong username or password.")
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
        # (未ログイン時のリダイレクト処理)
        current_url = request.full_path.rstrip('?')
        if current_url and current_url != '/login':
            return redirect(url_for("login", next=quote(current_url, safe='/?&=')))
        else:
            return redirect(url_for("login"))

    # ★ 修正点: 
    # ログイン済みで、アクセスパスがルート("/") かつ クエリ文字列がない場合
    # (例: http://localhost:8080/ のリクエスト)
    # Grafanaにプロキシせず、直接DEFAULT_REDIRECTにリダイレクトする
    if not path and not request.query_string:
        return redirect(DEFAULT_REDIRECT)

    # (ここから &kiosk 強制付与ロジック - 前回から変更なし)
    upstream = urljoin(GRAFANA_URL.rstrip("/") + "/", path)
    
    # 1. 元のクエリ文字列をデコード
    original_query = request.query_string.decode()
    
    # 2. クエリを (key, value) のタプルのリストに分解
    query_tuples = parse_qsl(original_query)
    
    # 3. 'kiosk' キーが既に存在するかチェック
    kiosk_present = any(key == 'kiosk' for key, value in query_tuples)
    
    final_query_tuples = query_tuples
    
    # 4. kiosk が存在しない場合、('kiosk', '') を追加
    if not kiosk_present:
        # ('kiosk', '') は urlencode によって 'kiosk=' に変換されます
        final_query_tuples.append(('kiosk', '')) 
        
    # 5. クエリ文字列を再構築
    final_query_string = urlencode(final_query_tuples)

    if final_query_string:
        upstream += "?" + final_query_string
    # (kioskロジック ここまで)

    # ★ 重要な修正: ヘッダーの処理
    headers = {}
    for k, v in request.headers:
        key_lower = k.lower()
        # Host, Content-Length は除外しない(Hostは特に重要)
        if key_lower not in ("content-length",):
            headers[k] = v
    
    # ★ 認証済みユーザーをGrafanaに渡す
    headers[PROXY_HEADER] = username
    
    # ★ プロキシ経由でアクセスされていることをGrafanaに伝える
    # X-Forwarded-* ヘッダーを設定
    headers['X-Forwarded-For'] = request.remote_addr
    headers['X-Forwarded-Proto'] = request.scheme
    headers['X-Forwarded-Host'] = request.host
    
    # ★ オリジナルのホスト情報を保持
    if 'X-Real-IP' not in headers:
        headers['X-Real-IP'] = request.remote_addr

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
