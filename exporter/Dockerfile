# ベースイメージ
FROM python:3.11-slim

# 作業ディレクトリ
WORKDIR /app

# 依存ライブラリをインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションコードとCSVファイルをコピー
COPY exporter.py .
COPY targets.csv .

# Exporterがリッスンするポート
EXPOSE 9100

# アプリケーションの実行
CMD ["python", "exporter.py"]
