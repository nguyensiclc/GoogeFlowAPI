"""
ImageGeneratorService — điều phối việc tạo ảnh, quản lý các provider (Google Flow & Gemini Chat),
chia sẻ chung một phiên trình duyệt Chrome (cùng session / context), quản lý project, tài khoản Google và bật/tắt service.
"""

from __future__ import annotations

import asyncio
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

    def _is_session_alive(self) -> bool:
        """Kiểm tra xem BrowserContext hiện tại có còn hoạt động hay không."""
        if not self._context:
            return False
        try:
            if hasattr(self._context, "is_closed") and self._context.is_closed():
                return False
            _ = self._context.pages
            return True
        except Exception:
            return False

    async def _ensure_session(self, force: bool = False) -> None:
        """Đảm bảo persistent context của Chrome và tab Flow đã sẵn sàng (tab Gemini chỉ mở khi cần, xem _ensure_gemini)."""
        if not force and self._is_session_alive() and self._flow_driver:
            return

        if self._context:
            logger.info("Phiên trình duyệt trước đó không còn khả dụng, đang khởi tạo lại …")
            await self.shutdown()

        active_account = AccountManager.get_active_account()
        profile_dir = active_account["profile_dir"]
        logger.info("Đang khởi tạo phiên Chrome dùng chung với profile: %s …", profile_dir)

        if not self._playwright:
            self._playwright = await async_playwright().start()

        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            executable_path=config.CHROME_EXECUTABLE,
            headless=config.HEADLESS,
            args=[
                "--start-maximized",
                f"--profile-directory={config.CHROME_PROFILE}",
                "--disable-blink-features=AutomationControlled",
                # parallel tabs: keep background tabs running at full speed
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
            ],
            no_viewport=True,
        )
        self._extra_flow = []

        self._flow_driver = GoogleFlowDriver(context=self._context)
        try:
            await self._flow_driver.start()
        except Exception as exc:
            logger.warning("Chưa thể khởi tạo tab Flow: %s", exc)

        # Tab Gemini không mở sẵn: chỉ mở khi có yêu cầu tạo ảnh bằng Gemini (_ensure_gemini).

    async def _ensure_gemini(self) -> None:
        """Mở tab Gemini Chat (lần đầu có yêu cầu dùng Gemini, hoặc khi tab đã bị đóng)."""
        if not self._is_session_alive():
            await self._ensure_session(force=True)
        if not self._gemini_driver or not self._gemini_driver._page or self._gemini_driver._page.is_closed():
            self._gemini_driver = GeminiChatDriver(context=self._context)
            await self._gemini_driver.start()

    async def startup(self) -> None:
        logger.info("ImageGeneratorService khởi động (enabled=%s) …", self.is_enabled)
        try:
            await self._ensure_session()
            active_account = AccountManager.get_active_account()
            logger.info("Dịch vụ đã sẵn sàng (Flow; Gemini mở khi cần) với tài khoản [%s]", active_account["name"])
        except Exception as exc:
            logger.warning("Chưa thể khởi chạy phiên Chrome lúc startup (có thể Chrome đang mở): %s", exc)

    async def shutdown(self) -> None:
        for d in getattr(self, "_extra_flow", []) or []:
            try:
                await d.stop()
            except Exception:
                pass
        self._extra_flow = []
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
        """One browser, one operation at a time: generation and project deletion never overlap."""
        if not hasattr(self, "_op_lock"):
            self._op_lock = asyncio.Lock()
        async with self._op_lock:
            return await self._generate_tasks(tasks)

    async def _generate_tasks(self, tasks: List[TaskItem]) -> List[GeneratedImage]:
        """Tạo ảnh tuần tự cho danh sách các TaskItem, tự động định tuyến đến Flow hoặc Gemini."""
        if not self.is_enabled:
            raise RuntimeError("Dịch vụ tạo ảnh đang tạm TẮT từ trang Cài đặt. Vui lòng bật lại.")

        await self._ensure_session()

        flow_only = all((t.provider or self.provider or config.DEFAULT_PROVIDER).strip().lower()
                        not in ("gemini", "gemini_chat") for t in tasks)
        workers = min(config.FLOW_PARALLEL, len(tasks))
        if flow_only and workers > 1:
            return await self._generate_parallel(tasks, workers)

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
                    await self._ensure_gemini()

                    path = await self._gemini_driver.generate_image(
                        prompt=task.prompt,
                        aspect_ratio=task.aspect_ratio,
                        reference_images=task.reference_images,
                        app_name=task.app_name,
                    )
                else:
                    if not self._flow_driver or not self._flow_driver._page or self._flow_driver._page.is_closed():
                        if not self._is_session_alive():
                            await self._ensure_session(force=True)
                        else:
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
                err_msg = str(exc)
                # Tự động hồi phục nếu browser hoặc page bị đóng trong lúc chạy
                if ("closed" in err_msg.lower() or "target page" in err_msg.lower()) and not getattr(task, "_retried", False):
                    logger.warning("Trình duyệt bị ngắt kết nối (%s), tự động khởi động lại session và thử lại …", err_msg)
                    setattr(task, "_retried", True)
                    try:
                        await self._ensure_session(force=True)
                        if is_gemini:
                            await self._ensure_gemini()
                            path = await self._gemini_driver.generate_image(
                                prompt=task.prompt,
                                aspect_ratio=task.aspect_ratio,
                                reference_images=task.reference_images,
                                app_name=task.app_name,
                            )
                        else:
                            path = await self._flow_driver.generate_image(
                                prompt=task.prompt,
                                aspect_ratio=task.aspect_ratio,
                                reference_images=task.reference_images,
                                app_name=task.app_name,
                                project_url=task.project_url,
                            )
                        results.append(GeneratedImage(prompt=task.prompt, file_path=path, provider=provider_target))
                        self.total_generated += 1
                        logger.info("[%d/%d] ✓ Thành công sau khi khôi phục [%s]: %s", idx, len(tasks), provider_name, path)
                        continue
                    except Exception as retry_exc:
                        exc = retry_exc

                logger.error("[%d/%d] ✗ Thất bại [%s]: %s", idx, len(tasks), provider_name, exc)
                results.append(
                    GeneratedImage(
                        prompt=task.prompt,
                        file_path=Path(""),
                        provider=provider_target,
                        error=str(exc),
                    )
                )
                # Google refused ("unusual activity"): do not send the remaining prompts, every one would be refused
                # too and keeps the account flagged. Mark them as not attempted.
                if "FLOW_REFUSED" in str(exc):
                    for rest in tasks[idx:]:
                        results.append(GeneratedImage(prompt=rest.prompt, file_path=Path(""), provider=provider_target,
                                                      error="FLOW_REFUSED: không gửi vì Google vừa từ chối ảnh trước"))
                    logger.error("Dừng lô vì Google Flow báo hoạt động bất thường.")
                    break

        return results

    # ── Parallel Flow tabs ───────────────────────────────────────────────────

    async def _flow_workers(self, n: int) -> List[GoogleFlowDriver]:
        """The main Flow tab plus n-1 extra tabs (kept open between calls, reopened if closed)."""
        if not self._flow_driver or not self._flow_driver._page or self._flow_driver._page.is_closed():
            self._flow_driver = GoogleFlowDriver(context=self._context)
            await self._flow_driver.start()
        extra = getattr(self, "_extra_flow", None)
        if extra is None:
            extra = self._extra_flow = []
        for i in range(n - 1):
            d = extra[i] if i < len(extra) else None
            if d is None or not d._page or d._page.is_closed():
                d = GoogleFlowDriver(context=self._context, own_page=True)
                await d.start()
                if i < len(extra):
                    extra[i] = d
                else:
                    extra.append(d)
        return [self._flow_driver] + extra[: n - 1]

    @staticmethod
    def _worker_app(app_name: Optional[str], k: int) -> Optional[str]:
        """Tab k > 0 works in its own Flow project ("<app>#w<k>"), so tabs never see each other's pictures."""
        if k == 0:
            return app_name
        return f"{app_name or 'default'}#w{k}"

    async def _generate_parallel(self, tasks: List[TaskItem], n: int) -> List[GeneratedImage]:
        """
        n Flow tabs take prompts from one queue. Every tab gets the same prompt text and the same reference images,
        so the pictures stay consistent; each tab keeps its own project (and uploads each reference once there).
        A refusal ("unusual activity") on any tab stops all tabs.
        """
        workers = await self._flow_workers(n)
        logger.info("Tạo %d ảnh với %d tab Flow song song …", len(tasks), len(workers))
        results: List[Optional[GeneratedImage]] = [None] * len(tasks)
        queue: asyncio.Queue = asyncio.Queue()
        for i, t in enumerate(tasks):
            queue.put_nowait((i, t))
        state = {"refused": False}

        async def run(k: int, drv: GoogleFlowDriver) -> None:
            await asyncio.sleep(k * 5)  # stagger the tabs: no burst of simultaneous requests
            while True:
                try:
                    i, t = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                if state["refused"]:
                    results[i] = GeneratedImage(prompt=t.prompt, file_path=Path(""), provider="flow",
                                                error="FLOW_REFUSED: không gửi vì Google vừa từ chối ảnh trước")
                    continue
                logger.info("[tab %d] [%d/%d] Đang tạo: %s", k + 1, i + 1, len(tasks), t.prompt[:70])
                try:
                    path = await drv.generate_image(
                        prompt=t.prompt,
                        aspect_ratio=t.aspect_ratio,
                        reference_images=t.reference_images,
                        app_name=self._worker_app(t.app_name, k),
                        project_url=t.project_url if k == 0 else None,
                    )
                    results[i] = GeneratedImage(prompt=t.prompt, file_path=path, provider="flow")
                    self.total_generated += 1
                    logger.info("[tab %d] ✓ %s", k + 1, path)
                except Exception as exc:
                    logger.error("[tab %d] ✗ %s", k + 1, exc)
                    results[i] = GeneratedImage(prompt=t.prompt, file_path=Path(""), provider="flow", error=str(exc))
                    if "FLOW_REFUSED" in str(exc):
                        state["refused"] = True
                        logger.error("Dừng mọi tab vì Google Flow báo hoạt động bất thường.")

        await asyncio.gather(*(run(k, d) for k, d in enumerate(workers)))
        return [r or GeneratedImage(prompt=t.prompt, file_path=Path(""), provider="flow", error="không chạy")
                for r, t in zip(results, tasks)]

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

    async def delete_flow_project(self, app_name: str) -> dict:
        """Deletes the app's project on Flow (and its parallel-tab projects "<app>#w<k>"), then forgets the links."""
        extra = [k for k in ProjectStore.list_projects() if k.startswith(f"{app_name}#w")]
        rs = [await self._delete_one_flow_project(k) for k in [app_name] + extra]
        return {"success": all(r.get("success") for r in rs),
                "had_project": any(r.get("had_project") for r in rs),
                "remote_deleted": any(r.get("remote_deleted") for r in rs),
                "message": " · ".join(r.get("message", "") for r in rs if r.get("message"))}

    async def _delete_one_flow_project(self, app_name: str) -> dict:
        url = ProjectStore.get_project(app_name)
        if not url:
            return {"success": True, "had_project": False, "remote_deleted": False,
                    "message": f"App '{app_name}' chưa có project trên Flow."}
        if not hasattr(self, "_op_lock"):
            self._op_lock = asyncio.Lock()
        async with self._op_lock:
            await self._ensure_session()
            try:
                await self._flow_driver.delete_project(url)
            except Exception as exc:
                logger.error("Không xoá được project Flow của [%s]: %s", app_name, exc)
                return {"success": False, "had_project": True, "remote_deleted": False, "message": str(exc)}
        ProjectStore.delete_project(app_name)
        return {"success": True, "had_project": True, "remote_deleted": True,
                "message": f"Đã xoá project Flow của '{app_name}'."}

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
