"""
ProjectStore — Quản lý ánh xạ Project URL trên flow.google.com theo từng Ứng dụng (app_name).

File lưu: .flow_projects.json (trong thư mục gốc dự án)
Cấu trúc:
{
  "default": {
    "project_url": "https://flow.google.com/project/<id>",
    "created_at": "2026-11-14T08:00:00"
  },
  "AppTruyenTranh": {
    "project_url": "https://flow.google.com/project/<id2>",
    "created_at": "2026-11-15T17:55:00"
  }
}
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_STORE_FILE = Path(__file__).parent.parent.parent / ".flow_projects.json"
_LEGACY_FILE = Path(__file__).parent.parent.parent / ".flow_project.json"


class ProjectStore:

    @classmethod
    def _read_data(cls) -> Dict[str, dict]:
        """Đọc dữ liệu tất cả project, tự động chuyển đổi từ file legacy nếu có."""
        if _STORE_FILE.exists():
            try:
                return json.loads(_STORE_FILE.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Lỗi đọc file .flow_projects.json: %s", exc)

        # Chuyển đổi từ file cũ .flow_project.json
        if _LEGACY_FILE.exists():
            try:
                legacy = json.loads(_LEGACY_FILE.read_text(encoding="utf-8"))
                legacy_url = legacy.get("project_url")
                if legacy_url:
                    converted = {
                        "default": {
                            "project_url": legacy_url,
                            "created_at": legacy.get("created_at") or datetime.now().isoformat(timespec="seconds"),
                        }
                    }
                    cls._write_data(converted)
                    return converted
            except Exception:
                pass

        return {}

    @classmethod
    def _write_data(cls, data: Dict[str, dict]) -> None:
        """Ghi dữ liệu ra file .flow_projects.json."""
        _STORE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def get_project(cls, app_name: str = "default") -> Optional[str]:
        """Trả về URL project được gán cho app_name, hoặc None nếu chưa có."""
        key = (app_name or "default").strip()
        data = cls._read_data()
        entry = data.get(key)
        if entry and isinstance(entry, dict):
            url = entry.get("project_url", "")
            if url and "/project/" in url:
                return url
        return None

    @classmethod
    def set_project(cls, app_name: str, project_url: str) -> None:
        """Lưu hoặc cập nhật project URL cho một app_name cụ thể."""
        key = (app_name or "default").strip()
        data = cls._read_data()
        data[key] = {
            "project_url": project_url,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        cls._write_data(data)
        logger.info("Đã lưu project cho app [%s] -> %s", key, project_url)

    @classmethod
    def list_projects(cls) -> Dict[str, dict]:
        """Trả về danh sách tất cả các app và project URL tương ứng."""
        return cls._read_data()

    @classmethod
    def delete_project(cls, app_name: str) -> bool:
        """Xóa liên kết project của một app."""
        key = (app_name or "default").strip()
        data = cls._read_data()
        if key in data:
            del data[key]
            cls._write_data(data)
            logger.info("Đã xóa liên kết project cho app [%s]", key)
            return True
        return False

    # ── Backward compatibility helpers ───────────────────────────────────────

    @classmethod
    def load(cls) -> Optional[str]:
        return cls.get_project("default")

    @classmethod
    def save(cls, project_url: str) -> None:
        cls.set_project("default", project_url)

    @classmethod
    def clear(cls, app_name: Optional[str] = None) -> None:
        if app_name:
            cls.delete_project(app_name)
        else:
            if _STORE_FILE.exists():
                _STORE_FILE.unlink()
            if _LEGACY_FILE.exists():
                _LEGACY_FILE.unlink()
            logger.info("Cleared all project stores.")
