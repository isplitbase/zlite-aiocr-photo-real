# zlite-aiocr-photo

写真撮影された決算書PDF (photo_pdf) を Claude API で OCR し、既存 Analygent の JSON 形式 (`{"list":[...]}`) に変換して返す Cloud Run サービス。

判定で `text_pdf` / `scan_pdf` と認識されたものは既存 Analygent (Gemini/GPT) で処理すべきと返却するだけで、本サービスは API を叩かない。

---

## アーキテクチャ

```
[PHP] zlite-getpdfinfo.php / do_ai.php
   │
   ▼ POST + ID Token
[Cloud Run] zlite-aiocr-photo  /v1/pipeline
   │
   ├─ S3からPDFをダウンロード (boto3)
   ├─ pdf_classifier_v3 で photo / text / scan を判定
   ├─ photo_pdf のみ pipeline_kintou_v7 で Claude OCR
   └─ schema_adapter_final6 で Analygent JSON (list形式) に変換
```

OCRエンジン本体 (`app/pipeline/src/`) は `AI-OCR_*.zip` のソースを未改変で配置しています。差し替え時は `src/` を上書きするだけで OK。

---

## エンドポイント

### `GET /health`
ヘルスチェック。`{"ok": true}` を返す。

### `POST /v1/pipeline`
メインエンドポイント。

**リクエスト**
```json
{
  "ai_case_id": 12345,
  "pdfurls": [
    "s3://zlite/u123-1.pdf",
    "s3://zlite/u123-2.pdf"
  ],
  "file_period": "今期",
  "file_date":   "2026-03-31",
  "model":       "claude-haiku-4-5-20251001",
  "max_side":    1400,
  "dpi":         200,
  "classify_only": false,
  "nodoai":      false
}
```

| キー | 型 | 必須 | 説明 |
|---|---|---|---|
| `ai_case_id` | int | 任意 | レスポンスにそのまま返す |
| `pdfurls` (or `files` / `file`) | string[] | ★ | S3 URI のリスト |
| `file_period` | string | 任意 | `今期` / `前期` / `前々期` |
| `file_date` | string | 任意 | `YYYY-MM-DD` |
| `model` | string | 任意 | デフォルト `claude-haiku-4-5-20251001` |
| `max_side` | int | 任意 | 画像長辺ピクセル (デフォルト 1400) |
| `dpi` | int | 任意 | レンダリングDPI (デフォルト 200) |
| `classify_only` | bool | 任意 | true なら判定のみで Claude を呼ばない |
| `nodoai` | bool | 任意 | true なら処理スキップ (互換) |

**レスポンス (成功)**
```json
{
  "status": "success",
  "ai_case_id": 12345,
  "results": [
    {
      "pdf": "u123-1.pdf",
      "s3_uri": "s3://zlite/u123-1.pdf",
      "kind": "photo_pdf",
      "photo_score": 0.65,
      "num_pages": 3,
      "routing": "claude_ai_ocr",
      "analygent": { "list": [ /* Analygent 確定仕様 */ ] },
      "records": 87,
      "cost_usd": 0.32,
      "elapsed_sec": 41.2
    },
    {
      "pdf": "u123-2.pdf",
      "kind": "text_pdf",
      "photo_score": 0.12,
      "routing": "existing_analygent_engine",
      "note": "現行Analygent (Gemini/GPT) で処理すべき"
    }
  ],
  "routing_counts": { "claude_ai_ocr": 1, "existing_analygent_engine": 1 },
  "total_cost_usd": 0.32,
  "model": "claude-haiku-4-5-20251001",
  "created_at": "2026-05-25T00:00:00Z"
}
```

### `POST /v1/aiocr-photo`
`/v1/pipeline` のエイリアス (同じ処理)。

### `POST /v1/classify`
判定のみ実行。Claude API は呼ばないので無料で振り分け確認できる。

---

## 環境変数

