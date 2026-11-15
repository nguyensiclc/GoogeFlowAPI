# Image Generation Service (Google Flow API)

Dịch vụ REST API độc lập (FastAPI) nhận input là prompt tạo ảnh, tự động hóa thao tác trên **flow.google.com** qua Playwright, và trả về kết quả ảnh cho các ứng dụng khác kết nối.

---

## 🌟 Tính năng chính

- **API tập trung**: Nhận 1 hoặc một danh sách nhiều prompt (`list[str]`), sinh ảnh tuần tự, nhận bao nhiêu prompt trả về bấy nhiêu ảnh.
- **Phân vùng Project tự động theo từng App (`app_name`)**: Mỗi ứng dụng gọi API truyền `app_name` (ví dụ `AppTruyenTranh`, `AppStory`) sẽ tự động được cấp và duy trì một Project riêng trên Google Flow mà không bị lẫn ảnh.
- **Hỗ trợ ảnh tham chiếu (Reference Image)**: Nhận ảnh qua URL, Base64 hoặc upload file trực tiếp (`POST /api/v1/generate-with-file`).
- **Tùy chỉnh tỷ lệ khung hình (Aspect Ratio)**: Hỗ trợ `16:9`, `4:3`, `1:1`, `3:4`, `9:16`, tự động khóa chế độ `x1` để tiết kiệm hạn ngạch.
- **Output đa dạng**: Trả về cả **đường dẫn file cục bộ (`file_path`)**, **đường link trực tiếp (`image_url`)**, và **chuỗi `base64`**.
- **Quản lý đa tài khoản Google**: Dễ dàng thêm, chuyển đổi tài khoản và bật/tắt dịch vụ qua giao diện Cài đặt trực quan (`http://localhost:8080`).

---

## 📋 Yêu cầu hệ thống

| Thành phần | Phiên bản |
|---|---|
| Python | 3.11+ (hiện đang dùng 3.12) |
| Google Chrome | Đã cài đặt trên máy |
| Playwright | Đã cài trong virtual environment |

---

## 🚀 Cài đặt & Chuẩn bị

### 1. Kích hoạt môi trường ảo

```powershell
cd d:\Code_Carrer\ClaudeCer\CreateImg
.\.venv\Scripts\Activate.ps1
```

*(Nếu cài đặt trên máy mới, chạy: `pip install -r requirements.txt` và `playwright install chromium`)*

### 2. Thiết lập Profile Chrome (Chỉ chạy 1 LẦN DUY NHẤT)

```powershell
.\.venv\Scripts\python.exe setup_profile.py
```

- Cửa sổ Chrome mở ra với profile riêng `.chrome_profile/`.
- Đăng nhập tài khoản Google của bạn.
- Truy cập thử `https://flow.google.com` để cấp quyền/chấp nhận điều khoản nếu có.
- Đóng cửa sổ Chrome. Profile sẽ được lưu vĩnh viễn cho app dùng.

---

## ▶️ Khởi động Server

```powershell
.\.venv\Scripts\python.exe -m app.main
```

- **Giao diện Cài đặt (Settings UI)**: `http://localhost:8080/` hoặc `http://localhost:8080/settings`
- **Swagger Docs**: `http://localhost:8080/docs`
- **API Endpoint**: `http://localhost:8080/api/v1/generate`

---

## 📡 Tài liệu API cho các ứng dụng khác kết nối

### 1. Tạo ảnh (`POST /api/v1/generate`)

#### Request:
- **URL**: `http://localhost:8080/api/v1/generate`
- **Method**: `POST`
- **Headers**: `Content-Type: application/json`
- **Body (Tạo kèm ảnh tham chiếu URL/Base64/File Path)**:
```json
{
  "app_name": "AppTruyenTranh",
  "items": [
    {
      "prompt": "Vẽ nhân vật anime theo phong cách tranh này",
      "reference_image": "https://example.com/character.png",
      "aspect_ratio": "9:16"
    }
  ]
}
```

