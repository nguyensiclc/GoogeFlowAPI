"""
ImageGeneratorService — điều phối việc tạo ảnh, quản lý các provider (Google Flow & Gemini Chat),
chia sẻ chung một phiên trình duyệt Chrome (cùng session / context), quản lý project, tài khoản Google và bật/tắt service.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.async_api import (
    BrowserContext,
    Playwright,
    async_playwright,
)

import config
from app.drivers.google_flow import GoogleFlowDriver
from app.drivers.gemini_chat import GeminiChatDriver
from app.drivers.project_store import ProjectStore
from app.services.account_service import AccountManager

logger = logging.getLogger(__name__)


class GeneratedImage:
    def __init__(
        self,
        prompt: str,
        file_path: Path,
        provider: str = "flow",
        error: Optional[str] = None,
    ) -> None:
        self.prompt = prompt
        self.file_path = file_path
        self.provider = provider
        self.error = error

    @property
    def base64(self) -> Optional[str]:
        if self.error or not self.file_path or not self.file_path.exists():
            return None
        return base64.b64encode(self.file_path.read_bytes()).decode()

    def to_dict(self) -> dict:
        return {
            "prompt": self.prompt,
            "provider": self.provider,
            "file_path": str(self.file_path) if self.file_path else None,
            "base64": self.base64,
            "error": self.error,
        }


class TaskItem:
    """Tác vụ tạo ảnh đơn lẻ, có thể kèm ảnh tham chiếu, tỷ lệ khung hình, app_name và provider."""
    def __init__(
        self,
        prompt: str,
        reference_images: Optional[List[Path]] = None,
        aspect_ratio: Optional[str] = None,
        app_name: Optional[str] = None,
        project_url: Optional[str] = None,
        provider: Optional[str] = None,
    ):
        self.prompt = prompt
        self.reference_images = reference_images or []
        self.aspect_ratio = aspect_ratio
        self.app_name = app_name
        self.project_url = project_url
        self.provider = (provider or config.DEFAULT_PROVIDER).strip().lower()


class ImageGeneratorService:
    """
    Quản lý luồng sinh ảnh từ prompt, hỗ trợ đa nhà cung cấp (Google Flow & Gemini Chat)
    hoạt động trong CÙNG một session Chrome (chia sẻ cookies & tài khoản đăng nhập).
    """

    def __init__(self, provider: Optional[str] = None) -> None:
        self.provider = (provider or config.DEFAULT_PROVIDER).strip().lower()
        self.is_enabled: bool = True  # Trạng thái bật/tắt của service

        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None

        self._flow_driver: Optional[GoogleFlowDriver] = None
        self._gemini_driver: Optional[GeminiChatDriver] = None

        self.total_generated: int = 0

    @property
    def _driver(self) -> Optional[GoogleFlowDriver]:
        """Tương thích ngược cho các lời gọi project management tới Flow driver."""
        return self._flow_driver

    # ── Shared Browser Session Lifecycle ─────────────────────────────────────

    async def _ensure_session(self) -> None:
        """Đảm bảo persistent context của Chrome và các driver đều đã sẵn sàng."""
        if self._context and self._flow_driver and self._gemini_driver:
            return

        active_account = AccountManager.get_active_account()
        profile_dir = active_account["profile_dir"]
        logger.info("Đang khởi tạo phiên Chrome dùng chung với profile: %s …", profile_dir)

        if not self._playwright:
            self._playwright = await async_playwright().start()

        if not self._context:
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                executable_path=config.CHROME_EXECUTABLE,
                headless=config.HEADLESS,
                args=[
                    "--start-maximized",
                    f"--profile-directory={config.CHROME_PROFILE}",
                    "--disable-blink-features=AutomationControlled",
                ],
                no_viewport=True,
            )

        if not self._flow_driver:
            self._flow_driver = GoogleFlowDriver(context=self._context)
            try:
                await self._flow_driver.start()
            except Exception as exc:
                logger.warning("Chưa thể khởi tạo tab Flow: %s", exc)

        if not self._gemini_driver:
            self._gemini_driver = GeminiChatDriver(context=self._context)
            try:
                await self._gemini_driver.start()
            except Exception as exc:
                logger.warning("Chưa thể khởi tạo tab Gemini: %s", exc)

    async def startup(self) -> None:
        logger.info("ImageGeneratorService khởi động (enabled=%s) …", self.is_enabled)
        try:
            await self._ensure_session()
            active_account = AccountManager.get_active_account()
            logger.info("Dịch vụ đã sẵn sàng cho cả Flow và Gemini với tài khoản [%s]", active_account["name"])
        except Exception as exc:
            logger.warning("Chưa thể khởi chạy phiên Chrome lúc startup (có thể Chrome đang mở): %s", exc)

    async def shutdown(self) -> None:
        if self._flow_driver:
            await self._flow_driver.stop()
            self._flow_driver = None

        if self._gemini_driver:
            await self._gemini_driver.stop()
            self._gemini_driver = None

        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None

        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

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
        1. Dừng toàn bộ phiên trình duyệt hiện tại.
        2. Kích hoạt tài khoản mới trong AccountManager.
        3. Khởi động lại phiên trình duyệt mới với profile của tài khoản đó.
        """
        logger.info("Đang chuyển sang tài khoản ID: %s …", account_id)
        await self.shutdown()

        account = AccountManager.switch_account(account_id)
        await self._ensure_session()

        logger.info("Đã chuyển thành công sang tài khoản: %s", account["name"])
        return account

    # ── Image generation ─────────────────────────────────────────────────────

    async def generate_tasks(self, tasks: List[TaskItem]) -> List[GeneratedImage]:
        """Tạo ảnh tuần tự cho danh sách các TaskItem, tự động định tuyến đến Flow hoặc Gemini."""
        if not self.is_enabled:
            raise RuntimeError("Dịch vụ tạo ảnh đang tạm TẮT từ trang Cài đặt. Vui lòng bật lại.")

        await self._ensure_session()

        results: List[GeneratedImage] = []
        for idx, task in enumerate(tasks, 1):
            ref_info = f" (+ {len(task.reference_images)} ảnh tham chiếu)" if task.reference_images else ""
            app_info = f" [App: {task.app_name}]" if task.app_name else ""
            provider_target = (task.provider or self.provider or config.DEFAULT_PROVIDER).strip().lower()

            is_gemini = provider_target in ("gemini", "gemini_chat")
            provider_name = "Gemini Chat" if is_gemini else "Google Flow"

            logger.info(
                "[%d/%d]%s [%s] Đang tạo ảnh cho: %s%s",
                idx,
                len(tasks),
                app_info,
                provider_name,
                task.prompt[:80],
                ref_info,
            )

            try:
                if is_gemini:
                    if not self._gemini_driver or not self._gemini_driver._page:
                        self._gemini_driver = GeminiChatDriver(context=self._context)
                        await self._gemini_driver.start()

                    path = await self._gemini_driver.generate_image(
                        prompt=task.prompt,
                        aspect_ratio=task.aspect_ratio,
                        reference_images=task.reference_images,
                        app_name=task.app_name,
                    )
                else:
                    if not self._flow_driver or not self._flow_driver._page:
                        self._flow_driver = GoogleFlowDriver(context=self._context)
                        await self._flow_driver.start()

                    path = await self._flow_driver.generate_image(
                        prompt=task.prompt,
                        aspect_ratio=task.aspect_ratio,
                        reference_images=task.reference_images,
                        app_name=task.app_name,
                        project_url=task.project_url,
                    )

                results.append(GeneratedImage(prompt=task.prompt, file_path=path, provider=provider_target))
                self.total_generated += 1
                logger.info("[%d/%d] ✓ Thành công [%s]: %s", idx, len(tasks), provider_name, path)

            except Exception as exc:
                logger.error("[%d/%d] ✗ Thất bại [%s]: %s", idx, len(tasks), provider_name, exc)
                results.append(
                    GeneratedImage(
                        prompt=task.prompt,
                        file_path=Path(""),
                        provider=provider_target,
                        error=str(exc),
                    )
                )

        return results

    async def generate(
        self,
        prompts: List[str],
        aspect_ratio: Optional[str] = None,
        reference_images: Optional[List[Path]] = None,
        app_name: Optional[str] = None,
        project_url: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> List[GeneratedImage]:
        """Tạo ảnh tuần tự cho từng prompt (tương thích ngược với lời gọi cũ)."""
        tasks = [
            TaskItem(
                prompt=p,
                aspect_ratio=aspect_ratio,
                reference_images=reference_images,
                app_name=app_name,
                project_url=project_url,
                provider=provider,
            )
            for p in prompts
        ]
        return await self.generate_tasks(tasks)

    # ── Project management (Google Flow) ─────────────────────────────────────

    def get_project_info(self, app_name: str = "default") -> dict:
        projects = ProjectStore.list_projects()
        entry = projects.get(app_name or "default")
        if entry:
            return entry
        return {
            "project_url": self._flow_driver._project_url if self._flow_driver else None,
            "created_at": None,
        }

    def list_projects(self) -> Dict[str, dict]:
        return ProjectStore.list_projects()

    def delete_project(self, app_name: str) -> bool:
        return ProjectStore.delete_project(app_name)

    async def switch_project(self, project_url: str, app_name: str = "default") -> dict:
        if not self._flow_driver or not self._flow_driver._page:
            await self._ensure_session()
        logger.info("Chuyển sang project [%s]: %s", app_name, project_url)
        ProjectStore.set_project(app_name, project_url)
        self._flow_driver._project_url = project_url
        await self._flow_driver._ensure_and_navigate_app_project(
            self._flow_driver._page,
            app_name=app_name,
            project_url=project_url,
        )
        return self.get_project_info(app_name)

    async def create_new_project(self, app_name: str = "default") -> dict:
        if not self._flow_driver or not self._flow_driver._page:
            await self._ensure_session()
        logger.info("Tạo project mới trên Flow cho app [%s] …", app_name)
        new_url = await self._flow_driver._create_new_project(self._flow_driver._page)
        ProjectStore.set_project(app_name, new_url)
        self._flow_driver._project_url = new_url
        return self.get_project_info(app_name)

    # ── System status for UI ─────────────────────────────────────────────────

    def get_system_status(self) -> Dict[str, Any]:
        """Trả về toàn bộ trạng thái hệ thống cho trang cài đặt UI."""
        active_account = AccountManager.get_active_account()
        accounts = AccountManager.list_accounts()
        project_info = self.get_project_info()

        flow_ready = (self._flow_driver is not None and self._flow_driver._page is not None)
        gemini_ready = (self._gemini_driver is not None and self._gemini_driver._page is not None)

        return {
            "is_enabled": self.is_enabled,
            "driver_ready": (flow_ready or gemini_ready),
            "flow_ready": flow_ready,
            "gemini_ready": gemini_ready,
            "default_provider": config.DEFAULT_PROVIDER,
            "active_account": active_account,
            "accounts": accounts,
            "project_url": project_info.get("project_url"),
            "projects": ProjectStore.list_projects(),
            "total_generated": self.total_generated,
        }
