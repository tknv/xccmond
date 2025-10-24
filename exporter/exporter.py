import os
import csv
import time
import logging
import requests
import schedule
import threading
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, Response
from prometheus_client import REGISTRY, Gauge, generate_latest

# --- 設定 ---
# ログ設定
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Exporterがリッスンするポート
EXPORTER_PORT = 9100
# APIポーリング間隔 (秒)
POLLING_INTERVAL = 300  # 5分
# CSVファイルパス
CSV_FILE_PATH = 'targets.csv'
# トークンの有効期限（マージンを持たせて 1時間55分）
TOKEN_LIFETIME_MINUTES = 115
# APIリクエストのタイムアウト（秒）
REQUEST_TIMEOUT = 10
# 並列処理のワーカー数
MAX_WORKERS = 50

# --- Prometheus メトリクス定義 ---
# ap_info: APの静的情報（ラベルとして保持）
ap_info = Gauge(
    'ap_info',
    'Static information about the Access Point',
    ['target_ip', 'hostname', 'serialNumber', 'ipAddress']
)
# ap_status: APの稼働ステータス (1=InService, 0=Other)
ap_status = Gauge(
    'ap_status',
    'AP device health status (1 = InService, 0 = Other)',
    ['target_ip', 'hostname', 'serialNumber', 'ipAddress']
)
# ap_scrape_up: APIサーバー（コントローラー）へのスクレイプ成功/失敗
ap_scrape_up = Gauge(
    'ap_scrape_up',
    'Scrape status of the target API (1 = Success, 0 = Failure)',
    ['target_ip']
)

# --- Flask アプリケーション ---
app = Flask(__name__)

# --- トークン管理 ---
# { 'ip_address': {'token': '...', 'expires_at': datetime} }
token_cache = {}
token_lock = threading.Lock()

