"""
media_helper.py — Xử lý nạp ảnh tham chiếu từ nhiều nguồn:
- URL web (http/https)
- Chuỗi Base64 (data:image/... hoặc raw base64)
- Đường dẫn file cục bộ (local path)
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import re
import urllib.request
import uuid
from pathlib import Path
from typing import List, Optional

import config

logger = logging.getLogger(__name__)

TEMP_REF_DIR = config.OUTPUT_DIR / "temp_refs"
TEMP_REF_DIR.mkdir(parents=True, exist_ok=True)


def prepare_reference_image(source: str) -> Path:
    """
    Nhận vào nguồn ảnh (URL, Base64, hoặc file path),
    đảm bảo file tồn tại trên đĩa và trả về đường dẫn file Path hợp lệ.
    """
    source = source.strip()

    # 1. Nguồn là URL HTTP/HTTPS
    if source.startswith("http://") or source.startswith("https://"):
        logger.info("Đang tải ảnh tham chiếu từ URL: %s", source[:80])
        ext = ".png"
        parsed_ext = Path(source.split("?")[0]).suffix
        if parsed_ext.lower() in [".png", ".jpg", ".jpeg", ".webp", ".bmp"]:
            ext = parsed_ext.lower()

        target_file = TEMP_REF_DIR / f"ref_{uuid.uuid4().hex}{ext}"
        req = urllib.request.Request(
            source,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            target_file.write_bytes(resp.read())

        return target_file.resolve()

    # 2. Nguồn là Base64 Data URL (data:image/...) hoặc chuỗi base64 thô
    if source.startswith("data:image/") or re.match(r"^[A-Za-z0-9+/=]{100,}$", source[:150]):
        logger.info("Đang giải mã ảnh tham chiếu từ Base64 …")
        ext = ".png"
        if source.startswith("data:image/"):
            header, b64_data = source.split(",", 1)
            if "jpeg" in header or "jpg" in header:
                ext = ".jpg"
            elif "webp" in header:
                ext = ".webp"
        else:
            b64_data = source

        target_file = TEMP_REF_DIR / f"ref_{uuid.uuid4().hex}{ext}"
        target_file.write_bytes(base64.b64decode(b64_data))
        return target_file.resolve()

    # 3. Nguồn là đường dẫn file cục bộ
    local_path = Path(source)
    if not local_path.is_absolute():
        local_path = (Path.cwd() / local_path).resolve()

    if local_path.exists() and local_path.is_file():
        return local_path

    raise FileNotFoundError(f"Không thể định vị hoặc tải ảnh tham chiếu từ nguồn: {source[:100]}")


def prepare_reference_images(sources: Optional[List[str]]) -> List[Path]:
    """Chuyển đổi danh sách các nguồn ảnh tham chiếu thành danh sách các file Path."""
    if not sources:
        return []
    paths: List[Path] = []
    for s in sources:
        if s and s.strip():
            paths.append(prepare_reference_image(s))
    return paths
