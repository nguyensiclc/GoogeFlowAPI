# Hướng Dẫn Tích Hợp Dịch Vụ Tạo Ảnh (Dành Cho Ứng Dụng Khác)

Service tạo ảnh từ Google Flow đang chạy ngầm độc lập tại:
- **Base URL**: `http://localhost:8080`
- **Swagger Docs**: `http://localhost:8080/docs`

---

## 1. Phân Vùng Project Theo Từng Ứng Dụng (`app_name`)

Dịch vụ hỗ trợ **tự động tách riêng mỗi ứng dụng một Project độc lập trên Google Flow**:
- Mỗi khi app gửi request kèm thông số `app_name` (ví dụ: `"AppTruyenTranh"`, `"AppStory"`, `"AppMarketing"`):
  - Hệ thống sẽ tự động kiểm tra xem app đó đã có Project riêng trên Flow chưa.
  - **Nếu chưa có**: Tự động bấm "Dự án mới" trên flow.google.com để tạo một Project riêng cho app đó và lưu lại URL.
  - **Nếu đã có**: Trình duyệt tự chuyển ngay vào đúng Project của app đó để tạo ảnh.
  - Các app không truyền `app_name` sẽ dùng Project mặc định (`default`).
- **Lợi ích**: Ảnh của app truyện tranh, app làm video hay app marketing không bị lẫn lộn vào nhau trên giao diện Google Flow.

---

## 2. Đặc tả API

### Cách 1: Gọi JSON (`POST /api/v1/generate`)

Hỗ trợ cả prompt thuần túy và prompt **kèm ảnh tham chiếu** (qua URL web, Base64 hoặc đường dẫn file cục bộ).

#### A. Gửi danh sách kèm ảnh tham chiếu và app_name riêng cho từng prompt:
```json
{
  "app_name": "AppTruyenTranh",
  "items": [
    {
      "prompt": "Vẽ nhân vật này đang ngồi đọc sách trong quán cafe anime, ánh nắng ấm áp",
      "reference_image": "https://example.com/sample_character.png",
      "aspect_ratio": "9:16"
    },
    {
      "prompt": "Vẽ nhân vật theo phong cách tranh sơn dầu này",
      "reference_image": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAA...",
      "aspect_ratio": "16:9"
    }
  ]
}
```

#### B. Gửi 1 ảnh tham chiếu chung cho nhiều prompt:
```json
{
  "app_name": "AppVideoShorts",
  "prompts": [
    "Cảnh 1: Nhân vật đang đi dạo trên đường phố Tokyo",
    "Cảnh 2: Nhân vật đang thưởng thức trà chiều"
  ],
  "reference_image": "https://example.com/sample_character.png",
  "aspect_ratio": "9:16"
}
```
  "aspect_ratio": "9:16"
}
```

#### C. Gửi prompt thuần (không kèm ảnh tham chiếu):
```json
{
  "prompts": [
    "A cute red panda sitting on a tree branch, 4k"
  ],
  "aspect_ratio": "1:1"
}
```

- **Thông số loại khung hình (Aspect Ratio)**:
  - Có thể dùng một trong các tên trường: `aspect_ratio`, `khung_hinh`, hoặc `loai_khung_hinh`.
  - Các giá trị được Flow hỗ trợ chính xác theo menu:
    - `"16:9"`: Ngang rộng (YouTube, phong cảnh)
    - `"4:3"`: Ngang chuẩn
    - `"1:1"`: Vuông (Avatar, Instagram)
    - `"3:4"`: Dọc
    - `"9:16"`: Dọc toàn màn hình (TikTok, Reels, Shorts, Story)
  - Bỏ trống = giữ nguyên cài đặt đang chọn trên dự án Flow.
  - Driver tự động đảm bảo chọn chế độ **`x1`** (tạo 1 ảnh mỗi prompt để tránh lãng phí hạn ngạch).

---

### Cách 2: Upload trực tiếp File ảnh tham chiếu (`POST /api/v1/generate-with-file`)

Endpoint dạng **Multipart Form** (`multipart/form-data`) phù hợp khi ứng dụng của bạn có sẵn file nhị phân trên máy:

- **Fields**:
  - `prompt` (text): Mô tả ảnh cần tạo.
  - `reference_file` (file): File ảnh tham chiếu (PNG, JPG, WEBP).
  - `aspect_ratio` (text, tuỳ chọn): `"16:9"`, `"4:3"`, `"1:1"`, `"3:4"`, `"9:16"`.
  - `app_name` (text, tuỳ chọn): Tên ứng dụng để tự động phân vùng project trên Flow (ví dụ: `"AppTruyenTranh"`).

Ví dụ cURL:
```bash
curl -X POST "http://localhost:8080/api/v1/generate-with-file" \
  -F "app_name=AppTruyenTranh" \
  -F "prompt=Vẽ phong cảnh với phong cách màu sắc tương tự ảnh này" \
  -F "reference_file=@D:/Photos/my_style.jpg" \
  -F "aspect_ratio=9:16"
