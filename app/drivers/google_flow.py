"""
GoogleFlowDriver — Playwright automation for flow.google.com image generation.

Flow hoạt động theo Project. Driver tự:
  1. Kiểm tra xem đã có project chưa (đọc .flow_project.json)
  2. Nếu chưa → vào flow.google.com → click "+ Dự án mới" → lưu URL
  3. Nhập prompt vào ProseMirror editor
  4. Click nút submit (→)
  5. Chờ ảnh mới xuất hiện → download → trả path

DOM structure (flow.google.com, Nov 2026):
  - New project btn : button có text "Dự án mới" trên trang chủ (xuất hiện khi hover)
  - Prompt input    : div[role="textbox"][contenteditable="true"] — ProseMirror
  - Submit btn      : nút cuối trong div.button-controls
  - Generated img   : flow-image-tile img  /  img[src*="flow.google.com/media"]
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

import config
from app.drivers.project_store import ProjectStore

logger = logging.getLogger(__name__)


# ─── Selectors ───────────────────────────────────────────────────────────────
class _Sel:
    # Nút "+ Dự án mới" trên trang chủ (xuất hiện khi hover vùng project)
    NEW_PROJECT_BTN = 'button:has-text("Dự án mới")'

    # Prompt input — ProseMirror contenteditable
    PROMPT = 'div[role="textbox"][contenteditable="true"]'

    # Nút submit "→" — nút cuối trong thanh điều khiển phía dưới
    SUBMIT = "div.button-controls button:last-child"
    SUBMIT_FALLBACKS = [
        'button[aria-label*="Tạo"]',
        'button[aria-label*="Create"]',
        'button[aria-label*="Send"]',
        '[class*="send-button"]',
    ]

    # Ảnh đã generate — src từ /asb/ hoặc /media/ hoặc có data-media-id
    GENERATED_IMG = (
        "flow-image-tile img[data-media-id], "
        "flow-image-tile img[src*='/asb/'], "
        "flow-image-tile img[src*='/media/'], "
        "flow-image-tile img"
    )

    # Hover target để nút New Project hiện ra (vùng project grid)
    PROJECT_GRID_AREA = "div.projects-grid, main, body"

    # Cài đặt trong ô prompt: tỷ lệ khung hình + số ảnh mỗi lần tạo (nhãn tiếng Việt / tiếng Anh)
    SETTINGS_BTN_NAMES = ["Cài đặt", "Settings"]
    ASPECT_GROUP_LABELS = ["Tỷ lệ khung hình mặc định của tính năng tạo hình ảnh", "Default image aspect ratio"]
    COUNT_GROUP_LABELS = ["Số lượng kết quả đầu ra mặc định của quá trình tạo hình ảnh", "Default number of image outputs"]


# Tỷ lệ Flow hỗ trợ cho ảnh
ASPECT_RATIOS = ("16:9", "4:3", "1:1", "3:4", "9:16")


class GoogleFlowDriver:
    """
    Quản lý một Playwright browser session để tạo ảnh trên flow.google.com.
    - Tự tạo / tái sử dụng Project (lưu ở .flow_project.json).
    - Mỗi lần generate: nhập prompt → submit → chờ ảnh → download.
    """

    def __init__(self, user_data_dir: Optional[str] = None) -> None:
        self.user_data_dir: str = user_data_dir or config.CHROME_USER_DATA_DIR
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._project_url: Optional[str] = None
        # Tỷ lệ đã chỉnh cho project hiện tại (để không mở Cài đặt mỗi ảnh)
        self._aspect_for: Optional[tuple[str, str]] = None

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        logger.info("Starting GoogleFlowDriver with profile: %s …", self.user_data_dir)
        self._playwright = await async_playwright().start()

        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=self.user_data_dir,
            executable_path=config.CHROME_EXECUTABLE,
            headless=config.HEADLESS,
            args=[
                "--start-maximized",
                f"--profile-directory={config.CHROME_PROFILE}",
                "--disable-blink-features=AutomationControlled",
            ],
            no_viewport=True,
        )

        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()

        # Đảm bảo có project hợp lệ ngay khi start
        self._project_url = await self._ensure_project(self._page)
        logger.info("Driver ready. Project: %s", self._project_url)

    async def stop(self) -> None:
        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("GoogleFlowDriver stopped.")

    async def __aenter__(self) -> "GoogleFlowDriver":
        await self.start()
        return self

    async def __aexit__(self, *_) -> None:
        await self.stop()

    # ── Public API ───────────────────────────────────────────────────────────

    async def generate_image(
        self,
        prompt: str,
        aspect_ratio: Optional[str] = None,
        reference_images: Optional[List[Path]] = None,
        app_name: Optional[str] = None,
        project_url: Optional[str] = None,
    ) -> Path:
        """
        Tạo ảnh từ prompt (có thể kèm ảnh tham chiếu). Trả về path file ảnh đã lưu.
        app_name: tên ứng dụng gọi API (tự động phân vùng Project riêng trên Flow).
        project_url: URL project chỉ định trực tiếp (nếu có).
        aspect_ratio: "16:9" | "4:3" | "1:1" | "3:4" | "9:16" — None = giữ cài đặt hiện tại của project.
        reference_images: danh sách đường dẫn các file ảnh tham chiếu cần tải lên.
        """
        if self._page is None:
            raise RuntimeError("Driver chưa được start.")

        page = self._page
        output_path = config.OUTPUT_DIR / f"{uuid.uuid4().hex}.png"

        # 1. Điều hướng đến project riêng của app (tự tạo nếu chưa có)
        await self._ensure_and_navigate_app_project(page, app_name=app_name, project_url=project_url)

        # 1b. Chỉnh tỷ lệ khung hình + 1 ảnh mỗi lần tạo (chỉ khi đổi tỷ lệ hoặc đổi project)
        if aspect_ratio:
            await self._ensure_aspect(page, aspect_ratio)

        # 2. Thu thập danh sách ID / URL các ảnh hiện có trước khi sinh ảnh mới
        existing_ids = await self._get_existing_media_ids(page)
        logger.info("Hiện có %d ảnh trong project trước khi submit.", len(existing_ids))

        # 2b. Nếu có ảnh tham chiếu, tải lên trước khi gõ prompt
        if reference_images:
            for ref_path in reference_images:
                await self._upload_reference_image(page, ref_path)

        # 3. Nhập prompt
        await self._type_prompt(page, prompt)

        # 4. Bấm submit
        await self._click_submit(page)

        # 5. Chờ chính xác thời điểm ảnh mới hoàn thành (Network + DOM + Image Loaded)
        logger.info("Đang theo dõi tiến trình tạo ảnh (tối đa %ds) …", config.IMAGE_GENERATION_TIMEOUT)
        new_img_locator = await self._wait_for_new_image(page, existing_ids)

        # 6. Lưu ảnh vừa tạo về disk
        await self._save_image_element(page, new_img_locator, output_path)
        logger.info("✅ Ảnh đã tạo và lưu thành công tại: %s", output_path)
        return output_path

    # ── Reference Image Upload Helpers ───────────────────────────────────────

    async def _find_add_media_button(self, page: Page):
        """Tìm nút '+' ở thanh prompt để mở menu thêm thành phần."""
        editor = page.locator(_Sel.PROMPT).first
        btn_handle = await editor.evaluate_handle("""(ed) => {
            let box = ed.parentElement;
            for (let i = 0; i < 8 && box; i++, box = box.parentElement) {
                const btns = Array.from(box.querySelectorAll('button'));
                const plusBtn = btns.find(b => {
                    const t = (b.innerText || '').trim();
                    const a = (b.getAttribute('aria-label') || '').toLowerCase();
                    return t === '+' || t.includes('add') || a.includes('thêm') || a.includes('tải lên') || a.includes('add');
                });
                if (plusBtn) return plusBtn;
            }
            return null;
        }""")
        if btn_handle and btn_handle.as_element():
            return btn_handle.as_element()

        for sel in [
            "div.bottom-controls button:first-child",
            "button:has-text('+')",
            "button[aria-label*='Thêm' i]",
        ]:
            loc = page.locator(sel).first
            if await loc.is_visible():
                return loc
        raise RuntimeError("Không tìm thấy nút '+' trên thanh prompt của Google Flow.")

    async def _upload_reference_image(self, page: Page, image_path: Path) -> None:
        """
        Thực hiện quy trình:
        1. Bấm dấu '+' ở thanh prompt
        2. Bấm vào 'Tải nội dung...'
        3. Chọn file ảnh từ image_path
        4. Chờ ảnh tải lên và gắn vào prompt bar
        """
        logger.info("Đang nạp ảnh tham chiếu lên Flow: %s", image_path.name)
        if not image_path.exists():
            raise FileNotFoundError(f"Không tìm thấy file ảnh tham chiếu: {image_path}")

        # 1. Bấm nút '+'
        add_btn = await self._find_add_media_button(page)
        await add_btn.click()
        await page.wait_for_timeout(800)

        # 2. Tìm nút 'Tải nội dung...' trong menu vừa mở
        upload_locators = [
            page.locator("text='Tải nội dung'").last,
            page.locator("button:has-text('Tải nội dung')").last,
            page.locator("[aria-label*='Tải nội dung' i]").last,
            page.locator("div:has-text('Tải nội dung')").last,
        ]

        upload_target = None
        for loc in upload_locators:
            try:
                if await loc.is_visible(timeout=2_000):
                    upload_target = loc
                    break
            except Exception:
                continue

        if not upload_target:
            upload_target = page.locator("text='Tải nội dung'").last

        try:
            async with page.expect_file_chooser(timeout=8_000) as fc_info:
                await upload_target.click()
            file_chooser = await fc_info.value
            await file_chooser.set_files(str(image_path.resolve()))
            logger.info("Đã chọn file ảnh qua file chooser thành công: %s", image_path.name)
        except Exception as exc:
            logger.warning("Không mở được file chooser (%s), thử set trực tiếp vào input[type=file] …", exc)
            file_inputs = page.locator("input[type='file']")
            if await file_inputs.count() > 0:
                await file_inputs.last.set_input_files(str(image_path.resolve()))
                logger.info("Đã gán file trực tiếp vào input[type=file].")
            else:
                raise RuntimeError(f"Không thể upload ảnh tham chiếu '{image_path.name}' lên Flow: {exc}")

        # 3. Chờ ảnh tải lên xong và xuất hiện trong thanh prompt
        logger.info("Chờ ảnh tham chiếu tải lên xong...")
        await page.wait_for_timeout(3_000)
        try:
            if await page.locator("text='Tất cả'").is_visible():
                await page.keyboard.press("Escape")
        except Exception:
            pass

    # ── Media Tracking Helpers ────────────────────────────────────────────────

    async def _get_existing_media_ids(self, page: Page) -> set[str]:
        """Lấy tập hợp các data-media-id hoặc src của ảnh hiện có."""
        try:
            return set(
                await page.evaluate(
                    """() => {
                        const imgs = Array.from(document.querySelectorAll('flow-image-tile img'));
                        return imgs.map(img => img.getAttribute('data-media-id') || img.src).filter(Boolean);
                    }"""
                )
            )
        except Exception:
            return set()

    # ── Wait for image (Độ chính xác cao) ─────────────────────────────────────

    async def _wait_for_new_image(self, page: Page, existing_ids: set[str]):
        """
        Nhận biết CHÍNH XÁC thời điểm ảnh tạo xong:
        1. Một thẻ <img> mới xuất hiện trong <flow-image-tile> với ID chưa từng có trước đó.
        2. Thẻ <img> đó đã tải xong hoàn toàn dữ liệu (img.complete === true VÀ img.naturalWidth > 0).
        """
        deadline = time.monotonic() + config.IMAGE_GENERATION_TIMEOUT

        while time.monotonic() < deadline:
            # Kiểm tra xem có ảnh nào mới và đã load xong chưa
            new_image_found = await page.evaluate(
                """(knownIds) => {
                    const knownSet = new Set(knownIds);
                    const imgs = Array.from(document.querySelectorAll('flow-image-tile img'));
                    for (let i = imgs.length - 1; i >= 0; i--) {
                        const img = imgs[i];
                        const id = img.getAttribute('data-media-id') || img.src;
                        if (id && !knownSet.has(id)) {
                            // Kiểm tra ảnh đã load xong và có kích thước thực tế
                            if (img.complete && img.naturalWidth > 0) {
                                return { found: true, id: id, index: i };
                            }
                        }
                    }
                    return { found: false };
                }""",
                list(existing_ids)
            )

            if new_image_found.get("found"):
                logger.info("🎉 Phát hiện ảnh mới đã hoàn tất! ID/URL: %s", str(new_image_found.get("id"))[:60])
                # Trả về locator của ảnh mới nhất
                return page.locator("flow-image-tile img").nth(new_image_found["index"])

            await page.wait_for_timeout(1_000)  # Thăm dò mỗi giây

        try:
            shot = config.OUTPUT_DIR / "debug-no-new-image.png"
            await page.screenshot(path=str(shot))
            logger.error("Không thấy ảnh mới — đã chụp màn hình: %s", shot)
        except Exception:
            pass
        raise TimeoutError(
            f"Quá thời gian ({config.IMAGE_GENERATION_TIMEOUT}s) mà không nhận được ảnh mới. "
            "Google Flow có thể đang quá tải hoặc gặp lỗi tạo ảnh."
        )

    # ── Save image ───────────────────────────────────────────────────────────

    async def _save_image_element(self, page: Page, img_locator, output_path: Path) -> None:
        """
        Lưu ảnh cụ thể vừa tạo về đĩa:
        Ưu tiên tải dữ liệu gốc từ URL qua page.request, fallback blob hoặc screenshot.
        """
        src = await img_locator.get_attribute("src", timeout=10_000)

        if src and src.startswith("https://"):
            # Ô ảnh trong thư viện dùng bản thu nhỏ: ".../asb/<id>=s512-rw". "=s0" = kích thước gốc.
            full = re.sub(r"=s\d+(-[A-Za-z0-9-]+)?$", "=s0", src)
            for url in ([full, src] if full != src else [src]):
                logger.info("Đang tải dữ liệu ảnh từ URL: %s …", url[-60:])
                resp = await page.request.get(url)
                if resp.ok:
                    output_path.write_bytes(await resp.body())
                    return
                logger.warning("HTTP %d khi tải ảnh %s, thử phương án khác.", resp.status, url[-20:])

        elif src and src.startswith("blob:"):
            data_url: str = await page.evaluate(
                """async (u) => {
                    const r = await fetch(u);
                    const b = await r.arrayBuffer();
                    const arr = new Uint8Array(b);
                    let s = '';
                    arr.forEach(x => s += String.fromCharCode(x));
                    return 'data:image/png;base64,' + btoa(s);
                }""",
                src,
            )
            output_path.write_bytes(base64.b64decode(data_url.split(",", 1)[1]))
            return

        # Fallback: Chụp ảnh trực tiếp element trên DOM
        logger.info("Chụp ảnh element canvas/DOM...")
        await img_locator.screenshot(path=str(output_path))

    # ── Project management ───────────────────────────────────────────────────

    async def _ensure_and_navigate_app_project(
        self,
        page: Page,
        app_name: Optional[str] = None,
        project_url: Optional[str] = None,
    ) -> str:
        """
        Đảm bảo page đang mở đúng project URL của app_name (hoặc project_url tùy chỉnh).
        Nếu project chưa có hoặc không còn tồn tại trên Flow, tự động tạo mới và cập nhật ProjectStore.
        """
        target_app = (app_name or "default").strip()

        # 1. Nếu caller cung cấp trực tiếp project_url
        if project_url and "/project/" in project_url:
            target_url = project_url
        else:
            # Lấy URL đã lưu cho app này
            target_url = ProjectStore.get_project(target_app)

        # 2. Nếu đã ở đúng project_url đó trên trình duyệt, không cần navigate lại
        if target_url and page.url.startswith(target_url):
            self._project_url = target_url
            return target_url

        # 3. Nếu có target_url, kiểm tra và navigate
        if target_url:
            logger.info("Chuyển tới project của app [%s]: %s", target_app, target_url)
            try:
                response = await page.goto(target_url, wait_until="domcontentloaded", timeout=config.PAGE_LOAD_TIMEOUT * 1000)
                await page.wait_for_load_state("networkidle", timeout=10_000)
                if "/project/" in page.url and (response is None or response.status != 404):
                    self._project_url = page.url
                    if page.url != target_url:
                        ProjectStore.set_project(target_app, page.url)
                    return self._project_url
                logger.warning("Project [%s] không còn hợp lệ trên Flow — sẽ tự tạo project mới.", target_app)
            except Exception as e:
                logger.warning("Không thể mở project [%s]: %s — sẽ tạo project mới.", target_app, e)

        # 4. Chưa có project hoặc project cũ không còn -> Tự động tạo project mới cho app
        logger.info("Tự động tạo Project mới trên Google Flow cho app [%s] …", target_app)
        new_url = await self._create_new_project(page)
        ProjectStore.set_project(target_app, new_url)
        self._project_url = new_url
        return new_url

    async def _ensure_project(self, page: Page) -> str:
        """Đảm bảo có project mặc định hợp lệ khi khởi động."""
        return await self._ensure_and_navigate_app_project(page, app_name="default")

    async def _project_exists(self, page: Page, url: str) -> bool:
        """Kiểm tra project còn tồn tại bằng cách navigate và xem có lỗi không."""
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
            if response and response.status == 404:
                return False
            # Kiểm tra có bị redirect về trang chủ không (project đã bị xóa)
            await page.wait_for_load_state("networkidle", timeout=10_000)
            if "/project/" in page.url:
                return True
            return False
        except Exception:
            return False

    async def _create_new_project(self, page: Page) -> str:
        """
        Vào trang chủ flow.google.com → hover để hiện nút "+ Dự án mới"
        → click → chờ redirect đến /project/<id> → trả URL.
        """
        logger.info("Vào trang chủ flow.google.com …")
        await page.goto(config.FLOW_URL, wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle", timeout=config.PAGE_LOAD_TIMEOUT * 1000)

        # Hover vào vùng main để nút "Dự án mới" hiện ra
        logger.info("Hover để hiện nút Dự án mới …")
        try:
            await page.locator("main").hover(timeout=5_000)
        except Exception:
            pass
        await page.wait_for_timeout(800)

        # Tìm và click nút "+ Dự án mới"
        new_btn = page.locator(_Sel.NEW_PROJECT_BTN).first
        await new_btn.wait_for(state="visible", timeout=config.PAGE_LOAD_TIMEOUT * 1000)
        await new_btn.click()
        logger.info("Đã click Dự án mới — chờ redirect …")

        # Chờ chuyển sang trang /project/<id>
        await page.wait_for_url("**/project/**", timeout=30_000)
        await page.wait_for_load_state("networkidle", timeout=config.PAGE_LOAD_TIMEOUT * 1000)

        project_url = page.url
        logger.info("Project mới được tạo: %s", project_url)
        return project_url

    # ── Navigation ───────────────────────────────────────────────────────────

    async def _navigate_to_project(self, page: Page) -> None:
        """Điều hướng đến project. Tạo mới nếu project không còn tồn tại."""
        await self._ensure_and_navigate_app_project(page, app_name="default", project_url=self._project_url)

    # ── Aspect ratio (Loại khung hình) ───────────────────────────────────────

    async def _find_aspect_settings_trigger(self, page: Page):
        """Tìm nút / pill mở bảng cài đặt chế độ hình ảnh (chứa Nano Banana / x1 / x2 / tỷ lệ...)."""
        strategies = [
            page.locator("div.bottom-controls button:has-text('Banana')").first,
            page.locator("button:has-text('Banana')").first,
            page.locator("button:has-text('Nano Banana')").first,
            page.locator("div.bottom-controls button:has-text('x')").first,
            page.locator("button:has-text('x1'), button:has-text('x2'), button:has-text('x3'), button:has-text('x4')").first,
            page.locator("div.bottom-controls button:has-text(':')").first,
            page.locator("button[aria-label*='cài đặt' i]").first,
            page.locator("button[aria-label*='settings' i]").first,
            page.locator("div.bottom-controls button").nth(1),
        ]
        for loc in strategies:
            try:
                if await loc.count() > 0 and await loc.is_visible():
                    return loc
            except Exception:
                continue
        raise RuntimeError("Không tìm thấy nút mở Cài đặt khung hình trên thanh prompt của Flow.")

    async def _ensure_aspect(self, page: Page, aspect: str) -> None:
        """
        Mở bảng 'Chế độ mặc định của tính năng tạo hình ảnh' ở thanh prompt,
        chọn loại khung hình (16:9, 4:3, 1:1, 3:4, 9:16) và x1, rồi đóng lại.
        Nhớ (project, tỷ lệ) đã chỉnh để các ảnh sau không phải mở lại nếu cùng tỷ lệ.
        """
        if aspect not in ASPECT_RATIOS:
            raise ValueError(f"Loại khung hình '{aspect}' không hợp lệ. Chỉ hỗ trợ: {', '.join(ASPECT_RATIOS)}")

        key = (self._project_url or "", aspect)
        if self._aspect_for == key:
            return

        logger.info("Đang cài đặt loại khung hình %s trên Google Flow …", aspect)

        # 1. Kiểm tra xem popover cài đặt đã mở sẵn chưa
        popover_indicator = page.locator("text='Chế độ mặc định của tính năng tạo hình ảnh', text='Default mode'")
        is_open = await popover_indicator.count() > 0 and await popover_indicator.first.is_visible()

        # 2. Nếu chưa mở, click nút trigger trên thanh prompt
        if not is_open:
            trigger = await self._find_aspect_settings_trigger(page)
            await trigger.click()
            await page.wait_for_timeout(600)

        # 3. Chờ các nút lựa chọn xuất hiện
        try:
            await page.locator(f"text='{aspect}'").first.wait_for(state="visible", timeout=4_000)
        except Exception:
            pass

        # 4. Click chọn loại khung hình
        aspect_clicked = False

        # Thử click qua JS evaluate (nhanh và chính xác theo text trong popover)
        try:
            clicked_js = await page.evaluate(
                """(targetAspect) => {
                    const all = Array.from(document.querySelectorAll('*'));
                    for (const el of all) {
                        const t = (el.innerText || '').trim();
                        if (t === targetAspect) {
                            const clickable = el.closest('button, [role="radio"], [role="button"]') || el;
                            clickable.click();
                            return true;
                        }
                    }
                    return false;
                }""",
                aspect,
            )
            if clicked_js:
                aspect_clicked = True
                logger.info("Đã chọn loại khung hình: %s", aspect)
                await page.wait_for_timeout(300)
        except Exception as exc:
            logger.warning("Click qua JS gặp lỗi: %s", exc)

        # Fallback bằng Playwright locators
        if not aspect_clicked:
            aspect_locators = [
                page.locator(f"button:has-text('{aspect}')").first,
                page.get_by_role("button", name=aspect, exact=True).first,
                page.locator(f"[role='radio']:has-text('{aspect}')").first,
                page.get_by_text(aspect, exact=True).first,
            ]
            for loc in aspect_locators:
                try:
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click()
                        aspect_clicked = True
                        logger.info("Đã click chọn loại khung hình: %s", aspect)
                        await page.wait_for_timeout(300)
                        break
                except Exception:
                    continue

        if not aspect_clicked:
            raise RuntimeError(f"Không thể click chọn loại khung hình '{aspect}' trong cài đặt của Flow.")

        # 5. Đảm bảo chọn 'x1' để tạo đúng 1 ảnh mỗi prompt
        try:
            await page.evaluate(
                """() => {
                    const all = Array.from(document.querySelectorAll('*'));
                    for (const el of all) {
                        if ((el.innerText || '').trim() === 'x1') {
                            const clickable = el.closest('button, [role="radio"], [role="button"]') || el;
                            clickable.click();
                            return true;
                        }
                    }
                    return false;
                }"""
            )
            await page.wait_for_timeout(200)
        except Exception as exc:
            logger.warning("Không chọn được x1: %s", exc)

        # 6. Đóng popover cài đặt hoàn toàn để không che khuất ô nhập prompt
        await self._close_aspect_popover(page)

        # 7. Đảm bảo ô prompt sẵn sàng nhận chữ
        editor = page.locator(_Sel.PROMPT).first
        await editor.wait_for(state="visible", timeout=5_000)

        self._aspect_for = key
        logger.info("✅ Đã thiết lập xong loại khung hình: %s", aspect)

    async def _close_aspect_popover(self, page: Page) -> None:
        """
        Đóng popover 'Chế độ mặc định của tính năng tạo hình ảnh'.
        Trong Flow, cài đặt tự động lưu ngay khi click. Chỉ cần đóng popover để giải phóng giao diện.
        """
        popover_indicator = page.locator("text='Chế độ mặc định của tính năng tạo hình ảnh', text='Default mode'")
        if not (await popover_indicator.count() > 0 and await popover_indicator.first.is_visible()):
            return

        logger.info("Đang đóng bảng cài đặt khung hình …")

        # 1. Click backdrop của Angular CDK nếu có
        try:
            backdrop = page.locator(".cdk-overlay-backdrop").last
            if await backdrop.count() > 0 and await backdrop.is_visible():
                await backdrop.click(position={"x": 5, "y": 5}, force=True, timeout=2_000)
                await page.wait_for_timeout(300)
        except Exception:
            pass

        # 2. Click vùng trống bên ngoài trên canvas
        if await popover_indicator.count() > 0 and await popover_indicator.first.is_visible():
            try:
                await page.mouse.click(200, 200)
                await page.wait_for_timeout(300)
            except Exception:
                pass

        # 3. Bấm Escape
        if await popover_indicator.count() > 0 and await popover_indicator.first.is_visible():
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)
            except Exception:
                pass

        # 4. Click vào editor để lấy lại tiêu điểm
        if await popover_indicator.count() > 0 and await popover_indicator.first.is_visible():
            try:
                editor = page.locator(_Sel.PROMPT).first
                if await editor.is_visible():
                    await editor.click(force=True, timeout=2_000)
                    await page.wait_for_timeout(300)
            except Exception:
                pass

        logger.info("Đã đóng bảng cài đặt khung hình.")

    # ── Prompt input ─────────────────────────────────────────────────────────

    async def _type_prompt(self, page: Page, prompt: str) -> None:
        """
        Nhập text vào ProseMirror editor (contenteditable div).
        Chèn cả đoạn một lần (nhanh: prompt ~3000 ký tự gõ từng phím mất >60 s);
        nếu editor không nhận thì gõ từng phím như cũ.
        """
        logger.info("Nhập prompt …")
        editor = page.locator(_Sel.PROMPT).first
        try:
            await editor.wait_for(state="visible", timeout=config.PAGE_LOAD_TIMEOUT * 1000)
        except Exception:
            shot = config.OUTPUT_DIR / "debug-prompt-hidden.png"
            try:
                await page.screenshot(path=str(shot))
                logger.error("Ô prompt bị ẩn — đã chụp màn hình: %s", shot)
            except Exception:
                pass
            raise
        await editor.click()
        await page.keyboard.press("Control+a")
        await page.keyboard.press("Delete")
        await page.keyboard.insert_text(prompt)
        await page.wait_for_timeout(300)
        got = " ".join((await editor.inner_text()).split())
        want = " ".join(prompt.split())
        if len(got) < len(want) * 0.9:
            logger.warning("Editor chưa nhận đủ prompt khi chèn (%d/%d ký tự) — gõ từng phím.", len(got), len(want))
            await editor.click()
            await page.keyboard.press("Control+a")
            await page.keyboard.press("Delete")
            await page.keyboard.type(prompt, delay=5)
        logger.info("Đã nhập: %s", prompt[:80])

    # ── Submit ───────────────────────────────────────────────────────────────

    async def _click_submit(self, page: Page) -> None:
        """
        Bấm nút gửi → của ô prompt rồi kiểm tra prompt đã được gửi (ô nhập trống lại).
        Giao diện Flow mới là khung chat "tác nhân": nút → nằm cạnh ô prompt (icon arrow_forward, nhãn "Bắt đầu tạo").
        """
        logger.info("Bấm submit …")
        editor = page.locator(_Sel.PROMPT).first

        async def sent() -> bool:
            for _ in range(6):
                try:
                    if not " ".join((await editor.inner_text()).split()):
                        return True
                except Exception:
                    return True  # editor replaced/removed after sending
                await page.wait_for_timeout(500)
            return False

        # Nút → trong cùng khung với ô prompt (tìm ngược lên các khung cha gần nhất). Khi tác nhân Flow còn đang trả lời
        # ảnh trước, nút này bị khoá: chờ rồi thử lại, thay vì bấm các selector cũ (chúng trúng ô đổi tên bộ sưu tập).
        find_send = """(ed) => {
            const isSend = (b) => {
                const t = (b.innerText || '').toLowerCase();
                const a = (b.getAttribute('aria-label') || '').toLowerCase();
                return t.includes('arrow_forward') || a.includes('bắt đầu tạo') || a.includes('gửi')
                    || a === 'send' || a.includes('create') || a.includes('generate');
            };
            let box = ed.parentElement;
            for (let i = 0; i < 8 && box; i++, box = box.parentElement) {
                const btn = [...box.querySelectorAll('button')].reverse()
                    .find((b) => isSend(b) && b.getClientRects().length);
                if (btn) {
                    if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') return 'busy';
                    btn.click();
                    return 'clicked';
                }
            }
            return 'none';
        }"""
        deadline = asyncio.get_event_loop().time() + 60
        attempt = 0
        while True:
            attempt += 1
            try:
                state = await editor.evaluate(find_send)
            except Exception as exc:
                logger.warning("Không đọc được nút → cạnh ô prompt: %s", exc)
                state = "none"
            if state == "clicked" and await sent():
                return
            if state != "busy":
                # khung chat cũng gửi bằng Enter
                try:
                    await editor.click()
                    await page.keyboard.press("End")
                    await page.keyboard.press("Enter")
                    if await sent():
                        logger.info("Đã gửi prompt bằng Enter.")
                        return
                except Exception:
                    pass
            if asyncio.get_event_loop().time() > deadline:
                break
            logger.info("Chưa gửi được prompt (nút: %s, lần %d) — chờ tác nhân Flow rảnh rồi thử lại …", state, attempt)
            await page.wait_for_timeout(2_000)

        try:
            await page.screenshot(path=str(Path(config.OUTPUT_DIR) / "debug-submit-failed.png"))
        except Exception:
            pass
        raise RuntimeError("Không gửi được prompt lên Flow (nút gửi không phản hồi sau 60s)")