| 変数 | 必須 | 説明 |
|---|---|---|
| `ANTHROPIC_API_KEY` | 任意 | **`app/pipeline/runner.py` に水野氏のキーが埋め込み済み**。Cloud Run の環境変数で設定すれば上書きされる (`os.environ.setdefault`)。ローテーション時に Secret Manager 経由で差し替え可能 |
| `S3_ACCESS_KEY` | ★ | S3 (zlite共通) アクセスキー |
| `S3_SECRET_KEY` | ★ | S3 シークレットキー |
| `S3_REGION` | 任意 | デフォルト `ap-northeast-1` |
| `CLAUDE_MODEL` | 任意 | リクエストに `model` が無い場合のデフォルト |
| `PORT` | - | Cloud Run が自動付与 (8080) |

Cloud Run のサービスアカウントに S3 アクセスがある場合は、`S3_ACCESS_KEY` / `S3_SECRET_KEY` 未設定でも boto3 のデフォルト資格情報で動作する。

### Anthropic API キーの取扱

埋め込み場所: `app/pipeline/runner.py` の `_EMBEDDED_ANTHROPIC_KEY` 定数。アプリオーナー水野氏の Anthropic アカウントで発行されたキー。

将来ローテーションする場合は2通り:
- **Secret Manager 経由 (推奨)**: `gcloud secrets versions add anthropic-api-key --data-file=-` で新値を投入し、Cloud Run の `--set-secrets ANTHROPIC_API_KEY=anthropic-api-key:latest` を有効化。本ファイルの変更不要
- **ソース書き換え (簡易)**: `_EMBEDDED_ANTHROPIC_KEY` の文字列を直接更新して再デプロイ

---

## デプロイ

Cloud Run へのデプロイは、Dockerfile を含む本ディレクトリのルートを対象にビルド。

```bash
# 例: gcloud で直接デプロイ (dev)
# ANTHROPIC_API_KEY はソース埋め込みのため --set-secrets には含めない
gcloud run deploy zlite-aiocr-photo \
    --source . \
    --region asia-northeast1 \
    --no-allow-unauthenticated \
    --memory 4Gi --cpu 2 --timeout 1800 \
    --set-env-vars CLAUDE_MODEL=claude-haiku-4-5-20251001 \
    --set-secrets S3_ACCESS_KEY=s3-access-key:latest,S3_SECRET_KEY=s3-secret-key:latest
```

prod は別リポジトリ `zlite-aiocr-photo-real` をデプロイ。コード自体は dev と同一で、デプロイ先 (Cloud Run service 名) と環境変数だけ切り替える。

---

## PHP 側からの呼び出し

`sapis/cash_ai_checkbyclaude.php` と同じ流儀 (Service Account ID Token + curl POST) で叩く。`sapis/zlite-getpdfinfo.php` の Cloud Run 呼び出し直後に photo_pdf 分岐を追加するのが推奨ルート。

```php
// 概略
$service_url = "https://zlite-aiocr-photo-512697354748.asia-northeast1.run.app";
if ($port == "8012") {
    $service_url = "https://zlite-aiocr-photo-real-512697354748.asia-northeast1.run.app";
}
$audience = $service_url;
$url = rtrim($service_url, "/") . "/v1/pipeline";

$idToken = getCloudRunIdToken($serviceAccountJsonPath, $audience);
$payload = [
    "ai_case_id"   => $ai_case_id,
    "pdfurls"      => $s3Urls,
    "file_period"  => $file_period,
    "file_date"    => $file_date,
];
list($respBody, $errno, $err, $httpCode)
    = postJsonToCloudRun($url, $idToken, $payload);
```

---

## ディレクトリ構成

```
zlite-aiocr-photo/
├── Dockerfile
├── requirements.txt
├── README.md
├── .gitignore
└── app/
    ├── __init__.py
    ├── main.py                          # FastAPI ルータ
    └── pipeline/
        ├── __init__.py
        ├── runner.py 