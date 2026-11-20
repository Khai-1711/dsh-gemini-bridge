import base64
import io
import os
import tempfile
from typing import Any, List, Tuple
from app.config import MAX_IMAGES_PER_TURN, logger

try:
    from PIL import Image
    HAS_PILLOW = True
    Image.MAX_IMAGE_PIXELS = 50_000_000  # 50 MP, đủ cho ảnh 8K, chống zip bomb
except ImportError:
    HAS_PILLOW = False

MAX_BASE64_LENGTH = 8 * 1024 * 1024  # 8MB

_TEMP_FILES: set = set()


# =====================================================================
# XỬ LÝ ẢNH ĐA PHƯƠNG THỨC (Multimodal Vision)
# =====================================================================

def _decode_compress_and_save_image(url_or_path: str) -> str:
    """Giải mã Base64, nén ảnh về JPEG nhẹ (nếu có Pillow) hoặc lưu trực tiếp."""
    if not url_or_path:
        return ""

    if url_or_path.startswith("data:image/"):
        if len(url_or_path) > MAX_BASE64_LENGTH:
            logger.warning("Bỏ qua ảnh Base64 quá lớn (>8MB).")
            return ""
        try:
            header, b64_data = url_or_path.split(",", 1)
            raw_bytes = base64.b64decode(b64_data)

            if HAS_PILLOW:
                with Image.open(io.BytesIO(raw_bytes)) as img:
                    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                        background = Image.new("RGB", img.size, (255, 255, 255))
                        alpha = img.convert("RGBA").split()[-1]
                        background.paste(img, mask=alpha)
                        img = background
                    else:
                        img = img.convert("RGB")
                    img.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                    fd, tmp_path = tempfile.mkstemp(suffix=".jpg", prefix="dsh_vision_")
                    with os.fdopen(fd, "wb") as f:
                        img.save(f, format="JPEG", quality=85, optimize=True)
            else:
                ext = ".png" if "png" in header.lower() else ".jpg"
                fd, tmp_path = tempfile.mkstemp(suffix=ext, prefix="dsh_vision_")
                with os.fdopen(fd, "wb") as f:
                    f.write(raw_bytes)

            del raw_bytes
            _TEMP_FILES.add(tmp_path)
            return tmp_path
        except Exception as e:
            logger.warning(f"Lỗi xử lý ảnh: {e}")
            return ""

    return ""


def extract_text_and_images(content: Any) -> Tuple[str, List[str]]:
    """Bóc tách đồng thời văn bản và danh sách đường dẫn file ảnh tạm."""
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []

    text_parts: List[str] = []
    temp_image_paths: List[str] = []

    if isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                text_parts.append(item)
            elif isinstance(item, dict):
                item_type = item.get("type", "")

                if item_type == "text" or "text" in item:
                    text_parts.append(str(item.get("text", "")))

                elif len(temp_image_paths) < MAX_IMAGES_PER_TURN:
                    if item_type == "image_url" or "image_url" in item:
                        img_obj = item.get("image_url", {})
                        url_data = (
                            img_obj.get("url", "")
                            if isinstance(img_obj, dict)
                            else str(img_obj)
                        )
                        saved = _decode_compress_and_save_image(url_data)
                        if saved:
                            temp_image_paths.append(saved)
                    elif item_type == "image" and isinstance(item.get("source"), dict):
                        src = item["source"]
                        if src.get("type") == "base64" and src.get("data"):
                            media_type = src.get("media_type", "image/png")
                            data_uri = f"data:{media_type};base64,{src['data']}"
                            saved = _decode_compress_and_save_image(data_uri)
                            if saved:
                                temp_image_paths.append(saved)
                else:
                    if item_type in ("image_url", "image") or "image_url" in item:
                        logger.warning(
                            f"Bỏ qua ảnh #{len(temp_image_paths) + 1} "
                            f"(vượt giới hạn {MAX_IMAGES_PER_TURN}/lượt)"
                        )

    return "\n".join(text_parts), temp_image_paths


def cleanup_temp_images(image_paths: List[str]) -> None:
    """Dọn dẹp file ảnh tạm sau khi đã gửi lên Gemini Web."""
    for path in image_paths:
        if path in _TEMP_FILES:
            try:
                os.remove(path)
            except FileNotFoundError:
                _TEMP_FILES.discard(path)
            except OSError as exc:
                logger.warning(
                    "Could not remove temporary image file (%s).",
                    type(exc).__name__,
                )
            else:
                _TEMP_FILES.discard(path)