*Hoặc tạo nhanh nhiều prompt với 1 ảnh tham chiếu chung:*
```json
{
  "prompts": ["Cảnh 1 trong công viên", "Cảnh 2 trên đường phố"],
  "reference_image": "https://example.com/character.png",
  "aspect_ratio": "9:16"
}
```

*Hoặc upload file trực tiếp:*
`POST /api/v1/generate-with-file` (Multipart Form với `prompt`, `reference_file`, `aspect_ratio`).

#### Response:
```json
{
  "images": [
    {
      "prompt": "Vẽ nhân vật anime...",
      "file_path": "outputs\\a1b2c3d4e5f6.png",
      "image_url": "http://localhost:8080/outputs/a1b2c3d4e5f6.png",
      "base64": "iVBORw0KGgoAAAANSUhEUgAA...",
      "error": null
    }
  ],
  "total": 1,
  "failed": []
}
```

> **Gợi ý**: Ứng dụng khác có thể dùng ngay `image_url` để hiển thị trên web, hoặc tải ảnh về thông qua URL `http://localhost:8080/outputs/<tên-file>.png`.

---

### 2. Quản lý Project

| Endpoint | Method | Chức năng | Body |
|---|---|---|---|
| `/api/v1/project` | `GET` | Xem thông tin Project hiện tại đang dùng | Không có |
| `/api/v1/project` | `PUT` | Chuyển sang 1 project có sẵn khác | `{"project_url": "https://flow.google.com/project/<id>"}` |
| `/api/v1/project/new` | `POST` | Tự động tạo 1 Project hoàn toàn mới trên Flow | Không có |
| `/api/v1/health` | `GET` | Kiểm tra tình trạng server | Không có |

---

## 💻 Code mẫu tích hợp từ các ứng dụng khác

### Python (`requests`)
```python
import requests

res = requests.post(
    "http://localhost:8000/api/v1/generate",
    json={"prompts": ["a cute red panda sitting on a tree branch, 4k"]}
)
data = res.json()

for img in data["images"]:
    if not img["error"]:
        print(f"Prompt: {img['prompt']}")
        print(f"Ảnh URL: {img['image_url']}")
        print(f"File cục bộ: {img['file_path']}")
```

### Node.js / TypeScript (`fetch`)
```javascript
const response = await fetch("http://localhost:8000/api/v1/generate", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    prompts: [
      "A futuristic electric car driving on Mars",
      "A watercolor painting of a sunflower field"
    ]
  })
});

const result = await response.json();
console.log(result.images);
```

### cURL
```bash
curl -X POST "http://localhost:8000/api/v1/generate" \
  -H "Content-Type: application/json" \
  -d '{"prompts": ["sunset at beach", "night city skyline"]}'
```

---

## 📂 Cấu trúc thư mục

```
CreateImg/
├── .chrome_profile/         ← Profile Chrome riêng của app (đã gitignore)
├── .flow_project.json       ← Lưu project_url hiện tại (đã gitignore)
├── outputs/                 ← Chứa các file ảnh đã tải về (được mount qua /outputs)
├── app/
│   ├── main.py              ← FastAPI entry point + lifespan + CORS + static files
│   ├── api/
│   │   └── routes.py        ← Endpoints: /generate, /project, /health
│   ├── services/
│   │   └── image_service.py ← Quản lý quá trình sinh ảnh tuần tự & project
│   └── drivers/
│       ├── google_flow.py   ← Playwright automation điều khiển flow.google.com
│       └── project_store.py ← Quản lý đọc/ghi file .flow_project.json
├── setup_profile.py         ← Script mở Chrome đăng nhập lần đầu
├── config.py                ← Cấu hình (thư mục, timeout, Chrome path...)
├── requirements.txt         ← Danh sách thư viện Python
└── README.md
```
