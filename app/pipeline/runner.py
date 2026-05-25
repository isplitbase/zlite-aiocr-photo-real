"""AI-OCR Hybrid Pipeline ランナー (Cloud Run実行).

元の hybrid_pipeline.py (CLI) を Cloud Run の HTTPハンドラから呼べる形に整形。

主な変更点:
  - 入力: S3 URI のリストを受け取り、boto3 で /tmp にダウンロードして処理
  - 出力: JSONを直接 dict で返す (ファイル書出しは行わない)
  - ANTHROPIC_API_KEY は埋め込み済み (Cloud Run env var で上書き可)
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import boto3

# ===========================================================================
# Anthropic API キー (アプリオーナー 水野氏 の Anthropic アカウント発行)
# ---------------------------------------------------------------------------
# Cloud Run の環境変数 / Secret Manager で ANTHROPIC_API_KEY を設定すれば
# 下記の埋め込み値より優先される (setdefault のため)。
# キーをローテーションする場合:
#   推奨) gcloud secrets versions add で Secret Manager 経由で差し替え
#   簡易) 下記文字列を書き換えて再デプロイ
# ===========================================================================
_EMBEDDED_ANTHROPIC_KEY = (
    "sk-ant-api03-8zeFzqfO0US5-tW44OZLLEKQ307rvYXqRWjNVI6ecPearXltGY8iD"
    "jf3LqgjX6Ry9BmzYfBt_-o4bLr2riTMEQ-660aMwAA"
)
os.environ.setdefault("ANTHROPIC_API_KEY", _EMBEDDED_ANTHROPIC_KEY)

# --- AI-OCR 内部モジュールを解決するため app/pipeline をパスに追加 -----
_PIPELINE_DIR = Path(__file__).resolve().parent  # .../app/pipeline
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))

from src.pdf_classifier_v3 import PDFKind, classify_pdf  # noqa: E402
from src.pdf_io import cap_resolution  # noqa: E402
from src.pdf_io_adaptive import load_as_pages_adaptive  # noqa: E402
from src.pipeline_kintou_v7 import PipelineKintouV7  # noqa: E402
from src.pipeline_v2 import PipelineConfig  # noqa: E402
from src.preprocess_v3 import run_default as run_v3  # noqa: E402
from src.schema_adapter_final6 import convert_v6_to_analygent  # noqa: E402


# ===========================================================================
# S3 / 入力ハンドリング
# ===========================================================================

def _parse_s3_uri(uri: str) -> Tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError(f"Unsupported uri (expected s3://...): {uri}")
    no_scheme = uri[len("s3://"):]
    parts = no_scheme.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Invalid s3 uri: {uri}")
    return parts[0], parts[1]


def _s3_client():
    """zlite共通の環境変数規約 (S3_ACCESS_KEY/S3_SECRET_KEY/S3_REGION) を読む."""
    access_key = os.getenv("S3_ACCESS_KEY") or os.getenv("AWS_ACCESS_KEY_ID")
    secret_key = os.getenv("S3_SECRET_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY")
    region = os.getenv("S3_REGION") or os.getenv("AWS_REGION") or "ap-northeast-1"
    if not access_key or not secret_key:
        return boto3.client("s3", region_name=region)
    return boto3.client(
        "s3",
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )


def _download_s3_to_tmp(s3_uri: str, work_dir: Path, idx: int) -> Path:
    bucket, key = _parse_s3_uri(s3_uri)
    base = Path(key).name or f"input_{idx}.pdf"
    local = work_dir / f"{idx:02d}_{base}"
    n = 1
    while local.exists():
        local = work_dir / f"{idx:02d}_{n}_{base}"
        n += 1
    _s3_client().download_file(bucket, key, str(local))
    return local


def _normalize_pdf_inputs(payload: Dict[str, Any], work_dir: Path) -> List[Dict[str, str]]:
    """payload から処理対象のPDFリストを正規化する.

    対応する入力キー:
      - "pdfurls" : ["s3://...", "s3://..."]   (推奨)
      - "files"   : ["s3://..."]                (zlite-getpdfinfo 互換)
      - "file"    : "s3://..."                  (単一)
    """
    raw = payload.get("pdfurls") or payload.get("files") or payload.get("file")
    if not raw:
        raise ValueError("payload に pdfurls / files / file のいずれかが必要です")

    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError("pdfurls はリスト形式で指定してください")

    items: List[Dict[str, str]] = []
    for i, uri in enumerate(raw, start=1):
        s = str(uri).strip()
        if not s:
            continue
        local = _download_s3_to_tmp(s, work_dir, i)
        items.append({
            "s3_uri": s,
            "local_path": str(local),
            "original_name": Path(_parse_s3_uri(s)[1]).name,
        })
    return items


# ===========================================================================
# AI-OCR 実行
# ===========================================================================

def _ocr_photo_pdf(pdf_path, *, model, max_side, dpi):
    """photo_pdf を Claude v7 で全ページOCR. (page_results, cost) を返す."""
    cfg = PipelineConfig(
        provider="claude",
        model=model,
        dpi=dpi,
        max_side=max_side,
        do_preprocess=True,
        do_warp=True,
        do_flatten=True,
        recheck_threshold=0.0,
        max_rechecks_per_page=0,
    )
    pipeline = PipelineKintouV7(cfg)

    imgs = load_as_pages_adaptive(pdf_path, target_long_side=max_side)
    page_results = []
    total_cost = 0.0
    for pi in range(1, len(imgs) + 1):
        img = imgs[pi - 1]
        img, _ = run_v3(img)
        img = cap_resolution(img, max_side=max_side)
        page_res, cost = pipeline.extract_page(img, page_index=pi)
        total_cost += cost
        page_results.append(page_res.model_dump(mode="json"))
    return page_results, total_cost


def _process_one(pdf_info, *, model, max_side, dpi, file_period, file_date, classify_only):
    pdf_path = Path(pdf_info["local_path"])
    rep = classify_pdf(pdf_path)
    result = {
        "pdf": pdf_info["original_name"],
        "s3_uri": pdf_info["s3_uri"],
        "kind": rep.kind.value,
        "photo_score": rep.photo_score,
        "num_pages": rep.num_pages,
    }

    if rep.kind == PDFKind.PHOTO_PDF:
        result["routing"] = "claude_ai_ocr"
        if classify_only:
            result["note"] = "photo_pdf判定 (classify-onlyのためOCR未実行)"
            return result

        t0 = time.time()
        page_results, cost = _ocr_photo_pdf(
            pdf_path,
            model=model,
            max_side=max_side,
            dpi=dpi,
        )
        analygent = convert_v6_to_analygent(
            page_results,
            file_period=file_period,
            file_date=file_date,
            output_format="list",
        )
        analygent.pop("_meta", None)
        result.update({
            "analygent": analygent,
            # ai_raw: Claude AI のページごとの生応答 (PipelineKintouV7.extract_page の戻り値).
            #   PHP 側 (zlite-aiocr-photo.php) で response_pdf カラムへ保存し、
            #   後段で決算年月日 / 単位 などのメタ情報を抽出できるようにする。
            "ai_raw": page_results,
            "records": len(analygent.get("list", [])),
            "cost_usd": round(cost, 4),
            "elapsed_sec": round(time.time() - t0, 1),
        })
    else:
        result["routing"] = "existing_analygent_engine"
        result["note"] = "現行Analygent (Gemini/GPT) で処理すべき (本サービス対象外)"

    return result


# ===========================================================================
# エントリポイント
# ===========================================================================

def _common_params(payload):
    return {
        "model": payload.get("model") or os.getenv("CLAUDE_MODEL") or "claude-haiku-4-5-20251001",
        "max_side": int(payload.get("max_side") or 1400),
        "dpi": int(payload.get("dpi") or 200),
        "file_period": payload.get("file_period"),
        "file_date": payload.get("file_date"),
    }


def run_aiocr_photo(payload):
    """メイン: PDF分類 → photo_pdfはClaude OCR → Analygent JSON返却."""
    if payload.get("nodoai") is True:
        return {
            "status": "skipped",
            "ai_case_id": payload.get("ai_case_id"),
            "reason": "nodoai=true のため AI-OCR をスキップしました",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    params = _common_params(payload)
    classify_only = bool(payload.get("classify_only"))

    with tempfile.TemporaryDirectory(prefix="aiocr_", dir="/tmp") as tmp:
        work_dir = Path(tmp)
        pdf_inputs = _normalize_pdf_inputs(payload, work_dir)

        results = []
        total_cost = 0.0
        routing_counts = {"claude_ai_ocr": 0, "existing_analygent_engine": 0}

        for pdf_info in pdf_inputs:
            try:
                r = _process_one(
                    pdf_info,
                    classify_only=classify_only,
                    **params,
                )
                results.append(r)
                key = r.get("routing", "")
                routing_counts[key] = routing_counts.get(key, 0) + 1
                total_cost += r.get("cost_usd", 0) or 0
            except Exception as e:
                results.append({
                    "pdf": pdf_info.get("original_name"),
                    "s3_uri": pdf_info.get("s3_uri"),
                    "status": "error",
                    "error": str(e),
                })

    return {
        "status": "success",
        "ai_case_id": payload.get("ai_case_id"),
        "postingPeriod": payload.get("postingPeriod"),
        "results": results,
        "routing_counts": routing_counts,
        "total_cost_usd": round(total_cost, 4),
        "model": params["model"],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def run_classify_only(payload):
    """判定のみ実行 (Claude API は呼ばない)."""
    payload2 = dict(payload)
    payload2["classify_only"] = True
    return run_aiocr_photo(payload2)
