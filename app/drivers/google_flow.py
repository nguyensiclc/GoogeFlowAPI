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

    def __init__(
        self,
        user_data_dir: Optional[str] = None,
        context: Optional[BrowserContext] = None,
        own_page: bool = False,
    ) -> None:
        # own_page: extra parallel worker — always opens its own tab and skips the default project at start.
        self._own_page = own_page
        self.user_data_dir: str = user_data_dir or config.CHROME_USER_DATA_DIR
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = context
        self._owns_context: bool = context is None
        self._page: Optional[Page] = None
        self._project_url: Optional[str] = None
        # Tỷ lệ đã chỉnh cho project hiện tại (để không mở Cài đặt mỗi ảnh)
        self._aspect_for: Optional[tuple[str, str]] = None

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        logger.info("Starting GoogleFlowDriver with profile: %s …", self.user_data_dir)
        if not self._context:
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

        if self._own_page and (not self._page or self._page.is_closed()):
            self._page = await self._context.new_page()
            logger.info("Worker Flow: đã mở tab riêng.")
            return

        if not self._page or self._page.is_closed():
            # Tìm xem có page nào đang mở Flow không
            flow_pages = [p for p in self._context.pages if "flow.google" in p.url]
            if flow_pages:
                self._page = flow_pages[0]
            elif self._context.pages and self._owns_context:
                self._page = self._context.pages[0]
            else:
                self._page = await self._context.new_page()

        # Đảm bảo có project hợp lệ ngay khi start
        self._project_url = await self._ensure_project(self._page)
        logger.info("Driver ready. Project: %s", self._project_url)

    async def stop(self) -> None:
        if self._page and not self._page.is_closed():
            try:
                await self._page.close()
            except Exception:
                pass
            self._page = None

        if self._owns_context:
            if self._context:
                await self._context.close()
                self._context = None
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
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

        try:
            return await self._generate_once(prompt, aspect_ratio, reference_images, app_name, project_url)
        except RuntimeError as exc:
            if "FLOW_REFUSED" not in str(exc):
                raise
        # Refused once: reload the Flow page (like pressing F5) a single time and send this picture again.
        # A second refusal is final: the caller stops the batch and the user waits.
        logger.warning("Flow báo 'hoạt động bất thường' — tải lại trang Flow (F5) rồi thử lại ảnh này 1 lần …")
        try:
            await self._page.reload(wait_until="domcontentloaded", timeout=60_000)
        except Exception as reload_exc:
            logger.warning("Tải lại trang lỗi: %s", reload_exc)
        await self._page.wait_for_timeout(8_000)
        self._aspect_for = None  # re-check the frame settings on the fresh page
        return await self._generate_once(prompt, aspect_ratio, reference_images, app_name, project_url)

    async def _generate_once(
        self,
        prompt: str,
        aspect_ratio: Optional[str],
        reference_images: Optional[List[Path]],
        app_name: Optional[str],
        project_url: Optional[str],
    ) -> Path:
        page = self._page
        output_path = config.OUTPUT_DIR / f"{uuid.uuid4().hex}.png"

        # 1. Điều hướng đến project riêng của app (tự tạo nếu chưa có)
        await self._ensure_and_navigate_app_project(page, app_name=app_name, project_url=project_url)

        # 1b. Chỉnh tỷ lệ khung hình + 1 ảnh mỗi lần tạo (chỉ khi đổi tỷ lệ hoặc đổi project)
        if aspect_ratio:
            await self._ensure_aspect(page, aspect_ratio)

        # 2. Ảnh tham chiếu: chọn lại từ thư viện của project nếu đã tải lên trước đó, chưa có thì mới tải lên.
        # The previous picture in this tab timed out: it may still land in the gallery. Wait for it (up to 90 s)
        # so it is never taken for THIS prompt's picture (that shifts every later scene by one).
        late = getattr(self, "_late_from", None)
        if late is not None:
            self._late_from = None
            for _ in range(90):
                if await self._get_existing_media_ids(page) - late:
                    logger.info("Ảnh trễ của lượt trước đã hiện — bỏ qua nó.")
                    await page.wait_for_timeout(2_000)
                    break
                await page.wait_for_timeout(1_000)

        before_refs = await self._get_existing_media_ids(page)
        uploads = 0
        if reference_images:
            for ref_path in reference_images:
                if await self._attach_reference(page, ref_path):
                    uploads += 1
        if uploads:
            # A fresh upload adds its own tile to the gallery: wait for it, so it is never taken for the new picture.
            for _ in range(24):
                if len(await self._get_existing_media_ids(page) - before_refs) >= uploads:
                    break
                await page.wait_for_timeout(500)

        # 2b. Thu thập ID các ảnh hiện có (sau khi đính kèm, để ảnh tham chiếu không bị nhầm là ảnh mới tạo)
        existing_ids = await self._get_existing_media_ids(page)
        logger.info("Hiện có %d ảnh trong project trước khi submit.", len(existing_ids))

        # 3. Nhập prompt
        await self._type_prompt(page, prompt)

        # 4. Bấm submit
        await self._click_submit(page)

        # 5. Chờ chính xác thời điểm ảnh mới hoàn thành (Network + DOM + Image Loaded)
        logger.info("Đang theo dõi tiến trình tạo ảnh (tối đa %ds) …", config.IMAGE_GENERATION_TIMEOUT)
        try:
            new_img_locator = await self._wait_for_new_image(page, existing_ids)
        except TimeoutError:
            self._late_from = existing_ids  # its picture may still arrive: the next prompt waits for it first
            raise

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

    async def _attach_reference(self, page: Page, image_path: Path) -> bool:
        """
        Đính kèm ảnh tham chiếu: chọn lại bản đã có trong thư viện (tìm theo tên file), chưa có thì tải lên 1 lần.
        True khi phải tải lên (ảnh mới xuất hiện thêm một ô trong project).
        """
        try:
            if await self._pick_from_library(page, image_path):
                logger.info("♻ Dùng lại ảnh tham chiếu đã có trên Flow: %s", image_path.name)
                return False
        except Exception as exc:
            logger.warning("Không chọn được ảnh từ thư viện (%s) — sẽ tải lên.", exc)
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(500)
            except Exception:
                pass
        logger.info("⬆ Ảnh tham chiếu chưa có trong project này — tải lên: %s", image_path.name)
        await self._upload_reference_image(page, image_path)
        return True

    async def _pick_from_library(self, page: Page, image_path: Path) -> bool:
        """Mở '+' → tìm tên file trong thư viện thành phần → bấm vào (Flow gắn ngay vào ô lệnh). False nếu chưa có."""
        chips = page.locator("button.chip-container")
        chips_before = await chips.count()
        add_btn = await self._find_add_media_button(page)
        await add_btn.click()
        # The asset dialog's own search box (not the page's search bar at the top).
        search = page.locator("input.search-input[aria-label*='Tìm kiếm thành phần' i],"
                              " input.search-input[placeholder*='Tìm kiếm thành phần' i]").last
        await search.wait_for(state="visible", timeout=8_000)
        await search.fill(image_path.stem)
        await page.wait_for_timeout(2_000)
        item = page.locator("button.asset-item", has=page.locator(".asset-title", has_text=image_path.name)).first
        if await item.count() == 0:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(700)
            return False
        await item.click()
        add = page.locator("button:has-text('Thêm vào câu lệnh'), button:has-text('Add to prompt')").first
        for _ in range(12):
            await page.wait_for_timeout(500)
            if await chips.count() > chips_before:  # attached straight away by the click
                return True
            if await add.count() > 0 and await add.is_visible() and await add.get_attribute("disabled") is None:
                await add.click()
                await page.wait_for_timeout(1_000)
                return True
        raise RuntimeError("đã chọn ảnh trong thư viện nhưng không thấy ảnh gắn vào ô lệnh")

    async def _upload_reference_image(self, page: Page, image_path: Path) -> None:
        """
        Thực hiện quy trình:
        1. Bấm dấu '+' ở thanh prompt
        2. Bấm vào 'Tải nội dung...'
        3. Chọn file ảnh từ image_path
        4. Chờ ảnh tải lên và bấm nút 'Thêm vào câu lệnh' (Add to prompt)
        5. Đảm bảo popup đã đóng và ảnh đã được đính kèm vào thanh prompt
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

        # 3. Chờ ảnh tải lên và bấm nút 'Thêm vào câu lệnh'
        logger.info("Chờ ảnh tải lên và nút 'Thêm vào câu lệnh' xuất hiện …")
        add_btn_locators = [
            page.locator("button:has-text('Thêm vào câu lệnh')").first,
            page.locator("button:has-text('Add to prompt')").first,
            page.locator("[aria-label*='Thêm vào câu lệnh' i]").first,
            page.locator("text='Thêm vào câu lệnh'").first,
        ]

        inserted = False
        for _ in range(25):  # Chờ tối đa 25 giây cho file upload xong
            for btn_loc in add_btn_locators:
                try:
                    if await btn_loc.count() > 0 and await btn_loc.is_visible():
                        is_disabled = await btn_loc.get_attribute("disabled")
                        if is_disabled is None:
                            logger.info("Bấm nút 'Thêm vào câu lệnh' …")
                            await btn_loc.click()
                            inserted = True
                            break
                except Exception:
                    pass
            if inserted:
                break
            await page.wait_for_timeout(1000)

        if not inserted:
            logger.warning("Không tìm thấy hoặc không click được nút 'Thêm vào câu lệnh', thử phím Enter …")
            try:
                await page.keyboard.press("Enter")
            except Exception:
                pass

        await page.wait_for_timeout(1000)

        # 4. Đảm bảo popup thư viện ảnh đã đóng lại
        try:
            dialog_close = page.locator("button:has-text('close'), button[aria-label*='Đóng' i]").first
            if await dialog_close.count() > 0 and await dialog_close.is_visible():
                await dialog_close.click()
                await page.wait_for_timeout(500)
        except Exception:
            pass

        logger.info("✅ Đã hoàn tất đính kèm ảnh tham chiếu: %s", image_path.name)

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
        # Google sometimes refuses the generation ("unusual activity" / "hoạt động bất thường"). Count those notices
        # now, so a NEW one means this request was refused: stop right away instead of waiting for the timeout.
        # generate_image reloads the page once and retries; a second refusal stops the batch.
        refused_before = await self._count_refusals(page)

        while time.monotonic() < deadline:
            if await self._count_refusals(page) > refused_before:
                try:
                    await page.screenshot(path=str(config.OUTPUT_DIR / "debug-flow-refused.png"))
                except Exception:
                    pass
                raise RuntimeError(
                    "FLOW_REFUSED: Google Flow từ chối tạo ảnh vì 'hoạt động bất thường'. "
                    "Hãy dừng tạo ảnh, đợi vài giờ rồi thử lại."
                )
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

    async def _count_refusals(self, page: Page) -> int:
        """How many 'unusual activity' refusal notices the page shows (Vietnamese or English UI)."""
        try:
            return await page.evaluate(
                """() => {
                    const t = (document.body && document.body.innerText || '').toLowerCase();
                    let n = 0;
                    for (const k of ['hoạt động bất thường', 'unusual activity']) n += t.split(k).length - 1;
                    return n;
                }"""
            )
        except Exception:
            return 0

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
            await self._close_side_panel_if_open(page)
            await self._ensure_agent_mode_off(page)
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
                    await self._close_side_panel_if_open(page)
                    await self._ensure_agent_mode_off(page)
                    return self._project_url
                logger.warning("Project [%s] không còn hợp lệ trên Flow — sẽ tự tạo project mới.", target_app)
            except Exception as e:
                logger.warning("Không thể mở project [%s]: %s — sẽ tạo project mới.", target_app, e)

        # 4. Chưa có project hoặc project cũ không còn -> Tự động tạo project mới cho app
        logger.info("Tự động tạo Project mới trên Google Flow cho app [%s] …", target_app)
        new_url = await self._create_new_project(page)
        ProjectStore.set_project(target_app, new_url)
        self._project_url = new_url
        await self._close_side_panel_if_open(page)
        await self._ensure_agent_mode_off(page)
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

    async def delete_project(self, project_url: str) -> bool:
        """
        Deletes a Flow project from the Flow home page: the project's card → its "more" menu → "Xóa"/"Delete" →
        confirm. Returns True when the card is gone afterwards. On failure, saves a screenshot and the card's HTML
        (debug-delete-*.png / .html in OUTPUT_DIR) and raises, so the selectors can be fixed.
        """
        page = self._page
        if page is None or page.is_closed():
            raise RuntimeError("Driver chưa được start.")
        m = re.search(r"/project/([^/?#]+)", project_url or "")
        if not m:
            raise RuntimeError(f"URL project không hợp lệ: {project_url}")
        pid = m.group(1)
        card_link = f'a[href*="/project/{pid}"]'

        await page.goto(config.FLOW_URL, wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle", timeout=config.PAGE_LOAD_TIMEOUT * 1000)
        link = page.locator(card_link).first
        try:
            await link.wait_for(state="attached", timeout=15_000)
        except Exception:
            logger.info("Không thấy project %s trên trang chủ Flow — coi như đã xoá.", pid)
            return True
        await link.scroll_into_view_if_needed()
        await link.hover()
        await page.wait_for_timeout(600)

        # The card = nearest ancestor of the link that also holds a menu button.
        card = link.locator("xpath=ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' project-card ')][1]")
        if not await card.count():
            card = link.locator("xpath=ancestor-or-self::*[.//button][1]")

        # Flow (Nov 2026): the card footer has a trash button aria-label="Xoá dự án" (shown on hover).
        direct = card.locator('button[aria-label*="Xoá dự án"], button[aria-label*="Xóa dự án"], '
                              'button[aria-label*="Delete project"], button:has(mat-icon:text-is("delete"))').first
        if await direct.count():
            await card.hover()
            await page.wait_for_timeout(300)
            await direct.click(force=True)
            await page.wait_for_timeout(700)
            return await self._confirm_and_verify_delete(page, card_link, pid)

        menu_btn = None
        for sel in [
            'button[aria-label*="Tùy chọn"]', 'button[aria-label*="tùy chọn"]', 'button[aria-label*="More"]',
            'button[aria-label*="more"]', 'button[aria-label*="Options"]', 'button[aria-label*="Thêm"]',
            'button:has-text("more_vert")', 'button:has-text("more_horiz")',
        ]:
            loc = card.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible(timeout=1_500):
                    menu_btn = loc
                    break
            except Exception:
                continue
        if menu_btn is None:
            await self._debug_delete(page, card, "no-menu")
            raise RuntimeError("Không tìm thấy nút menu (⋮) của project trên trang chủ Flow.")
        await menu_btn.click()
        await page.wait_for_timeout(500)

        delete_item = None
        for sel in ['[role="menuitem"]:has-text("Xóa")', '[role="menuitem"]:has-text("Xoá")', '[role="menuitem"]:has-text("Delete")',
                    'button:has-text("Xóa dự án")', 'button:has-text("Delete project")',
                    '.cdk-overlay-pane button:has-text("Xóa")', '.cdk-overlay-pane button:has-text("Delete")']:
            loc = page.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible(timeout=1_500):
                    delete_item = loc
                    break
            except Exception:
                continue
        if delete_item is None:
            await self._debug_delete(page, card, "no-delete-item")
            await page.keyboard.press("Escape")
            raise RuntimeError("Menu của project không có mục Xóa/Delete.")
        await delete_item.click()
        await page.wait_for_timeout(700)
        return await self._confirm_and_verify_delete(page, card_link, pid)

    async def _confirm_and_verify_delete(self, page: Page, card_link: str, pid: str) -> bool:
        # Confirmation dialog (if any): the destructive button.
        for sel in ['[role="dialog"] button:has-text("Xóa")', '[role="dialog"] button:has-text("Xoá")', '[role="dialog"] button:has-text("Delete")',
                    '[role="alertdialog"] button:has-text("Xóa")', '[role="alertdialog"] button:has-text("Delete")',
                    '.cdk-overlay-pane button:has-text("Xóa")', '.cdk-overlay-pane button:has-text("Delete")']:
            loc = page.locator(sel).last
            try:
                if await loc.count() and await loc.is_visible(timeout=1_500):
                    await loc.click()
                    break
            except Exception:
                continue

        # Done when the card is gone (reload once to be sure).
        for _ in range(2):
            try:
                await page.locator(card_link).first.wait_for(state="detached", timeout=10_000)
                logger.info("🗑 Đã xoá project Flow %s", pid)
                return True
            except Exception:
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_load_state("networkidle", timeout=15_000)
                if await page.locator(card_link).count() == 0:
                    logger.info("🗑 Đã xoá project Flow %s", pid)
                    return True
        await self._debug_delete(page, page.locator(card_link).first, "still-there")
        raise RuntimeError("Đã bấm xoá nhưng project vẫn còn trên trang chủ Flow.")

    async def _debug_delete(self, page: Page, card, tag: str) -> None:
        try:
            await page.screenshot(path=str(config.OUTPUT_DIR / f"debug-delete-{tag}.png"))
            html = await card.evaluate("el => el.outerHTML") if await card.count() else await page.content()
            (config.OUTPUT_DIR / f"debug-delete-{tag}.html").write_text(html[:200_000], encoding="utf-8")
        except Exception:
            pass

    # ── Navigation ───────────────────────────────────────────────────────────

    async def _navigate_to_project(self, page: Page) -> None:
        """Điều hướng đến project. Tạo mới nếu project không còn tồn tại."""
        await self._ensure_and_navigate_app_project(page, app_name="default", project_url=self._project_url)

    # ── Aspect ratio (Loại khung hình) ───────────────────────────────────────

    async def _close_side_panel_if_open(self, page: Page) -> None:
        """Nếu panel trợ lý bên phải (Phiên không có tiêu đề) đang mở, đóng nó lại."""
        try:
            close_btn = page.locator("button:has-text('close'), button[aria-label*='Đóng' i], button[aria-label*='Close' i]").first
            if await close_btn.count() > 0 and await close_btn.is_visible():
                logger.info("Đóng side panel trợ lý của Flow …")
                await close_btn.click()
                await page.wait_for_timeout(400)
        except Exception:
            pass

    async def _ensure_agent_mode_off(self, page: Page) -> None:
        """
        Tắt chế độ 'Tác nhân' (nếu đang bật) để thanh prompt hiển thị nút cấu hình ảnh trực tiếp.
        Khi Tác nhân bật (agent-mode-chip-checked), nút cài đặt ảnh bị ẩn.
        """
        try:
            editor = page.locator(_Sel.PROMPT).first
            if not await editor.is_visible():
                return
            toggled = await editor.evaluate("""(ed) => {
                let box = ed.parentElement;
                for (let i = 0; i < 8 && box; i++, box = box.parentElement) {
                    const btns = Array.from(box.querySelectorAll('button'));
                    const agentBtn = btns.find(b => b.innerText.includes('Tác nhân') || b.classList.contains('agent-mode-chip'));
                    if (agentBtn && (agentBtn.classList.contains('agent-mode-chip-checked') || agentBtn.getAttribute('aria-checked') === 'true')) {
                        agentBtn.click();
                        return true;
                    }
                }
                return false;
            }""")
            if toggled:
                logger.info("Đã tắt chế độ Tác nhân (chuyển sang tạo ảnh trực tiếp).")
                await page.wait_for_timeout(500)
        except Exception as exc:
            logger.warning("Không thể kiểm tra/tắt chế độ Tác nhân: %s", exc)

    async def _find_aspect_settings_trigger(self, page: Page):
        """Tìm nút / pill mở bảng cài đặt chế độ hình ảnh (chứa Nano Banana / x1 / x2 / tỷ lệ...)."""
        await self._close_side_panel_if_open(page)
        await self._ensure_agent_mode_off(page)

        editor = page.locator(_Sel.PROMPT).first
        await editor.wait_for(state="visible", timeout=10_000)

        # 1. Tìm chính xác qua cây DOM của thanh prompt
        btn_handle = await editor.evaluate_handle("""(ed) => {
            let box = ed.parentElement;
            for (let i = 0; i < 8 && box; i++, box = box.parentElement) {
                const btns = Array.from(box.querySelectorAll('button'));
                const stBtn = btns.find(b => {
                    const t = (b.innerText || '').toLowerCase();
                    return b.classList.contains('settings-trigger-button') ||
                           t.includes('banana') || t.includes('16:9') || t.includes('4:3') ||
                           t.includes('1:1') || t.includes('3:4') || t.includes('9:16') ||
                           t.includes('x1') || t.includes('x2') || t.includes('x3') || t.includes('x4');
                });
                if (stBtn && stBtn.offsetWidth > 0) return stBtn;
            }
            return null;
        }""")

        if btn_handle and btn_handle.as_element():
            return btn_handle.as_element()

        # 2. Fallback locators
        for sel in [
            ".settings-trigger-button",
            "button:has-text('Banana')",
            "button:has-text('16:9'), button:has-text('4:3'), button:has-text('1:1'), button:has-text('3:4'), button:has-text('9:16')",
            "button:has-text('x1'), button:has-text('x2')",
        ]:
            loc = page.locator(sel).first
            try:
                if await loc.count() > 0 and await loc.is_visible():
                    return loc
            except Exception:
                continue

        raise RuntimeError("Không tìm thấy nút mở Cài đặt khung hình trên thanh prompt của Flow.")

    async def _ensure_aspect(self, page: Page, aspect: str) -> None:
        """
        Mở bảng cài đặt ở thanh prompt, chọn loại khung hình (16:9, 4:3, 1:1, 3:4, 9:16) và x1, rồi đóng lại.
        Nhớ (project, tỷ lệ) đã chỉnh để các ảnh sau không phải mở lại nếu cùng tỷ lệ.
        """
        if aspect not in ASPECT_RATIOS:
            raise ValueError(f"Loại khung hình '{aspect}' không hợp lệ. Chỉ hỗ trợ: {', '.join(ASPECT_RATIOS)}")

        key = (self._project_url or "", aspect)
        if self._aspect_for == key:
            return

        logger.info("Đang cài đặt loại khung hình %s trên Google Flow …", aspect)

        # 1. Đảm bảo giao diện sẵn sàng (đóng side panel, tắt Agent Mode)
        await self._close_side_panel_if_open(page)
        await self._ensure_agent_mode_off(page)

        # 2. Kiểm tra xem popover cài đặt đã mở sẵn chưa
        popover = page.locator(".cdk-overlay-pane").first
        is_open = await popover.count() > 0 and await popover.is_visible()

        # 3. Nếu chưa mở, click nút trigger trên thanh prompt
        if not is_open:
            trigger = await self._find_aspect_settings_trigger(page)
            await trigger.click()
            await page.wait_for_timeout(600)

        # 4. Chờ overlay xuất hiện
        try:
            await page.locator(".cdk-overlay-pane").first.wait_for(state="visible", timeout=5_000)
        except Exception:
            pass

        # 5. Click chọn loại khung hình trong overlay
        aspect_clicked = False
        try:
            aspect_clicked = await page.evaluate(
                """(targetAspect) => {
                    const overlay = document.querySelector('.cdk-overlay-pane');
                    const root = overlay || document;
                    const all = Array.from(root.querySelectorAll('*'));
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
            if aspect_clicked:
                logger.info("Đã chọn loại khung hình: %s", aspect)
                await page.wait_for_timeout(300)
        except Exception as exc:
            logger.warning("Click qua JS gặp lỗi: %s", exc)

        # Fallback bằng Playwright locators
        if not aspect_clicked:
            aspect_locators = [
                page.locator(f".cdk-overlay-pane button:has-text('{aspect}')").first,
                page.locator(f"button:has-text('{aspect}')").first,
                page.get_by_role("button", name=aspect, exact=True).first,
            ]
            for loc in aspect_locators:
                try:
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click()
                        aspect_clicked = True
                        logger.info("Đã click chọn loại khung hình (fallback): %s", aspect)
                        await page.wait_for_timeout(300)
                        break
                except Exception:
                    continue

        if not aspect_clicked:
            raise RuntimeError(f"Không thể click chọn loại khung hình '{aspect}' trong cài đặt của Flow.")

        # 6. Đảm bảo chọn 'x1' để tạo đúng 1 ảnh mỗi prompt
        try:
            await page.evaluate(
                """() => {
                    const overlay = document.querySelector('.cdk-overlay-pane');
                    const root = overlay || document;
                    const all = Array.from(root.querySelectorAll('button, [role="radio"], [role="button"]'));
                    const x1 = all.find(b => (b.innerText || '').trim() === 'x1');
                    if (x1) {
                        x1.click();
                        return true;
                    }
                    return false;
                }"""
            )
            await page.wait_for_timeout(200)
        except Exception as exc:
            logger.warning("Không chọn được x1: %s", exc)

        # 7. Đóng popover cài đặt hoàn toàn để không che khuất ô nhập prompt
        await self._close_aspect_popover(page)

        # 8. Đảm bảo ô prompt sẵn sàng nhận chữ
        editor = page.locator(_Sel.PROMPT).first
        await editor.wait_for(state="visible", timeout=5_000)

        self._aspect_for = key
        logger.info("✅ Đã thiết lập xong loại khung hình: %s", aspect)

    async def _close_aspect_popover(self, page: Page) -> None:
        """
        Đóng popover cài đặt khung hình (Angular CDK).
        Trong Flow, cài đặt tự động lưu ngay khi click. Chỉ cần đóng popover để giải phóng giao diện.
        """
        overlay = page.locator(".cdk-overlay-pane").first
        if not (await overlay.count() > 0 and await overlay.is_visible()):
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
        if await overlay.count() > 0 and await overlay.is_visible():
            try:
                await page.mouse.click(100, 100)
                await page.wait_for_timeout(300)
            except Exception:
                pass

        # 3. Bấm Escape
        if await overlay.count() > 0 and await overlay.is_visible():
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)
            except Exception:
                pass

        # 4. Click vào editor để lấy lại tiêu điểm
        if await overlay.count() > 0 and await overlay.is_visible():
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


