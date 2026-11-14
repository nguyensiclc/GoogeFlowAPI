"""
API routes cho Image Generation Service và Quản lý cài đặt / tài khoản.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional, List
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form
from pydantic import BaseModel, Field

import config
from app.services.account_service import AccountManager
from app.services.image_service import TaskItem
from app.services.media_helper import prepare_reference_images, TEMP_REF_DIR

router = APIRouter()


# ─── Models ──────────────────────────────────────────────────────────────────

class TaskItemModel(BaseModel):
    prompt: str = Field(
        ...,
        description="Nội dung prompt tạo ảnh",
        examples=["A beautiful portrait in this exact watercolor style"],
    )
    reference_image: Optional[str] = Field(
        None,
        description="URL ảnh (http/https), chuỗi Base64 hoặc đường dẫn file cục bộ của ảnh tham chiếu",
    )
    reference_images: Optional[list[str]] = Field(
        None,
        description="Danh sách nhiều ảnh tham chiếu nếu cần",
    )
    aspect_ratio: Optional[Literal["16:9", "4:3", "1:1", "3:4", "9:16"]] = Field(
        None,
        description="Tỷ lệ khung hình: 16:9, 4:3, 1:1, 3:4, 9:16",
    )
    khung_hinh: Optional[Literal["16:9", "4:3", "1:1", "3:4", "9:16"]] = Field(
        None,
        description="Bí danh tiếng Việt của aspect_ratio",
    )
    loai_khung_hinh: Optional[Literal["16:9", "4:3", "1:1", "3:4", "9:16"]] = Field(
        None,
        description="Bí danh tiếng Việt của aspect_ratio",
    )

    def get_aspect_ratio(self) -> Optional[str]:
        return self.aspect_ratio or self.khung_hinh or self.loai_khung_hinh


class GenerateRequest(BaseModel):
    prompts: Optional[list[str]] = Field(
        None,
        description="Một hoặc nhiều text prompt đơn giản (dùng khi không truyền items).",
        examples=[["a sunset over mountains"]],
    )
    items: Optional[list[TaskItemModel]] = Field(
        None,
        description="Danh sách tác vụ chi tiết, mỗi mục có thể kèm ảnh tham chiếu và loại khung hình riêng.",
    )
    reference_image: Optional[str] = Field(
        None,
        description="Ảnh tham chiếu áp dụng chung cho tất cả các prompts trong request (URL, Base64 hoặc path file).",
    )
    reference_images: Optional[list[str]] = Field(
        None,
        description="Danh sách nhiều ảnh tham chiếu áp dụng chung cho tất cả các prompts.",
    )
    aspect_ratio: Optional[Literal["16:9", "4:3", "1:1", "3:4", "9:16"]] = Field(
        default=None,
        description="Tỷ lệ khung hình cho mọi ảnh trong lần gọi này. Hỗ trợ: 16:9, 4:3, 1:1, 3:4, 9:16. Bỏ trống = giữ cài đặt hiện tại của Flow.",
        examples=["9:16"],
    )
    khung_hinh: Optional[Literal["16:9", "4:3", "1:1", "3:4", "9:16"]] = Field(
        default=None,
        description="Bí danh tiếng Việt của aspect_ratio",
        examples=["3:4"],
    )
    loai_khung_hinh: Optional[Literal["16:9", "4:3", "1:1", "3:4", "9:16"]] = Field(
        default=None,
        description="Bí danh tiếng Việt của aspect_ratio",
        examples=["16:9"],
    )

    def get_aspect_ratio(self) -> Optional[str]:
        return self.aspect_ratio or self.khung_hinh or self.loai_khung_hinh


class ImageResult(BaseModel):
    prompt: str
    file_path: str | None = None
    image_url: str | None = None
    base64: str | None = None
    error: str | None = None


class GenerateResponse(BaseModel):
    images: list[ImageResult]
    total: int
    failed: list[str]


class ProjectInfo(BaseModel):
    project_url: str | None
    created_at: str | None


class SetProjectRequest(BaseModel):
    project_url: str = Field(
        ...,
        description="URL project muốn dùng. Dạng: https://flow.google.com/project/<id>",
        examples=["https://flow.google.com/project/b77249bb-f939-4cc4-8a55-c9aa914782ae"],
    )


class CreateAccountRequest(BaseModel):
    name: str = Field(
        ...,
        min_length=1,
        description="Tên gợi nhớ cho tài khoản Google (ví dụ: Tài khoản Pro, Account 2...)",
        examples=["Tài khoản Pro"],
    )


class ToggleServiceRequest(BaseModel):
    enable: Optional[bool] = Field(
        default=None,
        description="True để bật, False để tắt, None để đảo ngược trạng thái hiện tại.",
    )


# ─── Dependency ──────────────────────────────────────────────────────────────

def get_service(request: Request):
    svc = request.app.state.image_service
    if svc is None:
        raise HTTPException(status_code=503, detail="Dịch vụ tạo ảnh chưa sẵn sàng.")
    return svc


# ─── Image generation ────────────────────────────────────────────────────────

@router.post(
    "/generate",
    response_model=GenerateResponse,
    summary="Tạo ảnh từ danh sách prompt (hỗ trợ kèm ảnh tham chiếu)",
)
async def generate_images(
    body: GenerateRequest,
    request: Request,
    service=Depends(get_service),
):
    """
    Tạo ảnh trên Google Flow:
    - Nhận danh sách text prompt (`prompts`), hoặc
    - Nhận danh sách chi tiết từng tác vụ (`items`), mỗi task có thể kèm ảnh tham chiếu riêng.
    - Hỗ trợ ảnh tham chiếu dạng URL, Base64 hoặc đường dẫn file cục bộ.
    """
    tasks: List[TaskItem] = []
    common_ratio = body.get_aspect_ratio()

    # 1. Nếu truyền items chi tiết
    if body.items:
        for it in body.items:
            sources: List[str] = []
            if it.reference_image:
                sources.append(it.reference_image)
            if it.reference_images:
                sources.extend(it.reference_images)
            if not sources and body.reference_image:
                sources.append(body.reference_image)
            if not sources and body.reference_images:
                sources.extend(body.reference_images)

            try:
                ref_paths = prepare_reference_images(sources)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"Lỗi nạp ảnh tham chiếu cho prompt '{it.prompt[:50]}': {exc}")

            ratio = it.get_aspect_ratio() or common_ratio
            tasks.append(TaskItem(prompt=it.prompt, reference_images=ref_paths, aspect_ratio=ratio))

    # 2. Nếu truyền prompts đơn giản
    elif body.prompts:
        common_sources: List[str] = []
        if body.reference_image:
            common_sources.append(body.reference_image)
        if body.reference_images:
            common_sources.extend(body.reference_images)

        try:
            common_ref_paths = prepare_reference_images(common_sources)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Lỗi nạp ảnh tham chiếu: {exc}")

        for p in body.prompts:
            tasks.append(TaskItem(prompt=p, reference_images=common_ref_paths, aspect_ratio=common_ratio))

    else:
        raise HTTPException(
            status_code=400,
            detail="Vui lòng cung cấp 'prompts' (danh sách prompt) hoặc 'items' (danh sách task).",
        )

    try:
        results = await service.generate_tasks(tasks)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    images: list[ImageResult] = []
    base_url = str(request.base_url).rstrip("/")

    for r in results:
        file_path_str = str(r.file_path) if r.file_path and r.file_path.name else None
        image_url = f"{base_url}/outputs/{Path(r.file_path).name}" if file_path_str and not r.error else None

        images.append(
            ImageResult(
                prompt=r.prompt,
                file_path=file_path_str,
                image_url=image_url,
                base64=r.base64,
                error=r.error,
            )
        )

    return GenerateResponse(
        images=images,
        total=len(images),
        failed=[r.prompt for r in results if r.error],
    )


@router.post(
    "/generate-with-file",
    response_model=GenerateResponse,
    summary="Tạo ảnh bằng cách upload trực tiếp file ảnh tham chiếu (Multipart Form)",
)
async def generate_with_file(
    prompt: str = Form(..., description="Prompt mô tả ảnh cần tạo"),
    reference_file: UploadFile = File(..., description="File ảnh tham chiếu (PNG, JPG, WEBP...)"),
    aspect_ratio: Optional[Literal["16:9", "4:3", "1:1", "3:4", "9:16"]] = Form(
        None, description="Tỷ lệ khung hình: 16:9, 4:3, 1:1, 3:4, 9:16"
    ),
    khung_hinh: Optional[Literal["16:9", "4:3", "1:1", "3:4", "9:16"]] = Form(
        None, description="Bí danh tiếng Việt của aspect_ratio"
    ),
    loai_khung_hinh: Optional[Literal["16:9", "4:3", "1:1", "3:4", "9:16"]] = Form(
        None, description="Bí danh tiếng Việt của aspect_ratio"
    ),
    request: Request = None,
    service=Depends(get_service),
):
    """
    Endpoint dạng Multipart Form giúp các ứng dụng khác có thể upload trực tiếp file nhị phân làm ảnh tham chiếu.
    """
    ext = Path(reference_file.filename).suffix or ".png"
    target_path = TEMP_REF_DIR / f"upload_{uuid.uuid4().hex}{ext}"

    content = await reference_file.read()
    target_path.write_bytes(content)

    chosen_ratio = aspect_ratio or khung_hinh or loai_khung_hinh
    task = TaskItem(
        prompt=prompt,
        reference_images=[target_path.resolve()],
        aspect_ratio=chosen_ratio,
    )

    try:
        results = await service.generate_tasks([task])
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    images: list[ImageResult] = []
    base_url = str(request.base_url).rstrip("/")

    for r in results:
        file_path_str = str(r.file_path) if r.file_path and r.file_path.name else None
        image_url = f"{base_url}/outputs/{Path(r.file_path).name}" if file_path_str and not r.error else None

        images.append(
            ImageResult(
                prompt=r.prompt,
                file_path=file_path_str,
                image_url=image_url,
                base64=r.base64,
                error=r.error,
            )
        )

    return GenerateResponse(
        images=images,
        total=len(images),
        failed=[r.prompt for r in results if r.error],
    )


# ─── Settings & Status ───────────────────────────────────────────────────────

@router.get("/settings", summary="Lấy toàn bộ trạng thái hệ thống cho trang cài đặt")
async def get_settings(service=Depends(get_service)):
    """Trả về trạng thái bật/tắt, tài khoản Google hiện tại, project và các tài khoản có sẵn."""
    return service.get_system_status()


@router.post("/settings/toggle", summary="Bật hoặc Tắt dịch vụ")
async def toggle_service(
    body: Optional[ToggleServiceRequest] = None,
    service=Depends(get_service),
):
    """Bật/tắt trạng thái nhận request của service."""
    enable_val = body.enable if body else None
    new_state = await service.toggle_service(enable=enable_val)
    return {
        "is_enabled": new_state,
        "message": f"Dịch vụ đã được {'BẬT' if new_state else 'TẮT'}.",
    }


# ─── Account Management ──────────────────────────────────────────────────────

@router.get("/accounts", summary="Danh sách tài khoản Google")
async def list_accounts():
    """Lấy danh sách các tài khoản Google đã cấu hình."""
    return AccountManager.list_accounts()


@router.post("/accounts", summary="Thêm tài khoản Google mới")
async def create_account(body: CreateAccountRequest):
    """Tạo tài khoản Google mới với profile riêng."""
    acc = AccountManager.create_account(name=body.name)
    return {
        "account": acc,
        "message": f"Đã tạo tài khoản '{acc['name']}'. Bấm 'Đăng nhập' để mở Chrome đăng nhập Google.",
    }


@router.post("/accounts/{account_id}/switch", summary="Chuyển sang tài khoản Google này")
async def switch_account(account_id: str, service=Depends(get_service)):
    """Chuyển đổi tài khoản Google đang dùng và tải lại phiên trình duyệt."""
    try:
        acc = await service.switch_account(account_id)
        return {
            "account": acc,
            "message": f"Đã chuyển sang tài khoản '{acc['name']}'.",
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/accounts/{account_id}/login", summary="Mở Chrome để đăng nhập tài khoản")
async def login_account(account_id: str):
    """Mở trình duyệt Chrome với profile của tài khoản này để người dùng đăng nhập tài khoản Google."""
    try:
        AccountManager.launch_login_browser(account_id)
        return {
            "message": "Trình duyệt Chrome đã mở. Hãy đăng nhập tài khoản Google và vào flow.google.com, sau đó đóng cửa sổ.",
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/accounts/{account_id}", summary="Xóa tài khoản Google")
async def delete_account(account_id: str):
    """Xóa một tài khoản khỏi danh sách."""
    try:
        AccountManager.delete_account(account_id)
        return {"message": "Đã xóa tài khoản thành công."}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ─── Project management ──────────────────────────────────────────────────────

@router.get("/project", response_model=ProjectInfo, summary="Xem project đang dùng")
async def get_project(service=Depends(get_service)):
    info = service.get_project_info()
    return ProjectInfo(**info)


@router.put("/project", response_model=ProjectInfo, summary="Chuyển sang dùng project khác")
async def set_project(body: SetProjectRequest, service=Depends(get_service)):
    if "/project/" not in body.project_url:
        raise HTTPException(
            status_code=400,
            detail="URL không hợp lệ. Phải có dạng https://flow.google.com/project/<id>",
        )
    info = await service.switch_project(body.project_url)
    return ProjectInfo(**info)


@router.post("/project/new", response_model=ProjectInfo, summary="Tạo project mới trên flow.google.com")
async def create_new_project(service=Depends(get_service)):
    info = await service.create_new_project()
    return ProjectInfo(**info)


# ─── Gallery ─────────────────────────────────────────────────────────────────

@router.get("/gallery", summary="Danh sách ảnh gần đây")
async def get_gallery(request: Request, limit: int = 24):
    """Lấy danh sách các ảnh gần đây nhất đã tạo."""
    output_dir = Path(config.OUTPUT_DIR)
    images = []
    base_url = str(request.base_url).rstrip("/")

    if output_dir.exists():
        files = sorted(
            output_dir.glob("*.png"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        for f in files[:limit]:
            images.append({
                "filename": f.name,
                "url": f"{base_url}/outputs/{f.name}",
                "created_at": f.stat().st_mtime,
                "size_bytes": f.stat().st_size,
            })

    return {"images": images, "total": len(images)}


# ─── Health ──────────────────────────────────────────────────────────────────

@router.get("/health", summary="Health check")
async def health(request: Request):
    svc = request.app.state.image_service
    is_ready = svc is not None and svc.is_enabled
    return {
        "status": "ok" if is_ready else "paused",
        "service_ready": svc is not None,
        "is_enabled": svc.is_enabled if svc else False,
    }
