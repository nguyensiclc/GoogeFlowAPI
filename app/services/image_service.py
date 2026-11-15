"""
ImageGeneratorService — điều phối việc tạo ảnh, quản lý project, tài khoản Google và bật/tắt service.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.drivers.google_flow import GoogleFlowDriver
from app.drivers.project_store import ProjectStore
from app.services.account_service import AccountManager

logger = logging.getLogger(__name__)


class GeneratedImage:
    def __init__(self, prompt: str, file_path: Path, error: Optional[str] = None) -> None:
        self.prompt = prompt
        self.file_path = file_path
        self.error = error

    @property
    def base64(self) -> Optional[str]:
        if self.error or not self.file_path or not self.file_path.exists():
            return None
        return base64.b64encode(self.file_path.read_bytes()).decode()

    def to_dict(self) -> dict:
        return {
            "prompt": self.prompt,
            "file_path": str(self.file_path) if self.file_path else None,
            "base64": self.base64,
            "error": self.error,
        }


class TaskItem:
    """Tác vụ tạo ảnh đơn lẻ, có thể kèm danh sách ảnh tham chiếu, tỷ lệ khung hình và app_name."""
    def __init__(
        self,
        prompt: str,
        reference_images: Optional[List[Path]] = None,
        aspect_ratio: Optional[str] = None,
        app_name: Optional[str] = None,
        project_url: Optional[str] = None,
    ):
        self.prompt = prompt
        self.reference_images = reference_images or []
        self.aspect_ratio = aspect_ratio
        self.app_name = app_name
        self.project_url = project_url


class ImageGeneratorService:
    """
    Quản lý luồng sinh ảnh từ prompt, chuyển tài khoản Google và bật/tắt dịch vụ.
    """

    def __init__(self, provider: str = "google_flow") -> None:
        self.provider = provider
        self.is_enabled: bool = True  # Trạng thái bật/tắt của service
        self._driver: Optional[GoogleFlowDriver] = None
        self.total_generated: int = 0

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def startup(self) -> None:
        logger.info("ImageGeneratorService khởi động (enabled=%s) …", self.is_enabled)
        active_account = AccountManager.get_active_account()
        profile_dir = active_account["profile_dir"]

        self._driver = GoogleFlowDriver(user_data_dir=profile_dir)
        try:
            await self._driver.start()
            logger.info("Dịch vụ sẵn sàng với tài khoản [%s]", active_account["name"])
        except Exception as exc:
            logger.warning("Chưa thể khởi chạy driver lúc startup (có thể Chrome đang mở): %s", exc)

    async def shutdown(self) -> None:
        if self._driver:
            await self._driver.stop()
            self._driver = None
        logger.info("ImageGeneratorService đã tắt.")

    # ── Toggle Service (Bật / Tắt) ───────────────────────────────────────────

    async def toggle_service(self, enable: Optional[bool] = None) -> bool:
        """Bật hoặc tắt trạng thái nhận yêu cầu tạo ảnh."""
        if enable is None:
            self.is_enabled = not self.is_enabled
        else:
            self.is_enabled = enable

        logger.info("Trạng thái dịch vụ đã chuyển thành: %s", "BẬT" if self.is_enabled else "TẮT")
        return self.is_enabled

    # ── Switch Google Account ────────────────────────────────────────────────

    async def switch_account(self, account_id: str) -> Dict[str, Any]:
        """
        Chuyển sang một tài khoản Google khác:
        1. Dừng browser session hiện tại.
        2. Kích hoạt tài khoản mới trong AccountManager.
        3. Khởi động browser session mới với profile của tài khoản đó.
        """
        logger.info("Đang chuyển sang tài khoản ID: %s …", account_id)
        account = AccountManager.switch_account(account_id)

        # Tắt driver hiện tại nếu đang chạy
        if self._driver:
            await self._driver.stop()
            self._driver = None

        # Khởi động lại với profile của tài khoản mới
        self._driver = GoogleFlowDriver(user_data_dir=account["profile_dir"])
        await self._driver.start()
        logger.info("Đã chuyển thành công sang tài khoản: %s", account["name"])

        return account

    # ── Image generation ─────────────────────────────────────────────────────

    async def generate_tasks(self, tasks: List[TaskItem]) -> List[GeneratedImage]:
        """Tạo ảnh tuần tự cho danh sách các TaskItem (mỗi task có thể có prompt + ảnh tham chiếu riêng)."""
        if not self.is_enabled:
            raise RuntimeError("Dịch vụ tạo ảnh đang tạm TẮT từ trang Cài đặt. Vui lòng bật lại.")

        if not self._driver or not self._driver._page:
            logger.info("Driver chưa sẵn sàng, đang khởi động lại …")
            active_account = AccountManager.get_active_account()
            self._driver = GoogleFlowDriver(user_data_dir=active_account["profile_dir"])
            await self._driver.start()

        results: List[GeneratedImage] = []
        for idx, task in enumerate(tasks, 1):
            ref_info = f" (+ {len(task.reference_images)} ảnh tham chiếu)" if task.reference_images else ""
            app_info = f" [App: {task.app_name}]" if task.app_name else ""
            logger.info("[%d/%d]%s Đang tạo ảnh cho: %s%s", idx, len(tasks), app_info, task.prompt[:80], ref_info)
            try:
                path = await self._driver.generate_image(
                    prompt=task.prompt,
                    aspect_ratio=task.aspect_ratio,
                    reference_images=task.reference_images,
                    app_name=task.app_name,
                    project_url=task.project_url,
                )
                results.append(GeneratedImage(prompt=task.prompt, file_path=path))
                self.total_generated += 1
                logger.info("[%d/%d] ✓ Thành công: %s", idx, len(tasks), path)
            except Exception as exc:
                logger.error("[%d/%d] ✗ Thất bại: %s", idx, len(tasks), exc)
                results.append(GeneratedImage(prompt=task.prompt, file_path=Path(""), error=str(exc)))

        return results

    async def generate(
        self,
        prompts: List[str],
        aspect_ratio: Optional[str] = None,
        reference_images: Optional[List[Path]] = None,
        app_name: Optional[str] = None,
        project_url: Optional[str] = None,
    ) -> List[GeneratedImage]:
        """Tạo ảnh tuần tự cho từng prompt (tương thích ngược với lời gọi cũ)."""
        tasks = [
            TaskItem(
                prompt=p,
                aspect_ratio=aspect_ratio,
                reference_images=reference_images,
                app_name=app_name,
                project_url=project_url,
            )
            for p in prompts
        ]
        return await self.generate_tasks(tasks)

    # ── Project management ───────────────────────────────────────────────────

    def get_project_info(self, app_name: str = "default") -> dict:
        projects = ProjectStore.list_projects()
        entry = projects.get(app_name or "default")
        if entry:
            return entry
        return {
            "project_url": self._driver._project_url if self._driver else None,
            "created_at": None,
        }

    def list_projects(self) -> Dict[str, dict]:
        return ProjectStore.list_projects()

    def delete_project(self, app_name: str) -> bool:
        return ProjectStore.delete_project(app_name)

    async def switch_project(self, project_url: str, app_name: str = "default") -> dict:
        if not self._driver:
            raise RuntimeError("Driver chưa khởi động.")
        logger.info("Chuyển sang project [%s]: %s", app_name, project_url)
        ProjectStore.set_project(app_name, project_url)
        self._driver._project_url = project_url
        await self._driver._ensure_and_navigate_app_project(
            self._driver._page,
            app_name=app_name,
            project_url=project_url,
        )
        return self.get_project_info(app_name)

    async def create_new_project(self, app_name: str = "default") -> dict:
        if not self._driver:
            raise RuntimeError("Driver chưa khởi động.")
        logger.info("Tạo project mới trên Flow cho app [%s] …", app_name)
        new_url = await self._driver._create_new_project(self._driver._page)
        ProjectStore.set_project(app_name, new_url)
        self._driver._project_url = new_url
        return self.get_project_info(app_name)

    # ── System status for UI ─────────────────────────────────────────────────

    def get_system_status(self) -> Dict[str, Any]:
        """Trả về toàn bộ trạng thái hệ thống cho trang cài đặt UI."""
        active_account = AccountManager.get_active_account()
        accounts = AccountManager.list_accounts()
        project_info = self.get_project_info()

        return {
            "is_enabled": self.is_enabled,
            "driver_ready": (self._driver is not None and self._driver._page is not None),
            "active_account": active_account,
            "accounts": accounts,
            "project_url": project_info.get("project_url"),
            "projects": ProjectStore.list_projects(),
            "total_generated": self.total_generated,
        }
