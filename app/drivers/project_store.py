"""
ProjectStore — lưu và đọc project ID của flow.google.com.

File lưu: .flow_project.json (trong thư mục gốc dự án)
Schema:
{
  "project_url": "https://flow.google.com/project/<id>",
  "created_at": "2026-11-14T08:00:00"
}
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_STORE_FILE = Path(__file__).parent.parent.parent / ".flow_project.json"


class ProjectStore:

    @staticmethod
    def load() -> Optional[str]:
        """Trả về project_url đã lưu, hoặc None nếu chưa có."""
        if not _STORE_FILE.exists():
            return None
        try:
            data = json.loads(_STORE_FILE.read_text(encoding="utf-8"))
            url = data.get("project_url", "")
            if url and "/project/" in url:
                logger.info("Loaded project URL from store: %s", url)
                return url
        except Exception as exc:
            logger.warning("Không đọc được project store: %s", exc)
        return None

    @staticmethod
    def save(project_url: str) -> None:
        """Lưu project_url vào file."""
        data = {
            "project_url": project_url,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        _STORE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Saved project URL: %s", project_url)

    @staticmethod
    def clear() -> None:
        """Xóa project đã lưu (để lần sau tạo project mới)."""
        if _STORE_FILE.exists():
            _STORE_FILE.unlink()
            logger.info("Cleared project store.")
