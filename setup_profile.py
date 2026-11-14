"""
setup_profile.py

Chay MOT LAN duy nhat de dang nhap Google vao profile rieng cua app.
Cach dung:
    .venv/Scripts/python.exe setup_profile.py

Script se mo Chrome voi profile tai .chrome_profile/
Ban dang nhap Google binh thuong, sau do vao flow.google.com
de chac chan da duoc grant access.
Khi xong, dong cua so Chrome -- session se duoc luu lai.
"""

import subprocess
from pathlib import Path

import config


def main() -> None:
    profile_dir = Path(config.CHROME_USER_DATA_DIR)
    print("=" * 60)
    print("  Setup Chrome Profile cho Image Generation Service")
    print("=" * 60)
    print()
    print(f"Profile dir : {profile_dir}")
    print(f"Chrome      : {config.CHROME_EXECUTABLE}")
    print()
    print("Chrome se mo voi profile trang (chua dang nhap).")
    print("Hay lam cac buoc sau:")
    print("  1. Dang nhap tai khoan Google")
    print("  2. Truy cap https://flow.google.com va chac chan co quyen truy cap")
    print("  3. Dong cua so Chrome khi xong")
    print()
    print("Nhan Enter de mo Chrome...")
    input()

    cmd = [
        config.CHROME_EXECUTABLE,
        f"--user-data-dir={profile_dir}",
        f"--profile-directory={config.CHROME_PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        "https://accounts.google.com",   # mo thang trang dang nhap
    ]

    print("Dang mo Chrome...")
    proc = subprocess.Popen(cmd)
    proc.wait()

    print()
    print("[OK] Profile da duoc luu tai:", profile_dir)
    print("     Bay gio ban co the chay server binh thuong:")
    print("     .venv/Scripts/python.exe -m app.main")


if __name__ == "__main__":
    main()
