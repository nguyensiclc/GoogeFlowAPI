"""
GeminiChatDriver — Playwright automation for gemini.google.com (Gemini Chat).

Tự động hóa Gemini Chat để tạo ảnh bằng mô hình Imagen 3 tích hợp:
  1. Mở trang gemini.google.com/app (chia sẻ cùng Chrome session / BrowserContext với Google Flow)
  2. Bắt đầu phiên trò chuyện mới (New chat)
  3. Upload ảnh tham chiếu (nếu có)
  4. Nhập prompt (tự động kèm tỷ lệ khung hình nếu được yêu cầu)
  5. Gửi câu lệnh và chờ Gemini tạo xong ảnh
  6. Tải dữ liệu ảnh gốc độ phân giải cao (=s0) về thư mục outputs/
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import uuid
from pathlib import Path
from typing import List, Optional

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

import config

logger = logging.getLogger(__name__)


class _GeminiSel:
    # URL chính
    URL = config.GEMINI_URL

    # Nút Cuộc trò chuyện mới
    NEW_CHAT_LINKS = [
        "a[href='/app']",
        "a[aria-label*='Cuộc trò chuyện mới']",
        "a[aria-label*='New chat']",
        "button:has-text('Cuộc trò chuyện mới')",
        "span:has-text('Cuộc trò chuyện mới')",
    ]

    # Hộp thoại đồng ý / điều khoản ban đầu
    CONSENT_BUTTONS = [
        "button:has-text('Tôi hiểu')",
        "button:has-text('Tôi hiểu')",
        "button:has-text('I understand')",
        "button:has-text('Đồng ý')",
        "button:has-text('I agree')",
        "button:has-text('Agree')",
    ]

    # Ô nhập prompt
    PROMPT_INPUT = (
        "div[role='textbox'][contenteditable='true'], "
        "rich-textarea div[contenteditable='true'], "
        "div.ql-editor"
    )

    # Nút gửi tin nhắn
    SEND_BUTTONS = [
        "button[aria-label*='Gửi tin nhắn']",
        "button[aria-label*='Send message']",
        "button.send-button",
        "button[aria-label*='Gửi']",
        "button[aria-label*='Send']",
    ]

    # Nút mở menu tải nội dung
    UPLOAD_MENU_BUTTONS = [
        "button[aria-label*='Nội dung tải lên và công cụ']",
        "button[aria-label*='Uploads and tools']",
        "button[aria-label*='Tải lên']",
        "button[aria-label*='Upload']",
        "button[mattooltip*='Tải lên']",
    ]

    # File input
    FILE_INPUT = "input[type='file'][accept*='image'], input[type='file']"

    # Nút đồng ý khi upload ảnh lần đầu
    UPLOAD_AGREE_BUTTONS = [
        "button:has-text('Đồng ý')",
        "button:has-text('I agree')",
        "button:has-text('Agree')",
    ]

    # Ảnh đã tạo trong hội thoại
    GENERATED_IMAGE = (
        "div.overlay-container img.image, "
        "button.image-button img, "
        "model-response img[src*='googleusercontent'], "
        "img.animate.loaded"
    )


class GeminiChatDriver:
    """
    Quản lý Playwright automation trên gemini.google.com.
    Chia sẻ cùng BrowserContext với Google Flow để dùng chung thông tin đăng nhập Google.
    """

    def __init__(
        self,
        user_data_dir: Optional[str] = None,
        context: Optional[BrowserContext] = None,
    ) -> None:
        self.user_data_dir: str = user_data_dir or config.CHROME_USER_DATA_DIR
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = context
        self._owns_context: bool = context is None
        self._page: Optional[Page] = None

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        logger.info("Khởi động GeminiChatDriver (profile: %s) …", self.user_data_dir)
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

        if not self._page or self._page.is_closed():
            # Tìm xem có page nào đang mở Gemini không
            gemini_pages = [p for p in self._context.pages if "gemini.google" in p.url]
            if gemini_pages:
                self._page = gemini_pages[0]
            else:
                self._page = await self._context.new_page()

        # Điều hướng đến trang Gemini
        if not self._page.url.startswith("https://gemini.google.com"):
            logger.info("Đang mở %s …", _GeminiSel.URL)
            await self._page.goto(_GeminiSel.URL, timeout=config.PAGE_LOAD_TIMEOUT * 1000, wait_until="domcontentloaded")
            await self._page.wait_for_timeout(3_000)

        # Xử lý các thông báo điều khoản/đồng ý nếu có
        await self._dismiss_consent_modals(self._page)
        logger.info("GeminiChatDriver đã sẵn sàng trên URL: %s", self._page.url)

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
        logger.info("GeminiChatDriver đã dừng.")

    async def __aenter__(self) -> "GeminiChatDriver":
        await self.start()
        return self

    async def __aexit__(self, *_) -> None:
        await self.stop()

    # ── Helpers ──────────────────────────────────────────────────────────────

    async def _dismiss_consent_modals(self, page: Page) -> None:
        """Kiểm tra và tự động click các nút đóng hộp thoại 'Tôi hiểu' / 'Đồng ý'."""
        for sel in _GeminiSel.CONSENT_BUTTONS:
            try:
                loc = page.locator(sel)
                if await loc.count() > 0 and await loc.first.is_visible():
                    logger.info("Đóng hộp thoại đồng ý trên Gemini: %s", sel)
                    await loc.first.click()
                    await page.wait_for_timeout(800)
            except Exception:
                pass

    async def _start_new_chat(self, page: Page) -> None:
        """Bắt đầu một phiên hội thoại mới trên Gemini."""
        logger.info("Bắt đầu cuộc trò chuyện mới trên Gemini …")
        for sel in _GeminiSel.NEW_CHAT_LINKS:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=1_500):
                    await loc.click()
                    await page.wait_for_timeout(1_500)
                    await self._dismiss_consent_modals(page)
                    return
            except Exception:
                continue

        # Nếu không click được nút, trực tiếp goto về /app
        try:
            await page.goto(_GeminiSel.URL, timeout=config.PAGE_LOAD_TIMEOUT * 1000, wait_until="domcontentloaded")
            await page.wait_for_timeout(2_000)
            await self._dismiss_consent_modals(page)
        except Exception as exc:
            logger.warning("Lỗi khi chuyển về cuộc trò chuyện mới: %s", exc)

    async def _upload_reference_images(self, page: Page, images: List[Path]) -> None:
        """Tải các file ảnh tham chiếu lên ô nhập của Gemini."""
        if not images:
            return

        for img_path in images:
            logger.info("Đang upload ảnh tham chiếu lên Gemini: %s", img_path.name)
            if not img_path.exists():
                raise FileNotFoundError(f"Không tìm thấy file ảnh tham chiếu: {img_path}")

            # 1. Bấm nút mở menu tải lên nếu input file chưa hiện
            file_input = page.locator(_GeminiSel.FILE_INPUT)
            if await file_input.count() == 0:
                for up_sel in _GeminiSel.UPLOAD_MENU_BUTTONS:
                    try:
                        btn = page.locator(up_sel).first
                        if await btn.is_visible(timeout=2_000):
                            await btn.click()
                            await page.wait_for_timeout(1_000)
                            break
                    except Exception:
                        continue

            # 2. Gán file vào input[type=file]
            file_input = page.locator(_GeminiSel.FILE_INPUT).first
            await file_input.wait_for(state="attached", timeout=10_000)
            await file_input.set_input_files(str(img_path.resolve()))
            await page.wait_for_timeout(1_500)

            # 3. Kiểm tra xem có popup điều khoản ảnh không ("Tạo nội dung từ hình ảnh và tệp")
            for ag_sel in _GeminiSel.UPLOAD_AGREE_BUTTONS:
                try:
                    agree_btn = page.locator(ag_sel)
                    if await agree_btn.count() > 0 and await agree_btn.first.is_visible():
                        logger.info("Bấm 'Đồng ý' điều khoản tải ảnh lên Gemini...")
                        await agree_btn.first.click()
                        await page.wait_for_timeout(1_000)
                        break
                except Exception:
                    pass

            # 4. Chờ attachment thumbnail xuất hiện trong ô prompt
            try:
                await page.wait_for_selector(
                    "uploader-attachment, [class*='attachment'], [class*='thumbnail']",
                    timeout=15_000,
                )
                logger.info("Đã đính kèm ảnh tham chiếu thành công: %s", img_path.name)
            except Exception:
                logger.warning("Không thấy thẻ thumbnail preview của ảnh, nhưng file đã được nạp.")

    async def _type_prompt(self, page: Page, prompt: str) -> None:
        """Nhập prompt vào ô chat của Gemini."""
        logger.info("Nhập prompt lên Gemini: %s …", prompt[:80])
        box = page.locator(_GeminiSel.PROMPT_INPUT).first
        await box.wait_for(state="visible", timeout=config.PAGE_LOAD_TIMEOUT * 1000)
        await box.click()
        await page.wait_for_timeout(200)

        # Xóa nội dung cũ nếu có
        await page.keyboard.press("Control+a")
        await page.keyboard.press("Delete")

        # Chèn văn bản prompt
        await page.keyboard.insert_text(prompt)
        await page.wait_for_timeout(300)

        got = " ".join((await box.inner_text()).split())
        want = " ".join(prompt.split())
        if len(got) < len(want) * 0.9:
            logger.warning("Editor chưa nhận đủ ký tự khi chèn — gõ từng phím …")
            await box.click()
            await page.keyboard.press("Control+a")
            await page.keyboard.press("Delete")
            await page.keyboard.type(prompt, delay=5)

    async def _submit(self, page: Page) -> None:
        """Gửi prompt đi bằng cách click nút Gửi hoặc bấm Enter."""
        logger.info("Bấm gửi prompt lên Gemini …")
        for sel in _GeminiSel.SEND_BUTTONS:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=2_000):
                    await btn.click()
                    logger.info("Đã click nút gửi: %s", sel)
                    return
            except Exception:
                continue

        # Fallback: Bấm Enter
        logger.info("Dùng fallback phím Enter để gửi prompt.")
        box = page.locator(_GeminiSel.PROMPT_INPUT).first
        await box.click()
        await page.keyboard.press("Enter")

    async def _wait_for_generated_image(self, page: Page, known_srcs: set[str]) -> str:
        """
        Chờ Gemini sinh ảnh xong và trả về URL ảnh chất lượng cao (=s0).
        """
        logger.info("Đang chờ Gemini tạo ảnh (tối đa %ds) …", config.IMAGE_GENERATION_TIMEOUT)
        deadline = asyncio.get_event_loop().time() + config.IMAGE_GENERATION_TIMEOUT

        while asyncio.get_event_loop().time() < deadline:
            await page.wait_for_timeout(2_000)

            # Tìm các ảnh trong vùng response
            imgs_found = await page.evaluate(
                """(knownList) => {
                    const known = new Set(knownList);
                    const candidates = Array.from(document.querySelectorAll(
                        'div.overlay-container img.image, ' +
                        'button.image-button img, ' +
                        'model-response img[src*="googleusercontent"], ' +
                        'img.animate.loaded, ' +
                        'img[src*="lh3.googleusercontent.com/gg/"]'
                    ));
                    for (let i = candidates.length - 1; i >= 0; i--) {
                        const img = candidates[i];
                        const src = img.src || img.getAttribute('src');
                        if (src && !known.has(src)) {
                            if (img.complete && img.naturalWidth > 200) {
                                return { found: true, src: src, width: img.naturalWidth, height: img.naturalHeight };
                            }
                        }
                    }
                    return { found: false };
                }""",
                list(known_srcs),
            )

            if imgs_found.get("found"):
                raw_src = imgs_found["src"]
                logger.info(
                    "🎉 Gemini đã sinh ảnh thành công! Kích thước hiển thị: %dx%d, URL: %s …",
                    imgs_found["width"],
                    imgs_found["height"],
                    raw_src[:70],
                )
                # Đổi đuôi về =s0 để lấy độ phân giải gốc cao nhất
                full_res_src = re.sub(r"=s\d+(-[A-Za-z0-9-]+)?$", "=s0", raw_src)
                return full_res_src

        # Chụp ảnh màn hình lỗi nếu quá hạn
        try:
            shot_path = config.OUTPUT_DIR / "debug_gemini_timeout.png"
            await page.screenshot(path=str(shot_path))
            logger.error("Hết thời gian chờ ảnh Gemini — đã lưu ảnh chụp: %s", shot_path)
        except Exception:
            pass

        raise TimeoutError(f"Quá thời gian ({config.IMAGE_GENERATION_TIMEOUT}s) mà Gemini chưa sinh xong ảnh.")

    # ── Public API ───────────────────────────────────────────────────────────

    async def generate_image(
        self,
        prompt: str,
        aspect_ratio: Optional[str] = None,
        reference_images: Optional[List[Path]] = None,
        app_name: Optional[str] = None,
        **kwargs,
    ) -> Path:
        """
        Tạo ảnh từ Gemini Chat:
        - prompt: Mô tả ảnh cần tạo.
        - aspect_ratio: Tỷ lệ khung hình (16:9, 4:3, 1:1, 3:4, 9:16).
        - reference_images: Danh sách file ảnh mẫu.
        - app_name: Tên ứng dụng gọi (để phân luồng ghi log).
        """
        if not self._page or self._page.is_closed():
            await self.start()

        page = self._page

        # 1. Bắt đầu phiên chat mới
        await self._start_new_chat(page)

        # 2. Thu thập danh sách ảnh đã có trên màn hình trước khi gửi prompt
        existing_srcs = set(
            await page.evaluate(
                """() => Array.from(document.querySelectorAll('img')).map(i => i.src).filter(Boolean)"""
            )
        )

        # 3. Tải các ảnh tham chiếu lên nếu có
        if reference_images:
            await self._upload_reference_images(page, reference_images)

        # 4. Chuẩn hóa prompt để Gemini luôn nhận diện là yêu cầu tạo ảnh
        img_triggers = [
            "tạo ảnh", "vẽ", "hình ảnh", "bức ảnh", "bức tranh",
            "generate image", "create image", "draw", "photo of", "picture of", "image of"
        ]
        p_lower = prompt.lower()
        if not any(t in p_lower for t in img_triggers):
            final_prompt = f"Tạo hình ảnh chi tiết: {prompt}"
        else:
            final_prompt = prompt

        if aspect_ratio:
            final_prompt += f", tỷ lệ khung hình {aspect_ratio}"

        # 5. Nhập prompt & Gửi
        await self._type_prompt(page, final_prompt)
        await self._submit(page)

        # 6. Chờ ảnh hoàn tất và lấy URL độ phân giải gốc
        image_url = await self._wait_for_generated_image(page, existing_srcs)

        # 7. Tải dữ liệu ảnh về thư mục outputs/
        out_filename = f"{uuid.uuid4().hex}.png"
        out_path = (config.OUTPUT_DIR / out_filename).resolve()

        logger.info("Đang tải dữ liệu ảnh từ URL về đĩa: %s", out_path.name)
        try:
            resp = await page.request.get(image_url)
            if resp.ok:
                out_path.write_bytes(await resp.body())
                logger.info("✓ Đã lưu ảnh thành công: %s (%d bytes)", out_path.name, out_path.stat().st_size)
                return out_path
            else:
                logger.warning("HTTP %d khi tải ảnh từ URL, thử fallback sang screenshot...", resp.status)
        except Exception as exc:
            logger.warning("Lỗi tải ảnh qua page.request: %s. Thử fallback...", exc)

        # Fallback: Tìm element ảnh và chụp trực tiếp
        img_loc = page.locator(_GeminiSel.GENERATED_IMAGE).last
        await img_loc.screenshot(path=str(out_path))
        logger.info("✓ Đã lưu ảnh qua element screenshot: %s", out_path.name)
        return out_path