```

---

#### Response trả về chung (JSON):
```json
{
  "images": [
    {
      "prompt": "Vẽ nhân vật này đang ngồi đọc sách...",
      "file_path": "outputs\\uuid1.png",
      "image_url": "http://localhost:8080/outputs/uuid1.png",
      "base64": "iVBORw0KGgoAAAANSUhEUgAA...",
      "error": null
    }
  ],
  "total": 1,
  "failed": []
}
```

> **Lưu ý quan trọng**:
> - Mỗi ảnh mất khoảng 15s - 60s để Google Flow tạo xong. Client gọi API cần đặt **Timeout tối thiểu 120s - 300s** nếu gửi danh sách nhiều prompt.
> - Kiểm tra trường `error` của từng item trong mảng `images` (nếu `error != null` nghĩa là prompt đó lỗi).
> - Có thể dùng trực tiếp `image_url` để hiển thị trên Web/App hoặc tải về.

---

## 2. Code Mẫu Client Tích Hợp

### Python (Đồng bộ / requests)
```python
import requests
from typing import List, Dict, Any

class ImageGenClient:
    def __init__(self, base_url: str = "http://localhost:8080"):
        self.base_url = base_url.rstrip("/")

    def is_healthy(self) -> bool:
        try:
            res = requests.get(f"{self.base_url}/api/v1/health", timeout=5)
            return res.status_code == 200
        except Exception:
            return False

    def generate(
        self,
        prompts: List[str],
        reference_image: Optional[str] = None,
        aspect_ratio: Optional[str] = None,
        timeout: int = 300,
    ) -> List[Dict[str, Any]]:
        """
        Nhận danh sách prompt (kèm ảnh tham chiếu tùy chọn: URL, base64 hoặc file path),
        trả về danh sách kết quả ảnh.
        """
        payload = {"prompts": prompts}
        if reference_image:
            payload["reference_image"] = reference_image
        if aspect_ratio:
            payload["aspect_ratio"] = aspect_ratio

        res = requests.post(
            f"{self.base_url}/api/v1/generate",
            json=payload,
            timeout=timeout,
        )
        res.raise_for_status()
        return res.json().get("images", [])

# Sử dụng:
if __name__ == "__main__":
    client = ImageGenClient()
    # 1. Tạo có kèm ảnh mẫu phong cách
    results = client.generate(
        prompts=["A character drinking bubble tea in this art style"],
        reference_image="https://example.com/anime_sample.png",
        aspect_ratio="9:16",
    )
    for img in results:
        if not img.get("error"):
            print("Ảnh URL:", img["image_url"])
        else:
            print("Lỗi:", img["error"])
```

### Python (Bất đồng bộ / httpx)
```python
import httpx

async def generate_images(prompts: list[str]) -> list[dict]:
    async with httpx.AsyncClient(timeout=300.0) as client:
        response = await client.post(
            "http://localhost:8000/api/v1/generate",
            json={"prompts": prompts}
        )
        response.raise_for_status()
        return response.json().get("images", [])
```

### TypeScript / JavaScript (Node.js hoặc Frontend)
```typescript
interface ImageResult {
  prompt: string;
  file_path: string | null;
  image_url: string | null;
  base64: string | null;
  error: string | null;
}

interface GenerateResponse {
  images: ImageResult[];
  total: number;
  failed: string[];
}

export async function generateImages(
  prompts: string[],
  baseUrl: string = "http://localhost:8000"
): Promise<ImageResult[]> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 300_000); // 5 phút timeout

  try {
    const res = await fetch(`${baseUrl}/api/v1/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompts }),
      signal: controller.signal,
    });

    if (!res.ok) {
      throw new Error(`API Error: ${res.status} ${res.statusText}`);
    }

    const data: GenerateResponse = await res.json();
    return data.images;
  } finally {
    clearTimeout(timeoutId);
  }
}
```