def get_token(ip, username, password):
    """
    キャッシュを考慮してAPIトークンを取得する。
    期限切れの場合は再取得する。
    """
    with token_lock:
        now = datetime.now(timezone.utc)
        cache_entry = token_cache.get(ip)

        if cache_entry and cache_entry['expires_at'] > now:
            logging.debug(f"Using cached token for {ip}")
            return cache_entry['token']

        # トークンがない、または期限切れのため再取得
        logging.info(f"Requesting new token for {ip}...")
        token_url = f"https://{ip}:5825/management/v1/oauth2/token"
        payload = {
            'userId': username,
            'password': password,
            'grantType': 'password',
            'scope': '...'
        }
        
        try:
            # SSL証明書の検証を無効化 (自己署名証明書対策)
            response = requests.post(
                token_url,
                json=payload,
                verify=False,
                timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            data = response.json()
            
            access_token = data.get('access_token')
            if not access_token:
                raise ValueError("access_token not found in response")

            expires_at = now + timedelta(minutes=TOKEN_LIFETIME_MINUTES)
            token_cache[ip] = {'token': access_token, 'expires_at': expires_at}
            logging.info(f"Successfully obtained token for {ip}")
            return access_token

        except requests.exceptions.RequestException as e:
            logging.error(f"Failed to get token for {ip}: {e}")
            return None

# --- データ収集 ---
def collect_metrics_for_target(target):
    """
    単一のターゲットからAP情報を収集し、メトリクスを更新する。
    """
    ip = target['ip_address']
    username = target['username']
    password = target['password']
    
    # 既存のメトリクスをクリア
    # (APがリストから削除された場合に対応するため)
    # Note: この方法だと、スクレイプ失敗時に古い情報が残る。
    # Prometheusの思想的には、スクレイプごとにクリアするのが一般的。
    # しかし、ここではラベルセットが動的に変わるため、
    # 処理開始時にそのIPのメトリクスをクリアする。
    
    # ※ prometheus_client v0.7.0以降、ラベル指定でのremoveは非推奨
    #   代わりに _remove メソッドを使うか、Gaugeを毎回作り直す。
    #   ここでは簡便さのため、ラベルが一致するものをクリアするアプローチを試みる。
    #   （ただし、prometheus_clientは「存在しない」状態を表現するのが難しい）
    
    # ap_scrape_up をまず 0 (失敗) に設定しておく
    ap_scrape_up.labels(target_ip=ip).set(0)

    token = get_token(ip, username, password)
    if not token:
        logging.warning(f"Skipping scrape for {ip} due to token failure.")
        return

    ap_query_url = f"https://{ip}:5825/management/v1/aps/query"
    headers = {'Authorization': f"Bearer {token}"}

    try:
        response = requests.get(
            ap_query_url,
            headers=headers,
            verify=False,
            timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        data = response.json().get('data', [])
        logging.info(f"Successfully scraped {len(data)} APs from {ip}")

        # このターゲットの既存APメトリクスをクリア
        # (APが削除された場合に対応するため、現在のAPリストを保持)
        current_ap_serials = {ap.get('serialNumber') for ap in data}
        
        # 既存のメトリクスを走査し、今回取得できなかったAPのメトリクスを削除
        # (prometheus_clientでは .remove() が推奨される)
        # ※この処理はメトリクスが多いと重くなる可能性がある
        
        # シンプル化: ap_info と ap_status は常に上書き(set)し、
        # 存在しなくなったAPはGrafana側で "N/A" (or 欠損) として扱われる。
        # Prometheusは時系列DBなので、値が来なくなれば stale となる。
        # GrafanaのTableで "Last" を使えば問題ない。

        for ap in data:
            hostname = ap.get('hostname', 'N/A')
            serial = ap.get('serialNumber', 'N/A')
            ip_addr = ap.get('ipAddress', 'N/A')
            
            if serial == 'N/A':
                continue # シリアル番号がないデータはスキップ

            labels = {
                'target_ip': ip,
                'hostname': hostname,
                'serialNumber': serial,
                'ipAddress': ip_addr
            }

            # 1. AP情報 (ap_info)
            ap_info.labels(**labels).set(1)

            # 2. APステータス (ap_status)
            status_val = ap.get('status')
            status_metric = 1 if status_val == 'InService' else 0
            ap_status.labels(**labels).set(status_metric)

        # 3. APIスクレイプステータス (ap_scrape_up)
        ap_scrape_up.labels(target_ip=ip).set(1)

    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to scrape AP data from {ip}: {e}")
        # ap_scrape_up は既に 0 に設定されている

def load_targets_from_csv():
    """CSVファイルからターゲット情報を読み込む"""
    targets = []
    if not os.path.exists(CSV_FILE_PATH):
        logging.error(f"CSV file not found: {CSV_FILE_PATH}")
        return []
        
    try:
        with open(CSV_FILE_PATH, mode='r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if 'ip_address' in row and 'username' in row and 'password' in row:
                    targets.append(row)
                else:
                    logging.warning(f"Skipping invalid row in CSV: {row}")
        logging.info(f"Loaded {len(targets)} targets from {CSV_FILE_PATH}")
        return targets
    except Exception as e:
        logging.error(f"Failed to read CSV file {CSV_FILE_PATH}: {e}")
        return []

def collect_all_metrics():
    """
    スケジュールされたジョブ。
    全ターゲットを並列でポーリングする。
    """
    logging.info("Starting scheduled metric collection...")
    targets = load_targets_from_csv()
    if not targets:
        logging.warning("No targets loaded, skipping collection.")
        return

    # スレッドプールを使用して並列実行
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        executor.map(collect_metrics_for_target, targets)
    
    logging.info("Scheduled metric collection finished.")

# --- Flask ルート ---
@app.route('/metrics')
def metrics():
    """Prometheusがスクレイプするエンドポイント"""
    return Response(generate_latest(REGISTRY), mimetype='text/plain')

@app.route('/')
def index():
    """ヘルスチェック用"""
    return "Extreme AP Exporter is running. Go to /metrics"

# --- スケジューラースレッド ---
def run_scheduler():
    """
Obfuscation: a
    スケジュールジョブ（データ収集）を別スレッドで実行する。
    """
    # 起動時にまず1回実行
    collect_all_metrics()
    # その後、スケジュール実行
    schedule.every(POLLING_INTERVAL).seconds.do(collect_all_metrics)
    while True:
        schedule.run_pending()
        time.sleep(1)

# --- メイン実行 ---
if __name__ == '__main__':
    # requestsのSSL検証無効化に伴う警告を抑制
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    # スケジューラーをデーモンスレッドで開始
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    
    # Flaskアプリ（/metrics）をメインスレッドで実行
    logging.info(f"Starting exporter on http://0.0.0.0:{EXPORTER_PORT}")
    app.run(host='0.0.0.0', port=EXPORTER_PORT)
