"""
AccountManager — Quản lý nhiều tài khoản Google / Chrome profiles cho service.

Cấu trúc lưu trữ:
  - .accounts.json: danh sách tài khoản và tài khoản đang kích hoạt.
  - .chrome_profiles/<account_id>/: thư mục user-data-dir riêng cho từng tài khoản.
"""

from __future__ import annotations

import json
import logging
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import config

logger = logging.getLogger(__name__)

ACCOUNTS_FILE = Path(__file__).parent.parent.parent / ".accounts.json"
PROFILES_BASE_DIR = Path(__file__).parent.parent.parent / ".chrome_profiles"


class AccountManager:
    """Quản lý các tài khoản Google với profile Chrome riêng biệt."""

    @classmethod
    def _ensure_init(cls) -> None:
        """Đảm bảo file .accounts.json và thư mục profiles tồn tại."""
        PROFILES_BASE_DIR.mkdir(parents=True, exist_ok=True)

        if not ACCOUNTS_FILE.exists():
            # Tự động tạo tài khoản mặc định
            # Nếu đã có thư mục .chrome_profile cũ thì tận dụng
            legacy_profile = Path(__file__).parent.parent.parent / ".chrome_profile"
            default_dir = legacy_profile if legacy_profile.exists() else (PROFILES_BASE_DIR / "default")
            default_dir.mkdir(parents=True, exist_ok=True)

            initial_data = {
                "active_account_id": "default",
                "accounts": [
                    {
                        "id": "default",
                        "name": "Tài khoản 1 (Mặc định)",
                        "profile_dir": str(default_dir.resolve()),
                        "project_url": None,
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                    }
                ],
            }
            cls._save_data(initial_data)

    @classmethod
    def _load_data(cls) -> Dict[str, Any]:
        cls._ensure_init()
        try:
            return json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("Lỗi đọc file .accounts.json: %s", exc)
            return {"active_account_id": "default", "accounts": []}

    @classmethod
    def _save_data(cls, data: Dict[str, Any]) -> None:
        ACCOUNTS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def list_accounts(cls) -> List[Dict[str, Any]]:
        """Trả về danh sách tất cả tài khoản, kèm cờ is_active."""
        data = cls._load_data()
        active_id = data.get("active_account_id", "default")
        result = []
        for acc in data.get("accounts", []):
            item = dict(acc)
            item["is_active"] = (acc["id"] == active_id)
            result.append(item)
        return result

    @classmethod
    def get_active_account(cls) -> Dict[str, Any]:
        """Lấy tài khoản đang được kích hoạt."""
        data = cls._load_data()
        active_id = data.get("active_account_id", "default")
        for acc in data.get("accounts", []):
            if acc["id"] == active_id:
                item = dict(acc)
                item["is_active"] = True
                return item

        # Fallback nếu không khớp
        accounts = data.get("accounts", [])
        if accounts:
            item = dict(accounts[0])
            item["is_active"] = True
            return item

        cls._ensure_init()
        return cls.get_active_account()

    @classmethod
    def get_account_by_id(cls, account_id: str) -> Optional[Dict[str, Any]]:
        data = cls._load_data()
        for acc in data.get("accounts", []):
            if acc["id"] == account_id:
                return dict(acc)
        return None

    @classmethod
    def create_account(cls, name: str) -> Dict[str, Any]:
        """Tạo một tài khoản mới với thư mục profile riêng."""
        cls._ensure_init()
        data = cls._load_data()

        acc_id = uuid.uuid4().hex[:8]
        profile_dir = (PROFILES_BASE_DIR / acc_id).resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)

        new_account = {
            "id": acc_id,
            "name": name.strip() or f"Tài khoản {len(data.get('accounts', [])) + 1}",
            "profile_dir": str(profile_dir),
            "project_url": None,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }

        data.setdefault("accounts", []).append(new_account)
        cls._save_data(data)
        logger.info("Đã tạo tài khoản mới: %s (%s)", new_account["name"], acc_id)
        return new_account

    @classmethod
    def switch_account(cls, account_id: str) -> Dict[str, Any]:
        """Đặt tài khoản mục tiêu làm active account."""
        data = cls._load_data()
        found = False
        target = None
        for acc in data.get("accounts", []):
            if acc["id"] == account_id:
                found = True
                target = acc
                break

        if not found:
            raise ValueError(f"Không tìm thấy tài khoản với ID: {account_id}")

        data["active_account_id"] = account_id
        cls._save_data(data)
        logger.info("Đã chuyển sang tài khoản: %s", target["name"])
        return target

    @classmethod
    def delete_account(cls, account_id: str) -> bool:
        """Xóa một tài khoản (không được xóa tài khoản duy nhất còn lại)."""
        data = cls._load_data()
        accounts = data.get("accounts", [])

        if len(accounts) <= 1:
            raise ValueError("Không thể xóa tài khoản duy nhất còn lại.")

        new_list = [acc for acc in accounts if acc["id"] != account_id]
        if len(new_list) == len(accounts):
            return False

        if data.get("active_account_id") == account_id:
            data["active_account_id"] = new_list[0]["id"]

        data["accounts"] = new_list
        cls._save_data(data)
        logger.info("Đã xóa tài khoản ID: %s", account_id)
        return True

    @classmethod
    def launch_login_browser(cls, account_id: str) -> None:
        """
        Mở cửa sổ Chrome độc lập cho tài khoản này để người dùng đăng nhập Google và vào Flow.
        """
        account = cls.get_account_by_id(account_id)
        if not account:
            raise ValueError(f"Không tìm thấy tài khoản: {account_id}")

        profile_dir = Path(account["profile_dir"])
        profile_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            config.CHROME_EXECUTABLE,
            f"--user-data-dir={profile_dir}",
            f"--profile-directory={config.CHROME_PROFILE}",
            "--no-first-run",
            "--no-default-browser-check",
            "https://accounts.google.com",
            "https://flow.google.com",
        ]
        logger.info("Mở trình duyệt đăng nhập cho %s...", account["name"])
        subprocess.Popen(cmd)
