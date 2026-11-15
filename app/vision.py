import base64
import io
import os
import shutil
import subprocess
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

_rapid_ocr = None
_rapid_ocr_checked = False


def _try_get_rapid_ocr():
    """Thử nạp RapidOCR nếu đã cài."""
    global _rapid_ocr, _rapid_ocr_checked
    if not _rapid_ocr_checked:
        _rapid_ocr_checked = True
        try:
            from rapidocr_onnxruntime import RapidOCR
            _rapid_ocr = RapidOCR()
            logger.info("Đã kích hoạt engine RapidOCR.")
        except Exception:
            _rapid_ocr = None
    return _rapid_ocr


def _windows_native_ocr(image_path: str) -> str:
    """Tầng 2a: Windows 11 Native OCR (Windows.Media.Ocr)."""
    if os.name != "nt":
        return ""

    abs_path = os.path.abspath(image_path).replace("'", "''")
    ps_script = f"""
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]

    function Await($WinRtTask, $ResultType) {{
        $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {{
            $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.IsGenericMethod
        }})[0].MakeGenericMethod($ResultType)
        $netTask = $asTask.Invoke($null, @($WinRtTask))
        $netTask.Wait(-1) | Out-Null
        return $netTask.Result
    }}

    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync('{abs_path}')) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    if ($null -eq $engine) {{
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new('en-US'))
    }}
    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    foreach ($line in $result.Lines) {{
        Write-Output $line.Text
    }}
    $stream.Dispose()
    """
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=15,
        )
        out = (proc.stdout or "").strip()
        if out:
            return out
    except Exception as e:
        logger.warning(f"Windows Native OCR lỗi: {e}")
    return ""


def _tesseract_ocr(image_path: str) -> str:
    """Tầng 2b: Tesseract OCR (cross-platform: Linux/Mac/Windows)."""
    tesseract_cmd = shutil.which("tesseract")
    if not tesseract_cmd:
        return ""
    # Thử eng+vie trước
    try:
        proc = subprocess.run(
            [tesseract_cmd, image_path, "stdout", "-l", "eng+vie"],
            capture_output=True, text=True, timeout=15,
        )
        out = (proc.stdout or "").strip()
        if out:
            return out
    except Exception:
        pass

    # Fallback chỉ dùng eng nếu eng+vie thất bại hoặc rỗng
    try:
        proc = subprocess.run(
            [tesseract_cmd, image_path, "stdout", "-l", "eng"],
            capture_output=True, text=True, timeout=15,
        )
        return (proc.stdout or "").strip()
    except Exception as e:
        logger.warning(f"Tesseract OCR lỗi: {e}")
        return ""


def extract_text_from_image_local(image_path: str) -> str:
    """
    Trích xuất chữ từ ảnh chụp màn hình (3 tầng fallback):
    1. RapidOCR (nếu đã cài rapidocr-onnxruntime)
    2a. Windows 11 Native OCR (nếu trên Windows)
    2b. Tesseract OCR (cross-platform, cần cài tesseract)
    """
    if not image_path or not os.path.exists(image_path):
        return "[Không tìm thấy file ảnh]"

    # Tầng 1: RapidOCR
    engine = _try_get_rapid_ocr()
    if engine is not None:
        try:
            result, _ = engine(image_path)
            if result:
                return "\n".join(item[1] for item in result if len(item) > 1)
        except Exception as e:
            logger.warning(f"RapidOCR lỗi: {e}")

    # Tầng 2a: Windows Native OCR
    if os.name == "nt":
        win_text = _windows_native_ocr(image_path)
        if win_text:
            return win_text

    # Tầng 2b: Tesseract (Linux/Mac/Windows)
    tess_text = _tesseract_ocr(image_path)
    if tess_text:
        return tess_text

    return "[Không thể trích xuất chữ từ ảnh — cài rapidocr-onnxruntime hoặc tesseract để kích hoạt OCR]"


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
            except Exception:
                pass
            _TEMP_FILES.discard(path)
