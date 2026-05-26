"""Anthropic Claude マルチモーダルアダプタ (デフォルト主軸)."""

from __future__ import annotations

import base64
import os
import sys
from typing import Optional

from PIL import Image

from .base import LLMClient, LLMResponse

DEFAULT_MODEL = "claude-sonnet-4-5"

# 概算単価 (USD / 1M tokens). 厳密な請求額計算ではなく目安.
_PRICES_PER_MTOK = {
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-opus-4-6": (15.0, 75.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


class ClaudeClient(LLMClient):
    name = "claude"

    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None):
        try:
            import anthropic  # noqa: WPS433
        except ImportError as e:
            raise RuntimeError(
                "anthropic SDK が未インストール. `pip install anthropic`"
            ) from e

        self._anthropic = anthropic
        self._model = model or DEFAULT_MODEL
        self._client = anthropic.Anthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"),
        )

    def extract(
        self,
        image: Image.Image,
        prompt: str,
        *,
        system: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        # JPEG エンコード (Anthropic の 5MB 制限回避).
        # base.py 側で base64化後の 5MB 上限を超えないよう品質を自動調整する.
        jpeg = self._image_to_jpeg_bytes(image, quality=90)
        b64 = base64.standard_b64encode(jpeg).decode("ascii")

        # 診断ログ用 (BadRequest 発生時に画像サイズと相関させる)
        img_w, img_h = image.size
        img_bytes = len(jpeg)

        kwargs = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        }
        if system:
            kwargs["system"] = system

        try:
            msg = self._client.messages.create(**kwargs)
        except Exception as e:
            # Anthropic SDK の BadRequestError などには .body / .status_code が付く.
            # tenacity 経由で RetryError にラップされる前に, 実エラー本文を Cloud Run ログに残す.
            err_type = type(e).__name__
            err_body = getattr(e, "body", None)
            err_status = getattr(e, "status_code", None)
            err_response = getattr(e, "response", None)
            err_msg = getattr(e, "message", None) or str(e)
            mb = img_bytes / 1024 / 1024
            print(
                f"[claude.py] Anthropic API error: type={err_type} "
                f"status={err_status} model={self._model} "
                f"img_size={img_w}x{img_h} jpeg_bytes={img_bytes} "
                f"({mb:.2f}MB) "
                f"msg={err_msg!r} body={err_body!r}",
                file=sys.stderr,
                flush=True,
            )
            if err_response is not None:
                try:
                    body_text = getattr(err_response, "text", None)
                    if body_text is None and hasattr(err_response, "read"):
                        body_text = err_response.read()
                    print(
                        f"[claude.py] response.body={body_text!r}",
                        file=sys.stderr,
                        flush=True,
                    )
                except Exception:  # noqa: BLE001
                    pass
            raise

        # text content blocksを連結
        text = "".join(
            getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text"
        )
        usage = getattr(msg, "usage", None)
        in_tok = getattr(usage, "input_tokens", 0) if usage else 0
        out_tok = getattr(usage, "output_tokens", 0) if usage else 0
        cost = self._estimate_cost(in_tok, out_tok)
        return LLMResponse(
            text=text,
            model=self._model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=cost,
        )

    def _estimate_cost(self, in_tok: int, out_tok: int) -> float:
        prices = _PRICES_PER_MTOK.get(self._model, (3.0, 15.0))
        return (in_tok / 1_000_000) * prices[0] + (out_tok / 1_000_000) * prices[1]
