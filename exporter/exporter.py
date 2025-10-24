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
# ログ設定（DEBUGレベルに変更して詳細表示）
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - [%(funcName)s:%(lineno)d] - %(message)s'
)

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
            logging.debug(f"[{ip}] Using cached token (expires at {cache_entry['expires_at']})")
            return cache_entry['token']

        # トークンがない、または期限切れのため再取得
        logging.info(f"[{ip}] Requesting new token...")
        token_url = f"https://{ip}:5825/management/v1/oauth2/token"
        payload = {
            'userId': username,
            'password': password,
            'grantType': 'password',
            'scope': '...'
        }
        
        logging.debug(f"[{ip}] Token request URL: {token_url}")
        logging.debug(f"[{ip}] Token request payload: userId={username}, grantType=password")
        
        try:
            # SSL証明書の検証を無効化 (自己署名証明書対策)
            response = requests.post(
                token_url,
                json=payload,
                verify=False,
                timeout=REQUEST_TIMEOUT
            )
            
            logging.debug(f"[{ip}] Token response status: {response.status_code}")
            logging.debug(f"[{ip}] Token response headers: {dict(response.headers)}")
            
            response.raise_for_status()
            data = response.json()
            
            logging.debug(f"[{ip}] Token response body keys: {list(data.keys())}")
            
            access_token = data.get('access_token')
            if not access_token:
                logging.error(f"[{ip}] access_token not found in response: {data}")
                raise ValueError("access_token not found in response")

            expires_at = now + timedelta(minutes=TOKEN_LIFETIME_MINUTES)
            token_cache[ip] = {'token': access_token, 'expires_at': expires_at}
            logging.info(f"[{ip}] Successfully obtained token (expires at {expires_at})")
            logging.debug(f"[{ip}] Token preview: {access_token[:20]}...")
            return access_token

        except requests.exceptions.RequestException as e:
            logging.error(f"[{ip}] Failed to get token: {type(e).__name__}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logging.error(f"[{ip}] Error response body: {e.response.text[:500]}")
            return None

# --- データ収集 ---
def collect_metrics_for_target(target):
    """
    単一のターゲットからAP情報を収集し、メトリクスを更新する。
    """
    ip = target['ip_address']
    username = target['username']
    password = target['password']
    
    logging.info(f"[{ip}] ===== Starting metric collection =====")
    
    # ap_scrape_up をまず 0 (失敗) に設定しておく
    ap_scrape_up.labels(target_ip=ip).set(0)

    token = get_token(ip, username, password)
    if not token:
        logging.warning(f"[{ip}] Skipping scrape due to token failure.")
        return

    ap_query_url = f"https://{ip}:5825/management/v1/aps/query"
    headers = {
        'Authorization': f"Bearer {token}",
        'Accept': 'application/json'  # curlコマンドと同じヘッダーを追加
    }

    logging.info(f"[{ip}] Querying AP data from: {ap_query_url}")
    logging.debug(f"[{ip}] Request headers: Authorization=Bearer {token[:20]}..., Accept=application/json")

    try:
        response = requests.get(
            ap_query_url,
            headers=headers,
            verify=False,
            timeout=REQUEST_TIMEOUT
        )
        
        logging.debug(f"[{ip}] AP query response status: {response.status_code}")
        logging.debug(f"[{ip}] AP query response headers: {dict(response.headers)}")
        
        response.raise_for_status()
        
        # レスポンスボディをログ出力（大きすぎる場合は切り詰め）
        response_text = response.text
        logging.debug(f"[{ip}] AP query response body (first 1000 chars): {response_text[:1000]}")
        
        response_json = response.json()
        
        # レスポンスが辞書かリストかを判定
        if isinstance(response_json, dict):
            logging.debug(f"[{ip}] Response is a dict with keys: {list(response_json.keys())}")
            data = response_json.get('data', [])
        elif isinstance(response_json, list):
            logging.debug(f"[{ip}] Response is a list directly")
            data = response_json
        else:
            logging.error(f"[{ip}] Unexpected response type: {type(response_json)}")
            data = []
        
        logging.info(f"[{ip}] Successfully scraped {len(data)} APs")
        
        if len(data) == 0:
            logging.warning(f"[{ip}] No AP data found in response!")
            logging.warning(f"[{ip}] Full response (first 2000 chars): {str(response_json)[:2000]}")

        # APデータの処理
        ap_count = 0
        for ap in data:
            hostname = ap.get('hostname', 'N/A')
            serial = ap.get('serialNumber', 'N/A')
            ip_addr = ap.get('ipAddress', 'N/A')
            status_val = ap.get('status', 'Unknown')
            
            logging.debug(f"[{ip}] Processing AP: hostname={hostname}, serial={serial}, ip={ip_addr}, status={status_val}")
            
            if serial == 'N/A':
                logging.warning(f"[{ip}] Skipping AP with missing serial number: {ap}")
                continue

            labels = {
                'target_ip': ip,
                'hostname': hostname,
                'serialNumber': serial,
                'ipAddress': ip_addr
            }

            # 1. AP情報 (ap_info)
            ap_info.labels(**labels).set(1)

            # 2. APステータス (ap_status)
            status_metric = 1 if status_val == 'InService' else 0
            ap_status.labels(**labels).set(status_metric)
            
            ap_count += 1

        logging.info(f"[{ip}] Successfully processed {ap_count} APs")

        # 3. APIスクレイプステータス (ap_scrape_up)
        ap_scrape_up.labels(target_ip=ip).set(1)
        logging.info(f"[{ip}] ===== Metric collection completed successfully =====")

    except requests.exceptions.RequestException as e:
        logging.error(f"[{ip}] Failed to scrape AP data: {type(e).__name__}: {e}")
        if hasattr(e, 'response') and e.response is not None:
            logging.error(f"[{ip}] Error response status: {e.response.status_code}")
            logging.error(f"[{ip}] Error response body: {e.response.text[:1000]}")
        logging.info(f"[{ip}] ===== Metric collection failed =====")
    except Exception as e:
        logging.error(f"[{ip}] Unexpected error during scraping: {type(e).__name__}: {e}")
        import traceback
        logging.error(f"[{ip}] Traceback: {traceback.format_exc()}")
        logging.info(f"[{ip}] ===== Metric collection failed =====")

def load_targets_from_csv():
    """CSVファイルからターゲット情報を読み込む"""
    logging.info(f"Loading targets from CSV: {CSV_FILE_PATH}")
    targets = []
    if not os.path.exists(CSV_FILE_PATH):
        logging.error(f"CSV file not found: {CSV_FILE_PATH}")
        return []
        
    try:
        with open(CSV_FILE_PATH, mode='r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row_num, row in enumerate(reader, start=1):
                if 'ip_address' in row and 'username' in row and 'password' in row:
                    targets.append(row)
                    logging.debug(f"CSV row {row_num}: ip={row['ip_address']}, username={row['username']}")
                else:
                    logging.warning(f"Skipping invalid row {row_num} in CSV: {row}")
        logging.info(f"Loaded {len(targets)} targets from {CSV_FILE_PATH}")
        return targets
    except Exception as e:
        logging.error(f"Failed to read CSV file {CSV_FILE_PATH}: {e}")
        import traceback
        logging.error(f"Traceback: {traceback.format_exc()}")
        return []

def collect_all_metrics():
    """
    スケジュールされたジョブ。
    全ターゲットを並列でポーリングする。
    """
    logging.info("========================================")
    logging.info("Starting scheduled metric collection...")
    logging.info("========================================")
    
    targets = load_targets_from_csv()
    if not targets:
        logging.warning("No targets loaded, skipping collection.")
        return

    # スレッドプールを使用して並列実行
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        executor.map(collect_metrics_for_target, targets)
    
    logging.info("========================================")
    logging.info("Scheduled metric collection finished.")
    logging.info("========================================")

# --- Flask ルート ---
@app.route('/metrics')
def metrics():
    """Prometheusがスクレイプするエンドポイント"""
    logging.debug("Metrics endpoint accessed")
    return Response(generate_latest(REGISTRY), mimetype='text/plain')

@app.route('/')
def index():
    """ヘルスチェック用"""
    return "Extreme AP Exporter is running. Go to /metrics"

# --- スケジューラースレッド ---
def run_scheduler():
    """
    スケジュールジョブ（データ収集）を別スレッドで実行する。
    """
    logging.info("Scheduler thread started")
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
    
    logging.info("=" * 60)
    logging.info("Extreme AP Exporter Starting")
    logging.info(f"Port: {EXPORTER_PORT}")
    logging.info(f"Polling interval: {POLLING_INTERVAL} seconds")
    logging.info(f"CSV file: {CSV_FILE_PATH}")
    logging.info("=" * 60)
    
    # スケジューラーをデーモンスレッドで開始
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    
    # Flaskアプリ（/metrics）をメインスレッドで実行
    logging.info(f"Starting exporter on http://0.0.0.0:{EXPORTER_PORT}")
    app.run(host='0.0.0.0', port=EXPORTER_PORT)