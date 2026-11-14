"""
Application configuration.
"""

import os
from pathlib import Path

# ─── Chrome / Playwright ────────────────────────────────────────────────────────

# Thư mục profile Chrome riêng cho app này — hoàn toàn tách biệt khỏi
# Chrome cá nhân. Lần đầu chạy setup_profile.py để đăng nhập Google.
CHROME_USER_DATA_DIR: str = os.getenv(
    "CHROME_USER_DATA_DIR",
    str(Path(__file__).parent / ".chrome_profile"),
)

# Tên profile bên trong user-data-dir (mặc định "Default" là ổn)
CHROME_PROFILE: str = os.getenv("CHROME_PROFILE", "Default")

# Đường dẫn Chrome executable trên máy
CHROME_EXECUTABLE: str = os.getenv(
    "CHROME_EXECUTABLE",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)

# Chạy browser ở chế độ có cửa sổ (False) hay headless (True).
# Giữ False để tránh bị Google block bot-detection.
HEADLESS: bool = os.getenv("HEADLESS", "false").lower() == "true"

# ─── Google Flow ────────────────────────────────────────────────────────────────
FLOW_URL: str = "https://flow.google.com"
# Project URL được tự động tạo và lưu vào .flow_project.json
# Không cần cấu hình thủ công.

# Seconds to wait for the image to appear after clicking Generate
IMAGE_GENERATION_TIMEOUT: int = int(os.getenv("IMAGE_GENERATION_TIMEOUT", "120"))

# Seconds to wait for page elements to become interactive
PAGE_LOAD_TIMEOUT: int = int(os.getenv("PAGE_LOAD_TIMEOUT", "30"))

# ─── Output ─────────────────────────────────────────────────────────────────────
OUTPUT_DIR: Path = Path(os.getenv("OUTPUT_DIR", "outputs"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ─── API ────────────────────────────────────────────────────────────────────────
# Đổi sang 127.0.0.1 và port 8080 để tránh bị xung đột với các phần mềm khác (như JONSBO Monitor trên port 8000)
API_HOST: str = os.getenv("API_HOST", "127.0.0.1")
API_PORT: int = int(os.getenv("API_PORT", "8080"))
