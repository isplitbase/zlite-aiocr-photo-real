"""LLMアダプタ基底クラス."""

from __future__ import annotations

import abc
import io
from dataclasses import dataclass
from typing import Optional

from PIL import Image


@dataclass
class LLMResponse:
    """LLM呼び出し結果."""

    text: str  # LLMの生レスポンス本文 (JSON文字列を想定)
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Optional[float] = None


class LLMClient(abc.ABC):
    """マルチモーダルLLMの最小インターフェース.

    画像 + プロンプトを送り, テキストを得るだけ.
    JSON抽出やリトライはパイプライン側で行う.
    """

    name: str = "abstract"

    @abc.abstractmethod
    def extract(
        self,
        image: Image.Image,
        prompt: str,
        *,
        system: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        ...

    @staticmethod
    def _image_to_png_bytes(image: Image.Image) -> bytes:
        """PNG ロスレスエンコード (gemini/openai 用)."""
        buf = io.BytesIO()
        image.convert("RGB").save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    @staticmethod
    def _image_to_jpeg_bytes(
        image: Image.Image,
        quality: int = 90,
        max_raw_bytes: int = 3_700_000,
    ) -> bytes:
        """JPEG エンコード (Anthropic Claude 用).

        Anthropic API は base64 エンコード後の文字列サイズで 5MB (5,242,880 bytes)
        を判定するため, raw bytes は 3,932,160 bytes 以下である必要がある.
        余裕を見て max_raw_bytes=3,700,000 をデフォルト上限とし,
        超える場合は品質を段階的に下げて再エンコードする.

        写真コンテンツの場合 quality=90 で PNG の 1/5〜1/10 のサイズになるため,
        通常は初回エンコードで上限内に収まる.
        """
        rgb = image.convert("RGB")
        last_data = b""
        for q in (quality, 85, 80, 70, 60, 50):
            buf = io.BytesIO()
            rgb.save(buf, format="JPEG", quality=q, optimize=True, progressive=False)
            data = buf.getvalue()
            last_data = data
            if len(data) <= max_raw_bytes:
                return data
        # 最低品質でも超える場合はそのまま返す
        # (Claude 側で 400 になるが claude.py のログには記録される)
        return last_data
