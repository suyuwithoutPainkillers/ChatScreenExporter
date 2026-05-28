# -*- coding: utf-8 -*-
"""
Screen chat exporter for Windows.

It captures a selected chat area, OCRs visible text, scrolls the chat, dedupes
overlapping pages, and writes a TXT transcript.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import datetime as dt
import difflib
import io
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Sequence


CONFIG_FILE = "config.json"
APP_VERSION = "V2.4.5"
SCREEN_EDGE_STOP_MARGIN = 2
APP_ICON_FILE = Path("assets") / "app_icon.ico"
SPEED_PRESETS = {
    "稳定": {"scroll_clicks": 5, "page_delay": 0.7, "ocr_scale": 1.6},
    "快速": {"scroll_clicks": 9, "page_delay": 0.25, "ocr_scale": 1.5},
    "极速": {"scroll_clicks": 14, "page_delay": 0.1, "ocr_scale": 1.35},
}


def runtime_base_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


def set_app_icon(root) -> None:
    try:
        icon_path = runtime_base_dir() / APP_ICON_FILE
        if icon_path.exists():
            root.iconbitmap(default=str(icon_path))
    except Exception:
        pass


def configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


@dataclass
class OCRLine:
    text: str
    x1: int
    y1: int
    x2: int
    y2: int
    confidence: float = 100.0

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2


@dataclass
class Message:
    time_text: str
    sender: str
    text: str
    y1: int = 0
    y2: int = 0


@dataclass
class PageCapture:
    page_number: int
    image_hash: str
    messages: list[Message]


@dataclass
class ScreenshotCapture:
    page_number: int
    image_hash: str
    image: object


@dataclass
class ExportConfig:
    region: list[int] | None = None  # x, y, width, height
    self_name: str = "我"
    other_name: str = "对方"
    backend: str = "auto"  # auto, tesseract, paddle
    recognition_mode: str = "ocr"  # ocr, ai
    tesseract_cmd: str = ""
    tessdata_dir: str = ""
    tesseract_lang: str = "chi_sim+eng"
    ai_model: str = "gpt-4.1-mini"
    ai_base_url: str = "https://api.openai.com/v1"
    ai_api_format: str = "responses"  # responses, chat_completions
    ai_image_detail: str = "high"
    ai_timeout: float = 90.0
    ai_pipeline: bool = True
    ai_concurrency: int = 3
    ai_batch_size: int = 3
    ai_capture_delay: float = 0.22
    ai_image_max_width: int = 1280
    ai_image_quality: int = 82
    max_page_errors: int = 20
    output_dir: str = "exports"
    direction: str = "up"  # up means start at newest and scroll toward older history
    max_pages: int = 400
    speed_preset: str = "快速"
    scroll_mode: str = "page"  # page, wheel
    page_overlap_clicks: int = 3
    scroll_clicks: int = 9
    page_delay: float = 0.25
    start_delay: float = 2.0
    stable_stop_pages: int = 4
    show_start_dialog: bool = True
    min_confidence: float = 35.0
    ocr_scale: float = 1.6
    merge_line_gap: int = 14
    dedupe_window: int = 80
    timestamp_center_min_ratio: float = 0.28
    timestamp_center_max_ratio: float = 0.72
    other_left_anchor_max_ratio: float = 0.32
    self_left_anchor_min_ratio: float = 0.38
    skip_placeholders: list[str] = field(
        default_factory=lambda: [
            "图片",
            "[图片]",
            "表情",
            "[表情]",
            "表情包",
            "[表情包]",
            "动态表情",
            "[动态表情]",
            "贴纸",
            "[贴纸]",
            "emoji",
            "[emoji]",
            "视频",
            "[视频]",
            "语音",
            "[语音]",
            "文件",
            "[文件]",
        ]
    )


class OCRBackend:
    name = "base"

    def read_lines(self, image) -> list[OCRLine]:
        raise NotImplementedError


class TesseractBackend(OCRBackend):
    name = "tesseract"

    def __init__(self, lang: str, scale: float, tesseract_cmd: str, tessdata_dir: str) -> None:
        try:
            import pytesseract  # type: ignore
            from pytesseract import Output  # type: ignore
            from PIL import ImageEnhance, ImageOps  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "缺少 pytesseract/Pillow。先运行: python -m pip install -r requirements.txt"
            ) from exc

        self.pytesseract = pytesseract
        self.Output = Output
        self.ImageEnhance = ImageEnhance
        self.ImageOps = ImageOps
        resolved_cmd = find_tesseract_cmd(tesseract_cmd)
        if resolved_cmd:
            self.pytesseract.pytesseract.tesseract_cmd = resolved_cmd
        self.tessdata_dir = resolve_tessdata_dir(tessdata_dir)
        if self.tessdata_dir:
            os.environ["TESSDATA_PREFIX"] = self.tessdata_dir
        self.lang = lang
        self.scale = scale
        try:
            self.pytesseract.get_tesseract_version()
        except Exception as exc:
            raise RuntimeError(
                "Tesseract OCR 程序不可用。请先安装 Tesseract，并确认命令行能运行 tesseract；中文还需要 chi_sim 语言包。"
            ) from exc

    @property
    def config(self) -> str:
        return "--psm 6"

    def _prepare(self, image):
        prepared = image.convert("RGB")
        if self.scale and self.scale != 1:
            w, h = prepared.size
            prepared = prepared.resize((int(w * self.scale), int(h * self.scale)))
        prepared = self.ImageOps.grayscale(prepared)
        prepared = self.ImageOps.autocontrast(prepared)
        prepared = self.ImageEnhance.Contrast(prepared).enhance(1.35)
        return prepared

    def read_lines(self, image) -> list[OCRLine]:
        prepared = self._prepare(image)
        data = self.pytesseract.image_to_data(
            prepared,
            lang=self.lang,
            output_type=self.Output.DICT,
            config=self.config,
        )
        grouped: dict[tuple[int, int, int, int], list[tuple[str, int, int, int, int, float]]] = {}
        count = len(data.get("text", []))
        scale = self.scale if self.scale else 1.0

        for idx in range(count):
            raw = (data["text"][idx] or "").strip()
            if not raw:
                continue
            try:
                conf = float(data["conf"][idx])
            except (TypeError, ValueError):
                conf = -1.0
            key = (
                int(data.get("block_num", [0])[idx]),
                int(data.get("par_num", [0])[idx]),
                int(data.get("line_num", [0])[idx]),
                int(data.get("word_num", [0])[idx] > 0),
            )
            x = int(data["left"][idx] / scale)
            y = int(data["top"][idx] / scale)
            w = int(data["width"][idx] / scale)
            h = int(data["height"][idx] / scale)
            grouped.setdefault(key, []).append((raw, x, y, x + w, y + h, conf))

        lines: list[OCRLine] = []
        for parts in grouped.values():
            if not parts:
                continue
            text = normalize_ocr_spacing(" ".join(part[0] for part in parts))
            x1 = min(part[1] for part in parts)
            y1 = min(part[2] for part in parts)
            x2 = max(part[3] for part in parts)
            y2 = max(part[4] for part in parts)
            confs = [part[5] for part in parts if part[5] >= 0]
            confidence = sum(confs) / len(confs) if confs else 100.0
            lines.append(OCRLine(text=text, x1=x1, y1=y1, x2=x2, y2=y2, confidence=confidence))

        return sorted(lines, key=lambda line: (line.y1, line.x1))


class PaddleBackend(OCRBackend):
    name = "paddle"

    def __init__(self) -> None:
        try:
            from paddleocr import PaddleOCR  # type: ignore
            import numpy as np  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "缺少 paddleocr/numpy。PaddleOCR 对中文更好，但通常建议用 Python 3.10/3.11 单独安装。"
            ) from exc
        self.np = np
        self.ocr = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)

    def read_lines(self, image) -> list[OCRLine]:
        result = self.ocr.ocr(self.np.array(image.convert("RGB")), cls=True)
        lines: list[OCRLine] = []
        if not result:
            return lines

        # PaddleOCR has had a few output shapes across versions. This keeps the
        # adapter permissive while preserving the fields we need.
        candidates = result[0] if isinstance(result, list) and result and isinstance(result[0], list) else result
        for item in candidates:
            try:
                box = item[0]
                text, conf = item[1][0], float(item[1][1]) * 100
            except Exception:
                continue
            xs = [int(point[0]) for point in box]
            ys = [int(point[1]) for point in box]
            clean = normalize_ocr_spacing(str(text).strip())
            if clean:
                lines.append(
                    OCRLine(
                        text=clean,
                        x1=min(xs),
                        y1=min(ys),
                        x2=max(xs),
                        y2=max(ys),
                        confidence=conf,
                    )
                )
        return sorted(lines, key=lambda line: (line.y1, line.x1))


def set_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def focus_window_at(x: int, y: int) -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        point = wintypes.POINT(int(x), int(y))
        user32.WindowFromPoint.argtypes = [wintypes.POINT]
        user32.WindowFromPoint.restype = wintypes.HWND
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL

        hwnd = user32.WindowFromPoint(point)
        if not hwnd:
            return False
        root_hwnd = user32.GetAncestor(hwnd, 2) or hwnd
        if user32.IsIconic(root_hwnd):
            user32.ShowWindow(root_hwnd, 9)
        return bool(user32.SetForegroundWindow(root_hwnd))
    except Exception:
        return False


def find_tesseract_cmd(configured: str = "") -> str:
    candidates = []
    if configured:
        candidates.append(configured)

    found = shutil.which("tesseract")
    if found:
        candidates.append(found)

    candidates.extend(
        [
            str(runtime_base_dir() / "Tesseract-OCR" / "tesseract.exe"),
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ]
    )

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(Path(candidate).resolve())
    return found or ""


def resolve_tessdata_dir(configured: str = "") -> str:
    candidates: list[Path] = []
    if configured:
        configured = os.path.expandvars(configured)
        configured_path = Path(configured)
        if configured_path.is_absolute():
            candidates.append(configured_path)
        else:
            candidates.append(Path.cwd() / configured_path)
            candidates.append(Path(__file__).resolve().parent / configured_path)

    candidates.extend(
        [
            runtime_base_dir() / "tessdata",
            runtime_base_dir() / "Tesseract-OCR" / "tessdata",
            Path(os.environ.get("LOCALAPPDATA", "")) / "TesseractOCR" / "tessdata",
            Path(r"C:\Program Files\Tesseract-OCR\tessdata"),
            Path(r"C:\Program Files (x86)\Tesseract-OCR\tessdata"),
        ]
    )

    for candidate in candidates:
        if candidate.exists() and any(candidate.glob("*.traineddata")):
            return str(candidate.resolve())
    return ""


def list_tesseract_languages(tesseract_cmd: str, tessdata_dir: str) -> list[str]:
    command = [tesseract_cmd or "tesseract"]
    if tessdata_dir:
        command.extend(["--tessdata-dir", tessdata_dir])
    command.append("--list-langs")
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = completed.stdout + completed.stderr
    languages: list[str] = []
    for line in output.splitlines():
        clean = line.strip()
        if not clean or clean.lower().startswith("list of available languages"):
            continue
        if re.fullmatch(r"[A-Za-z0-9_+-]+", clean):
            languages.append(clean)
    return languages


def normalize_ocr_spacing(text: str) -> str:
    text = text.replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text).strip()
    # Remove artificial spaces inserted between Chinese characters by OCR.
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    return text


def normalize_signature_text(text: str) -> str:
    text = normalize_ocr_spacing(text).lower()
    text = re.sub(r"\s+", "", text)
    return text


def is_timestamp(text: str) -> bool:
    clean = normalize_ocr_spacing(text)
    clean = clean.replace("：", ":")
    time_part = r"(?:上午|下午|晚上|凌晨|中午)?\s*\d{1,2}:\d{2}"
    patterns = [
        rf"^\d{{4}}[年/-]\d{{1,2}}[月/-]\d{{1,2}}日?\s*(?:周[一二三四五六日天]|星期[一二三四五六日天])?\s*{time_part}$",
        rf"^\d{{1,2}}[月/-]\d{{1,2}}日?\s*{time_part}$",
        rf"^(?:今天|昨天|前天)\s*{time_part}$",
        rf"^(?:周[一二三四五六日天]|星期[一二三四五六日天])\s*{time_part}$",
        rf"^{time_part}$",
    ]
    return any(re.match(pattern, clean) for pattern in patterns)


def is_placeholder(text: str, placeholders: Sequence[str]) -> bool:
    clean = normalize_ocr_spacing(text)
    compact = clean.replace(" ", "")
    if compact in placeholders:
        return True
    stripped = compact.strip("[]【】()（）")
    return stripped in {item.strip("[]【】()（）") for item in placeholders}


def sender_for_line(line: OCRLine, width: int, cfg: ExportConfig) -> str:
    if line.x1 <= width * cfg.other_left_anchor_max_ratio:
        return cfg.other_name
    if line.x1 >= width * cfg.self_left_anchor_min_ratio:
        return cfg.self_name
    return cfg.self_name if line.cx >= width / 2 else cfg.other_name


def extract_messages(lines: list[OCRLine], image_size: tuple[int, int], cfg: ExportConfig) -> list[Message]:
    width, _height = image_size
    current_time = ""
    messages: list[Message] = []

    for line in sorted(lines, key=lambda item: (item.y1, item.x1)):
        text = normalize_ocr_spacing(line.text)
        if not text or line.confidence < cfg.min_confidence:
            continue

        centered = width * cfg.timestamp_center_min_ratio <= line.cx <= width * cfg.timestamp_center_max_ratio
        if centered and is_timestamp(text):
            current_time = text.replace("：", ":")
            continue

        if is_placeholder(text, cfg.skip_placeholders):
            continue

        sender = sender_for_line(line, width, cfg)
        if messages:
            prev = messages[-1]
            gap = line.y1 - prev.y2
            same_sender = prev.sender == sender
            if same_sender and 0 <= gap <= cfg.merge_line_gap:
                prev.text = normalize_ocr_spacing(prev.text + "\n" + text)
                prev.y2 = max(prev.y2, line.y2)
                if not prev.time_text:
                    prev.time_text = current_time
                continue

        messages.append(
            Message(
                time_text=current_time,
                sender=sender,
                text=text,
                y1=line.y1,
                y2=line.y2,
            )
        )

    return messages


def get_ai_api_key() -> str:
    return os.environ.get("AI_API_KEY", "").strip() or os.environ.get("OPENAI_API_KEY", "").strip()


def api_url(base_url: str, endpoint: str) -> str:
    base = (base_url or "https://api.openai.com/v1").strip().rstrip("/")
    if base.endswith(endpoint):
        return base
    return f"{base}/{endpoint.lstrip('/')}"


def prepare_ai_image(image, cfg: ExportConfig):
    prepared = image.convert("RGB")
    max_width = int(getattr(cfg, "ai_image_max_width", 0) or 0)
    if max_width > 0 and prepared.size[0] > max_width:
        width, height = prepared.size
        new_height = max(1, int(height * (max_width / width)))
        try:
            from PIL import Image  # type: ignore

            resample = Image.Resampling.LANCZOS
        except Exception:
            resample = 1
        prepared = prepared.resize((max_width, new_height), resample=resample)
    return prepared


def image_to_data_url(image, cfg: ExportConfig) -> str:
    buffer = io.BytesIO()
    quality = min(95, max(55, int(getattr(cfg, "ai_image_quality", 82) or 82)))
    prepare_ai_image(image, cfg).save(buffer, format="JPEG", quality=quality, optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def get_response_text(response: dict) -> str:
    if isinstance(response.get("output_text"), str):
        return response["output_text"]

    parts: list[str] = []
    for item in response.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts).strip()


def parse_json_object(text: str) -> dict:
    clean = text.strip()
    clean = re.sub(r"^```(?:json)?\s*", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\s*```$", "", clean)
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        start = clean.find("{")
        end = clean.rfind("}")
        if start >= 0 and end > start:
            return json.loads(clean[start : end + 1])
        raise


def ai_prompt(cfg: ExportConfig) -> str:
    return f"""
你是微信聊天截图转录器。图片是用户框选的聊天消息列表区域。

请只识别屏幕上可见的文字聊天消息，忽略输入框、按钮和窗口标题。
如果某条消息是照片/图片气泡、表情包、动态表情、贴纸、emoji大图、语音、视频或文件，只跳过这一条非文字消息；同一屏里的其他文字消息仍然必须正常识别。
照片/图片/表情包里即使有文字，也不要识别里面的文字，不要写“图片”“表情”“表情包”占位。

发送者判断规则：
- 右侧气泡/右侧头像对应：{cfg.self_name}
- 左侧气泡/左侧头像对应：{cfg.other_name}

微信时间规则：
- 时间通常是居中显示在聊天列表中间的灰色/浅色时间条，并不是每条消息都有。
- 一条消息上方如果没有新的居中时间条，就沿用最近一次已经出现的居中时间条。
- 如果当前截图顶部的消息上方没有可见时间条，且本截图之前还没有出现过时间条，则 time 留空字符串，不要猜测。
- 不要把聊天内容里的数字、输入框文字或窗口标题当作时间。

请按图片从上到下的顺序返回 JSON，格式必须完全是：
{{"messages":[{{"time":"", "sender":"{cfg.self_name}", "text":"消息文字"}}]}}

要求：
- text 保留原文，不要翻译，不要补全。
- 如果同一个人连续发了多条完全相同的文字，也必须逐条输出，不要合并，不要去重。
- 看不清的文字不要瞎猜；实在看不清可以跳过该消息。
- 如果这一屏只有照片、图片、表情包、贴纸、语音、视频或文件，没有可见文字聊天消息，才返回 {{"messages":[]}}。
- 只输出 JSON，不要输出解释。
""".strip()


def ai_batch_prompt(cfg: ExportConfig, page_numbers: Sequence[int]) -> str:
    page_list = ", ".join(str(number) for number in page_numbers)
    return f"""
你是微信聊天截图转录器。你会一次收到多张聊天区域截图，每张图前面都有“第 N 屏”的文字标签。
本次需要识别的屏号是：{page_list}。

请逐屏独立识别，只识别屏幕上可见的文字聊天消息，忽略输入框、按钮、窗口标题和侧边栏。
如果某条消息是照片、图片气泡、表情包、动态表情、贴纸、emoji大图、语音、视频或文件，只跳过这一条非文字消息；同一屏里的其他文字消息必须继续识别。
照片/图片/表情包里即使有文字，也不要识别里面的文字，不要写“图片”“表情”“表情包”占位。

发送者判断规则：
- 右侧气泡/右侧头像对应：{cfg.self_name}
- 左侧气泡/左侧头像对应：{cfg.other_name}

微信时间规则：
- 时间通常是居中显示在聊天列表中间的灰色/浅色时间条，并不是每条消息都有。
- 某条消息上方如果没有新的居中时间条，就沿用该屏内最近一次已经出现的居中时间条。
- 如果当前截图顶部的消息上方没有可见时间条，且该屏之前还没有出现过时间条，则 time 留空字符串，不要猜测。
- 不要把聊天内容里的数字、输入框文字或窗口标题当作时间。

请按照每张图片从上到下的顺序返回 JSON，格式必须完全是：
{{"pages":[{{"page":1,"messages":[{{"time":"", "sender":"{cfg.self_name}", "text":"消息文字"}}]}}]}}

要求：
- page 必须使用对应图片的屏号。
- text 保留原文，不要翻译，不要补全。
- 如果同一个人连续发了多条完全相同的文字，也必须逐条输出，不要合并，不要去重。
- 看不清的文字不要瞎猜；实在看不清可以跳过该消息。
- 如果某一屏只有照片、图片、表情包、贴纸、语音、视频或文件，没有可见文字聊天消息，该屏返回空 messages。
- 只输出 JSON，不要输出解释。
""".strip()


def post_json(url: str, payload: dict, api_key: str, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"AI API 请求失败：HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"AI API 网络请求失败：{exc}") from exc
    return json.loads(raw)


def ai_max_tokens_for(count: int) -> int:
    return max(2500, min(12000, 2500 * max(1, count)))


def build_ai_images_content(images: Sequence[tuple[int, object]], cfg: ExportConfig, responses_format: bool) -> list[dict]:
    content: list[dict] = []
    multi_image = len(images) > 1
    for page_number, image in images:
        if multi_image:
            label_type = "input_text" if responses_format else "text"
            content.append({"type": label_type, "text": f"第 {page_number} 屏："})
        data_url = image_to_data_url(image, cfg)
        if responses_format:
            content.append({"type": "input_image", "image_url": data_url, "detail": cfg.ai_image_detail})
        else:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": data_url, "detail": cfg.ai_image_detail},
                }
            )
    return content


def call_ai_model_images(images: Sequence[tuple[int, object]], cfg: ExportConfig, prompt: str, api_key: str) -> str:
    if not images:
        return ""
    if cfg.ai_api_format == "responses":
        content = [{"type": "input_text", "text": prompt}]
        content.extend(build_ai_images_content(images, cfg, responses_format=True))
        payload = {
            "model": cfg.ai_model,
            "input": [
                {
                    "role": "user",
                    "content": content,
                }
            ],
            "temperature": 0,
            "max_output_tokens": ai_max_tokens_for(len(images)),
        }
        body = post_json(api_url(cfg.ai_base_url, "responses"), payload, api_key, cfg.ai_timeout)
        return get_response_text(body)

    if cfg.ai_api_format == "chat_completions":
        content = [{"type": "text", "text": prompt}]
        content.extend(build_ai_images_content(images, cfg, responses_format=False))
        payload = {
            "model": cfg.ai_model,
            "messages": [
                {
                    "role": "user",
                    "content": content,
                }
            ],
            "temperature": 0,
            "max_tokens": ai_max_tokens_for(len(images)),
        }
        body = post_json(api_url(cfg.ai_base_url, "chat/completions"), payload, api_key, cfg.ai_timeout)
        choices = body.get("choices", [])
        if choices:
            message = choices[0].get("message", {})
            content = message.get("content", "")
            if isinstance(content, list):
                return "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
            return str(content)
        return ""

    raise RuntimeError("--ai-api-format 只能是 responses 或 chat_completions。")


def call_ai_model(image, cfg: ExportConfig, prompt: str, api_key: str) -> str:
    return call_ai_model_images([(1, image)], cfg, prompt, api_key)


def is_fatal_recognition_error(exc: Exception) -> bool:
    text = str(exc).lower()
    fatal_markers = [
        "model_not_found",
        "does not exist",
        "invalid_api_key",
        "unauthorized",
        "permission",
        "no api key",
        "401",
        "403",
        "404",
        "需要 api key",
        "需要 openai_api_key",
    ]
    return any(marker in text for marker in fatal_markers)


def is_timeout_recognition_error(exc: Exception) -> bool:
    text = str(exc).lower()
    timeout_markers = [
        "timed out",
        "timeout",
        "read operation timed out",
        "the read operation timed out",
    ]
    return any(marker in text for marker in timeout_markers)


def empty_pages_for(captures: Sequence[ScreenshotCapture]) -> list[PageCapture]:
    return [
        PageCapture(page_number=capture.page_number, image_hash=capture.image_hash, messages=[])
        for capture in captures
    ]


def parse_ai_message_items(items: object, cfg: ExportConfig) -> list[Message]:
    if not isinstance(items, list):
        return []

    messages: list[Message] = []
    current_time = ""
    for item in items:
        if not isinstance(item, dict):
            continue
        text = normalize_ocr_spacing(str(item.get("text", "")).strip())
        if not text or is_placeholder(text, cfg.skip_placeholders):
            continue
        time_text = normalize_ocr_spacing(str(item.get("time", "")).strip())
        sender = normalize_ocr_spacing(str(item.get("sender", "")).strip())
        if sender not in (cfg.self_name, cfg.other_name):
            sender = cfg.self_name if sender.lower() in ("me", "self", "right", "我") else cfg.other_name
        if time_text:
            current_time = time_text
        messages.append(Message(time_text=time_text or current_time, sender=sender, text=text))
    return messages


def parse_ai_page_number(value: object) -> int | None:
    if isinstance(value, int):
        return value
    match = re.search(r"\d+", str(value))
    return int(match.group(0)) if match else None


def extract_messages_with_ai(image, cfg: ExportConfig) -> list[Message]:
    api_key = get_ai_api_key()
    if not api_key:
        raise RuntimeError("AI识别模式需要 API Key。请在启动界面填写 Key，或先设置环境变量 AI_API_KEY / OPENAI_API_KEY。")

    data = parse_json_object(call_ai_model(image, cfg, ai_prompt(cfg), api_key))
    return parse_ai_message_items(data.get("messages", []), cfg)


def extract_pages_with_ai_batch(captures: Sequence[ScreenshotCapture], cfg: ExportConfig) -> list[PageCapture]:
    api_key = get_ai_api_key()
    if not api_key:
        raise RuntimeError("AI识别模式需要 API Key。请在启动界面填写 Key，或先设置环境变量 AI_API_KEY / OPENAI_API_KEY。")

    prompt = ai_batch_prompt(cfg, [capture.page_number for capture in captures])
    data = parse_json_object(
        call_ai_model_images(
            [(capture.page_number, capture.image) for capture in captures],
            cfg,
            prompt,
            api_key,
        )
    )
    messages_by_page: dict[int, list[Message]] = {}
    pages = data.get("pages")
    if isinstance(pages, list):
        for index, page_data in enumerate(pages):
            if not isinstance(page_data, dict):
                continue
            page_number = parse_ai_page_number(
                page_data.get("page", page_data.get("page_number", page_data.get("screen", "")))
            )
            if page_number is None:
                page_number = captures[index].page_number if index < len(captures) else None
            if page_number is None:
                continue
            messages_by_page[page_number] = parse_ai_message_items(page_data.get("messages", []), cfg)
    elif isinstance(pages, dict):
        for page_key, page_value in pages.items():
            page_number = parse_ai_page_number(page_key)
            if page_number is None:
                continue
            if isinstance(page_value, dict):
                page_value = page_value.get("messages", [])
            messages_by_page[page_number] = parse_ai_message_items(page_value, cfg)
    elif len(captures) == 1:
        messages_by_page[captures[0].page_number] = parse_ai_message_items(data.get("messages", []), cfg)

    if len(captures) > 1 and not messages_by_page:
        raise RuntimeError("批量AI响应缺少 pages 字段，自动改用单屏重试")

    return [
        PageCapture(
            page_number=capture.page_number,
            image_hash=capture.image_hash,
            messages=messages_by_page.get(capture.page_number, []),
        )
        for capture in captures
    ]


def fill_missing_times(messages: list[Message]) -> list[Message]:
    last_time = ""
    for message in messages:
        if message.time_text:
            last_time = message.time_text
        elif last_time:
            message.time_text = last_time
    return messages


def message_signature(message: Message) -> str:
    return f"{message.time_text}|{message.sender}|{normalize_signature_text(message.text)}"


def is_duplicate_recent(signature: str, recent: Iterable[str]) -> bool:
    if signature in recent:
        return True
    sig_text = signature.split("|", 2)[-1]
    sig_prefix = signature.rsplit("|", 1)[0]
    for existing in recent:
        if existing.rsplit("|", 1)[0] != sig_prefix:
            continue
        existing_text = existing.split("|", 2)[-1]
        if sig_text and difflib.SequenceMatcher(None, sig_text, existing_text).ratio() >= 0.96:
            return True
    return False


def flatten_pages(pages: list[PageCapture], direction: str, dedupe_window: int) -> list[Message]:
    ordered_pages = list(reversed(pages)) if direction == "up" else pages
    previous_page_boundary: deque[str] = deque(maxlen=max(10, dedupe_window))
    messages: list[Message] = []
    for page in ordered_pages:
        accepted_from_page: list[Message] = []
        for message in page.messages:
            signature = message_signature(message)
            if is_duplicate_recent(signature, previous_page_boundary):
                continue
            accepted_from_page.append(message)
        messages.extend(accepted_from_page)
        for message in page.messages:
            previous_page_boundary.append(message_signature(message))
    return fill_missing_times(messages)


def image_hash(image) -> str:
    small = image.convert("L").resize((64, 64))
    return hashlib.sha1(small.tobytes()).hexdigest()


def load_config(path: Path) -> ExportConfig:
    cfg = ExportConfig()
    if not path.exists():
        return cfg
    data = json.loads(path.read_text(encoding="utf-8"))
    valid_keys = set(asdict(cfg).keys())
    for key, value in data.items():
        if key in valid_keys:
            setattr(cfg, key, value)
    return cfg


def save_config(path: Path, cfg: ExportConfig) -> None:
    path.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8")


def select_region() -> list[int]:
    import tkinter as tk

    set_dpi_awareness()
    root = tk.Tk()
    set_app_icon(root)
    root.attributes("-fullscreen", True)
    root.attributes("-alpha", 0.28)
    root.attributes("-topmost", True)
    root.configure(bg="black")
    root.title(f"选择聊天记录区域 {APP_VERSION}")

    canvas = tk.Canvas(root, cursor="cross", bg="black", highlightthickness=0)
    canvas.pack(fill=tk.BOTH, expand=True)
    instruction = canvas.create_text(
        30,
        30,
        anchor="nw",
        fill="white",
        font=("Microsoft YaHei", 18),
        text="拖拽框选聊天消息区域，只选消息列表，不要包含输入框或左侧会话列表。按 Esc 取消。",
    )
    state: dict[str, int | None] = {"x0": None, "y0": None, "rect": None}
    result: dict[str, list[int] | None] = {"region": None}

    def on_press(event):
        state["x0"] = event.x
        state["y0"] = event.y
        if state["rect"]:
            canvas.delete(state["rect"])
        state["rect"] = canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#34d399", width=3)

    def on_drag(event):
        if state["rect"] and state["x0"] is not None and state["y0"] is not None:
            canvas.coords(state["rect"], state["x0"], state["y0"], event.x, event.y)

    def on_release(event):
        if state["x0"] is None or state["y0"] is None:
            return
        x1, y1 = int(state["x0"]), int(state["y0"])
        x2, y2 = int(event.x), int(event.y)
        x, y = min(x1, x2), min(y1, y2)
        w, h = abs(x2 - x1), abs(y2 - y1)
        if w >= 100 and h >= 100:
            result["region"] = [x, y, w, h]
            root.quit()

    def on_escape(_event):
        root.quit()

    canvas.tag_raise(instruction)
    root.bind("<Escape>", on_escape)
    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    root.mainloop()
    root.destroy()

    if not result["region"]:
        raise SystemExit("已取消区域选择。")
    return result["region"]


def ask_start_options(cfg: ExportConfig) -> bool:
    import tkinter as tk
    from tkinter import filedialog, messagebox

    def region_text() -> str:
        if not cfg.region:
            return "尚未选择"
        x, y, w, h = cfg.region
        return f"x={x}, y={y}, 宽={w}, 高={h}"

    def output_text() -> str:
        return str(Path(cfg.output_dir).resolve())

    set_dpi_awareness()
    root = tk.Tk()
    set_app_icon(root)
    root.title(f"聊天记录导出 {APP_VERSION}")
    root.attributes("-topmost", True)
    root.resizable(False, False)

    frame = tk.Frame(root, padx=22, pady=18)
    frame.pack(fill=tk.BOTH, expand=True)

    title = tk.Label(frame, text="导出前确认设置", font=("Microsoft YaHei", 14, "bold"))
    title.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))

    tk.Label(frame, text="我的名字是：", font=("Microsoft YaHei", 10)).grid(row=1, column=0, sticky="e", padx=(0, 8), pady=6)
    self_var = tk.StringVar(value=cfg.self_name)
    self_entry = tk.Entry(frame, textvariable=self_var, width=30, font=("Microsoft YaHei", 10))
    self_entry.grid(row=1, column=1, columnspan=2, sticky="w", pady=6)

    tk.Label(frame, text="对方名字是：", font=("Microsoft YaHei", 10)).grid(row=2, column=0, sticky="e", padx=(0, 8), pady=6)
    other_var = tk.StringVar(value=cfg.other_name)
    other_entry = tk.Entry(frame, textvariable=other_var, width=30, font=("Microsoft YaHei", 10))
    other_entry.grid(row=2, column=1, columnspan=2, sticky="w", pady=6)

    hint = tk.Label(
        frame,
        text="右侧头像默认识别为“我的名字”，左侧头像默认识别为“对方名字”。",
        fg="#555555",
        font=("Microsoft YaHei", 9),
    )
    hint.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 12))

    tk.Label(frame, text="读取范围：", font=("Microsoft YaHei", 10)).grid(row=4, column=0, sticky="e", padx=(0, 8), pady=6)
    region_var = tk.StringVar(value=region_text())
    tk.Label(frame, textvariable=region_var, fg="#333333", font=("Microsoft YaHei", 10)).grid(row=4, column=1, sticky="w", pady=6)

    tk.Label(frame, text="保存到：", font=("Microsoft YaHei", 10)).grid(row=5, column=0, sticky="e", padx=(0, 8), pady=6)
    output_var = tk.StringVar(value=output_text())
    tk.Label(frame, textvariable=output_var, fg="#333333", font=("Microsoft YaHei", 10), wraplength=360, justify="left").grid(row=5, column=1, sticky="w", pady=6)

    tk.Label(frame, text="速度：", font=("Microsoft YaHei", 10)).grid(row=6, column=0, sticky="e", padx=(0, 8), pady=6)
    preset_var = tk.StringVar(value=cfg.speed_preset if cfg.speed_preset in SPEED_PRESETS else "快速")
    tk.OptionMenu(frame, preset_var, *SPEED_PRESETS.keys()).grid(row=6, column=1, sticky="w", pady=6)

    pipeline_var = tk.BooleanVar(value=cfg.ai_pipeline)
    tk.Checkbutton(frame, text="AI流水线加速", variable=pipeline_var, font=("Microsoft YaHei", 10)).grid(row=7, column=1, sticky="w", pady=4)

    tk.Label(frame, text="AI并发数：", font=("Microsoft YaHei", 10)).grid(row=8, column=0, sticky="e", padx=(0, 8), pady=6)
    concurrency_var = tk.StringVar(value=str(cfg.ai_concurrency))
    tk.Spinbox(frame, from_=1, to=8, increment=1, textvariable=concurrency_var, width=8, font=("Microsoft YaHei", 10)).grid(row=8, column=1, sticky="w", pady=6)

    tk.Label(frame, text="AI合并屏数：", font=("Microsoft YaHei", 10)).grid(row=9, column=0, sticky="e", padx=(0, 8), pady=6)
    batch_var = tk.StringVar(value=str(cfg.ai_batch_size))
    tk.Spinbox(frame, from_=1, to=6, increment=1, textvariable=batch_var, width=8, font=("Microsoft YaHei", 10)).grid(row=9, column=1, sticky="w", pady=6)

    tk.Label(frame, text="AI上传宽度：", font=("Microsoft YaHei", 10)).grid(row=10, column=0, sticky="e", padx=(0, 8), pady=6)
    image_width_var = tk.StringVar(value=str(cfg.ai_image_max_width))
    tk.Spinbox(frame, from_=600, to=2400, increment=80, textvariable=image_width_var, width=8, font=("Microsoft YaHei", 10)).grid(row=10, column=1, sticky="w", pady=6)

    tk.Label(frame, text="滚动方式：", font=("Microsoft YaHei", 10)).grid(row=11, column=0, sticky="e", padx=(0, 8), pady=6)
    scroll_labels = {"page": "整屏翻页", "wheel": "滚轮滚动"}
    scroll_values = {value: key for key, value in scroll_labels.items()}
    scroll_var = tk.StringVar(value=scroll_labels.get(cfg.scroll_mode, "整屏翻页"))
    tk.OptionMenu(frame, scroll_var, *scroll_values.keys()).grid(row=11, column=1, sticky="w", pady=6)

    tk.Label(frame, text="防漏重叠：", font=("Microsoft YaHei", 10)).grid(row=12, column=0, sticky="e", padx=(0, 8), pady=6)
    overlap_var = tk.StringVar(value=str(cfg.page_overlap_clicks))
    tk.Spinbox(frame, from_=0, to=10, increment=1, textvariable=overlap_var, width=8, font=("Microsoft YaHei", 10)).grid(row=12, column=1, sticky="w", pady=6)

    tk.Label(frame, text="识别模式：", font=("Microsoft YaHei", 10)).grid(row=13, column=0, sticky="e", padx=(0, 8), pady=6)
    mode_labels = {"ocr": "本机OCR", "ai": "AI识别(GPT)"}
    mode_values = {value: key for key, value in mode_labels.items()}
    mode_var = tk.StringVar(value=mode_labels.get(cfg.recognition_mode, "本机OCR"))
    tk.OptionMenu(frame, mode_var, *mode_values.keys()).grid(row=13, column=1, sticky="w", pady=6)

    tk.Label(frame, text="接口格式：", font=("Microsoft YaHei", 10)).grid(row=14, column=0, sticky="e", padx=(0, 8), pady=6)
    format_labels = {"responses": "OpenAI Responses", "chat_completions": "兼容Chat Completions"}
    format_values = {value: key for key, value in format_labels.items()}
    format_var = tk.StringVar(value=format_labels.get(cfg.ai_api_format, "OpenAI Responses"))
    tk.OptionMenu(frame, format_var, *format_values.keys()).grid(row=14, column=1, sticky="w", pady=6)

    tk.Label(frame, text="API地址：", font=("Microsoft YaHei", 10)).grid(row=15, column=0, sticky="e", padx=(0, 8), pady=6)
    base_url_var = tk.StringVar(value=cfg.ai_base_url)
    tk.Entry(frame, textvariable=base_url_var, width=42, font=("Microsoft YaHei", 10)).grid(row=15, column=1, columnspan=2, sticky="w", pady=6)

    tk.Label(frame, text="模型名：", font=("Microsoft YaHei", 10)).grid(row=16, column=0, sticky="e", padx=(0, 8), pady=6)
    model_var = tk.StringVar(value=cfg.ai_model)
    tk.Entry(frame, textvariable=model_var, width=30, font=("Microsoft YaHei", 10)).grid(row=16, column=1, columnspan=2, sticky="w", pady=6)

    tk.Label(frame, text="API Key：", font=("Microsoft YaHei", 10)).grid(row=17, column=0, sticky="e", padx=(0, 8), pady=6)
    api_key_var = tk.StringVar(value="")
    tk.Entry(frame, textvariable=api_key_var, width=30, show="*", font=("Microsoft YaHei", 10)).grid(row=17, column=1, columnspan=2, sticky="w", pady=6)
    tk.Label(frame, text="仅 AI识别 需要；留空则使用环境变量 AI_API_KEY / OPENAI_API_KEY。", fg="#555555", font=("Microsoft YaHei", 8)).grid(row=18, column=1, columnspan=2, sticky="w")

    tk.Label(frame, text="最多扫描屏数：", font=("Microsoft YaHei", 10)).grid(row=19, column=0, sticky="e", padx=(0, 8), pady=6)
    max_pages_var = tk.StringVar(value=str(cfg.max_pages))
    tk.Spinbox(frame, from_=1, to=20000, increment=50, textvariable=max_pages_var, width=10, font=("Microsoft YaHei", 10)).grid(row=19, column=1, sticky="w", pady=6)

    result = {"start": False}

    def choose_region() -> None:
        root.withdraw()
        root.update_idletasks()
        time.sleep(0.15)
        try:
            cfg.region = select_region()
            region_var.set(region_text())
        except SystemExit:
            pass
        finally:
            root.deiconify()
            root.attributes("-topmost", True)
            root.lift()
            self_entry.focus_set()

    def choose_output_dir() -> None:
        selected = filedialog.askdirectory(parent=root, title="选择 TXT 保存文件夹", initialdir=output_text())
        if selected:
            cfg.output_dir = selected
            output_var.set(output_text())

    def begin() -> None:
        self_name = self_var.get().strip()
        other_name = other_var.get().strip()
        if not self_name or not other_name:
            messagebox.showwarning("需要填写名字", "请把两个人的名字都填上。", parent=root)
            return
        if not cfg.region:
            messagebox.showinfo("选择聊天范围", "请先框选聊天消息区域。只选消息列表，不要包含底部输入框。", parent=root)
            choose_region()
            if not cfg.region:
                return
        try:
            cfg.max_pages = max(1, int(max_pages_var.get().strip()))
        except ValueError:
            messagebox.showwarning("屏数无效", "最多扫描屏数需要填写数字。", parent=root)
            return
        try:
            cfg.ai_concurrency = min(8, max(1, int(concurrency_var.get().strip())))
        except ValueError:
            messagebox.showwarning("并发数无效", "AI并发数需要填写数字。", parent=root)
            return
        try:
            cfg.ai_batch_size = min(6, max(1, int(batch_var.get().strip())))
        except ValueError:
            messagebox.showwarning("合并屏数无效", "AI合并屏数需要填写数字。", parent=root)
            return
        try:
            cfg.ai_image_max_width = min(2400, max(600, int(image_width_var.get().strip())))
        except ValueError:
            messagebox.showwarning("上传宽度无效", "AI上传宽度需要填写数字。", parent=root)
            return
        try:
            cfg.page_overlap_clicks = min(10, max(0, int(overlap_var.get().strip())))
        except ValueError:
            messagebox.showwarning("重叠值无效", "防漏重叠需要填写数字。", parent=root)
            return
        cfg.self_name = self_name
        cfg.other_name = other_name
        cfg.recognition_mode = mode_values.get(mode_var.get(), "ocr")
        cfg.ai_pipeline = bool(pipeline_var.get())
        cfg.scroll_mode = scroll_values.get(scroll_var.get(), "page")
        cfg.ai_api_format = format_values.get(format_var.get(), "responses")
        cfg.ai_base_url = base_url_var.get().strip() or "https://api.openai.com/v1"
        cfg.ai_model = model_var.get().strip() or "gpt-4.1-mini"
        if cfg.recognition_mode == "ai" and api_key_var.get().strip():
            os.environ["AI_API_KEY"] = api_key_var.get().strip()
            os.environ["OPENAI_API_KEY"] = api_key_var.get().strip()
        cfg.speed_preset = preset_var.get()
        preset = SPEED_PRESETS.get(cfg.speed_preset, SPEED_PRESETS["快速"])
        cfg.scroll_clicks = int(preset["scroll_clicks"])
        cfg.page_delay = float(preset["page_delay"])
        cfg.ocr_scale = float(preset["ocr_scale"])
        result["start"] = True
        root.destroy()

    def cancel() -> None:
        root.destroy()

    button_frame = tk.Frame(frame)
    button_frame.grid(row=20, column=0, columnspan=3, sticky="e", pady=(12, 0))
    tk.Button(button_frame, text="框选聊天范围", width=14, command=choose_region).pack(side=tk.LEFT, padx=(0, 8))
    tk.Button(button_frame, text="选择保存位置", width=14, command=choose_output_dir).pack(side=tk.LEFT, padx=(0, 8))
    tk.Button(button_frame, text="取消", width=10, command=cancel).pack(side=tk.LEFT, padx=(0, 8))
    tk.Button(button_frame, text="开始导出", width=12, command=begin).pack(side=tk.LEFT)

    root.bind("<Return>", lambda _event: begin())
    root.bind("<Escape>", lambda _event: cancel())
    self_entry.focus_set()
    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_width()) // 2
    y = (root.winfo_screenheight() - root.winfo_height()) // 2
    root.geometry(f"+{x}+{y}")
    root.mainloop()
    return result["start"]


def make_backend(cfg: ExportConfig) -> OCRBackend:
    errors: list[str] = []
    if cfg.backend in ("auto", "paddle"):
        try:
            return PaddleBackend()
        except RuntimeError as exc:
            errors.append(str(exc))
            if cfg.backend == "paddle":
                raise
    if cfg.backend in ("auto", "tesseract"):
        try:
            return TesseractBackend(cfg.tesseract_lang, cfg.ocr_scale, cfg.tesseract_cmd, cfg.tessdata_dir)
        except RuntimeError as exc:
            errors.append(str(exc))
            if cfg.backend == "tesseract":
                raise
    raise RuntimeError("没有可用 OCR 后端。\n" + "\n".join(errors))


def apply_cli_overrides(cfg: ExportConfig, args: argparse.Namespace) -> ExportConfig:
    for key in [
        "self_name",
        "other_name",
        "backend",
        "recognition_mode",
        "tesseract_cmd",
        "tessdata_dir",
        "tesseract_lang",
        "ai_model",
        "ai_base_url",
        "ai_api_format",
        "ai_image_detail",
        "ai_timeout",
        "ai_pipeline",
        "ai_concurrency",
        "ai_batch_size",
        "ai_capture_delay",
        "ai_image_max_width",
        "ai_image_quality",
        "max_page_errors",
        "output_dir",
        "direction",
        "max_pages",
        "speed_preset",
        "scroll_mode",
        "page_overlap_clicks",
        "scroll_clicks",
        "page_delay",
        "start_delay",
        "stable_stop_pages",
        "show_start_dialog",
        "min_confidence",
        "ocr_scale",
    ]:
        if hasattr(args, key):
            value = getattr(args, key)
            if value is not None:
                setattr(cfg, key, value)
    return cfg


def write_txt(messages: list[Message], cfg: ExportConfig, output_path: Path, page_count: int) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "聊天记录屏幕 OCR 导出",
        f"导出时间：{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"扫描页数：{page_count}",
        f"识别模式：{cfg.recognition_mode}",
        f"区域：{cfg.region}",
        "说明：时间来自聊天窗口中实际显示的时间标记；没有单独显示时间的消息会沿用最近一次时间标记。",
        "",
    ]
    for message in messages:
        time_text = message.time_text or "未知时间"
        text = message.text.replace("\r\n", "\n").replace("\r", "\n")
        text = text.replace("\n", "\\n")
        lines.append(f"[{time_text}] {message.sender}：{text}")
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class ScanControlWindow:
    def __init__(self, max_pages: int) -> None:
        import tkinter as tk

        self.tk = tk
        self.root = tk.Tk()
        set_app_icon(self.root)
        self.root.title(f"正在导出聊天记录 {APP_VERSION}")
        self.root.attributes("-topmost", True)
        self.root.resizable(False, False)
        self.paused = False
        self.stop_requested = False

        frame = tk.Frame(self.root, padx=18, pady=14)
        frame.pack(fill=tk.BOTH, expand=True)

        self.status_var = tk.StringVar(value="准备开始...")
        self.detail_var = tk.StringVar(value=f"最多扫描 {max_pages} 屏")
        tk.Label(frame, text="导出进行中", font=("Microsoft YaHei", 13, "bold")).pack(anchor="w", pady=(0, 8))
        tk.Label(frame, textvariable=self.status_var, font=("Microsoft YaHei", 10)).pack(anchor="w")
        tk.Label(frame, textvariable=self.detail_var, fg="#555555", font=("Microsoft YaHei", 9)).pack(anchor="w", pady=(4, 12))

        button_frame = tk.Frame(frame)
        button_frame.pack(anchor="e")
        self.pause_button = tk.Button(button_frame, text="暂停", width=10, command=self.toggle_pause)
        self.pause_button.pack(side=tk.LEFT, padx=(0, 8))
        tk.Button(button_frame, text="停止并保存", width=12, command=self.request_stop).pack(side=tk.LEFT)

        self.root.protocol("WM_DELETE_WINDOW", self.request_stop)
        self._center()
        self.safe_update()

    def _center(self) -> None:
        self.root.update_idletasks()
        x = self.root.winfo_screenwidth() - self.root.winfo_width() - 24
        y = 80
        self.root.geometry(f"+{max(0, x)}+{y}")

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self.pause_button.configure(text="继续" if self.paused else "暂停")
        if self.paused:
            self.status_var.set("已暂停，点击“继续”后恢复。")

    def request_stop(self) -> None:
        self.stop_requested = True
        self.paused = False
        self.pause_button.configure(state="disabled")
        self.status_var.set("正在停止并保存已识别内容...")
        self.safe_update()

    def set_status(self, page_number: int, max_pages: int, page_messages: int, pages_done: int, visible_messages: int) -> None:
        self.status_var.set(f"第 {page_number}/{max_pages} 屏，当前识别 {page_messages} 条")
        self.detail_var.set(f"已扫描 {pages_done} 屏，去重前约 {visible_messages} 条文本消息")
        self.safe_update()

    def set_text(self, status: str, detail: str = "") -> None:
        self.status_var.set(status)
        if detail:
            self.detail_var.set(detail)
        self.safe_update()

    def safe_update(self) -> None:
        try:
            self.root.update_idletasks()
            self.root.update()
        except self.tk.TclError:
            self.stop_requested = True

    def wait_if_paused(self) -> None:
        while self.paused and not self.stop_requested:
            self.safe_update()
            time.sleep(0.12)

    def sleep(self, seconds: float) -> None:
        end_at = time.time() + max(0.0, seconds)
        while time.time() < end_at and not self.stop_requested:
            self.wait_if_paused()
            self.safe_update()
            time.sleep(0.05)

    def close(self) -> None:
        try:
            self.root.destroy()
        except self.tk.TclError:
            pass


def show_result_dialog(output_path: Path, message_count: int, page_count: int, stopped: bool, reason: str = "") -> None:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    set_app_icon(root)
    root.title(f"导出结果 {APP_VERSION}")
    root.attributes("-topmost", True)
    root.resizable(False, False)

    frame = tk.Frame(root, padx=22, pady=18)
    frame.pack(fill=tk.BOTH, expand=True)

    title = "已停止并保存" if stopped else "导出完成"
    tk.Label(frame, text=title, font=("Microsoft YaHei", 14, "bold")).pack(anchor="w", pady=(0, 10))
    tk.Label(frame, text=f"TXT 文件已保存到：\n{output_path.resolve()}", justify="left", wraplength=520, font=("Microsoft YaHei", 10)).pack(anchor="w")
    tk.Label(frame, text=f"扫描屏数：{page_count}    导出消息：{message_count}", fg="#555555", font=("Microsoft YaHei", 9)).pack(anchor="w", pady=(10, 0))
    if reason:
        tk.Label(frame, text=f"停止原因：{reason}", fg="#555555", wraplength=520, justify="left", font=("Microsoft YaHei", 9)).pack(anchor="w", pady=(4, 0))

    def open_file() -> None:
        try:
            os.startfile(str(output_path.resolve()))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("无法打开文件", str(exc), parent=root)

    def open_folder() -> None:
        try:
            os.startfile(str(output_path.resolve().parent))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("无法打开文件夹", str(exc), parent=root)

    button_frame = tk.Frame(frame)
    button_frame.pack(anchor="e", pady=(16, 0))
    tk.Button(button_frame, text="打开TXT", width=10, command=open_file).pack(side=tk.LEFT, padx=(0, 8))
    tk.Button(button_frame, text="打开文件夹", width=12, command=open_folder).pack(side=tk.LEFT, padx=(0, 8))
    tk.Button(button_frame, text="关闭", width=10, command=root.destroy).pack(side=tk.LEFT)

    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_width()) // 2
    y = (root.winfo_screenheight() - root.winfo_height()) // 2
    root.geometry(f"+{x}+{y}")
    root.mainloop()


UI_BG = "#f3f4f6"
UI_SURFACE = "#ffffff"
UI_PANEL = "#f8f9fb"
UI_MUTED = "#6b7280"
UI_TEXT = "#111827"
UI_BORDER = "#bdc8ce"
UI_LINE = "#e5e7eb"
UI_PRIMARY = "#00647c"
UI_PRIMARY_DARK = "#00556a"
UI_DANGER = "#ba1a1a"
UI_DANGER_BG = "#fff1f0"


def ui_font(size: int = 10, weight: str = "normal") -> tuple[str, int, str]:
    return ("Microsoft YaHei UI", size, weight)


def ui_center(root, width: int | None = None, height: int | None = None) -> None:
    root.update_idletasks()
    actual_width = width or root.winfo_width()
    actual_height = height or root.winfo_height()
    x = (root.winfo_screenwidth() - actual_width) // 2
    y = (root.winfo_screenheight() - actual_height) // 2
    root.geometry(f"{actual_width}x{actual_height}+{max(0, x)}+{max(0, y)}")


def ui_card(parent, title: str, icon: str = ""):
    import tkinter as tk

    outer = tk.Frame(parent, bg=UI_SURFACE, highlightbackground=UI_BORDER, highlightthickness=1)
    header = tk.Frame(outer, bg=UI_SURFACE)
    header.pack(fill=tk.X, padx=16, pady=(14, 8))
    if icon:
        tk.Label(header, text=icon, bg=UI_SURFACE, fg=UI_PRIMARY, font=ui_font(15, "bold")).pack(side=tk.LEFT, padx=(0, 8))
    tk.Label(header, text=title, bg=UI_SURFACE, fg=UI_TEXT, font=ui_font(13, "bold")).pack(side=tk.LEFT)
    body = tk.Frame(outer, bg=UI_SURFACE)
    body.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 16))
    return outer, body


def ui_button(parent, text: str, command, primary: bool = False, danger: bool = False, width: int = 12, height: int = 1):
    import tkinter as tk

    bg = UI_PRIMARY if primary else UI_SURFACE
    fg = "#ffffff" if primary else UI_TEXT
    if danger:
        bg = UI_PRIMARY
        fg = "#ffffff"
    button = tk.Button(
        parent,
        text=text,
        command=command,
        width=width,
        height=height,
        bg=bg,
        fg=fg,
        activebackground=UI_PRIMARY_DARK if (primary or danger) else UI_PANEL,
        activeforeground="#ffffff" if (primary or danger) else UI_TEXT,
        relief=tk.FLAT,
        bd=0,
        highlightthickness=1,
        highlightbackground=UI_PRIMARY if (primary or danger) else UI_BORDER,
        font=ui_font(10, "bold" if primary else "normal"),
        cursor="hand2",
        padx=8,
        pady=5,
    )
    return button


def ui_input(parent, variable, width: int = 24, show: str | None = None):
    import tkinter as tk

    entry = tk.Entry(
        parent,
        textvariable=variable,
        width=width,
        show=show,
        bg="#ffffff",
        fg=UI_TEXT,
        insertbackground=UI_TEXT,
        relief=tk.FLAT,
        highlightthickness=1,
        highlightbackground=UI_BORDER,
        highlightcolor=UI_PRIMARY,
        font=ui_font(10),
    )
    return entry


def ui_spin(parent, variable, from_: int, to: int, increment: int = 1, width: int = 8):
    import tkinter as tk

    return tk.Spinbox(
        parent,
        from_=from_,
        to=to,
        increment=increment,
        textvariable=variable,
        width=width,
        bg="#ffffff",
        fg=UI_TEXT,
        relief=tk.FLAT,
        highlightthickness=1,
        highlightbackground=UI_BORDER,
        highlightcolor=UI_PRIMARY,
        font=ui_font(10),
    )


def ui_option(parent, variable, values: Sequence[str], width: int = 18):
    import tkinter as tk

    option = tk.OptionMenu(parent, variable, *values)
    option.configure(
        width=width,
        bg="#ffffff",
        fg=UI_TEXT,
        activebackground=UI_PANEL,
        relief=tk.FLAT,
        highlightthickness=1,
        highlightbackground=UI_BORDER,
        font=ui_font(10),
    )
    menu = option.nametowidget(option.menuname)
    menu.configure(font=ui_font(10), bg="#ffffff", fg=UI_TEXT, activebackground=UI_PANEL)
    return option


def ui_field_box(parent, label: str, row: int, column: int, sticky: str = "ew", padx: tuple[int, int] = (0, 12)):
    import tkinter as tk

    box = tk.Frame(parent, bg=UI_SURFACE)
    box.grid(row=row, column=column, sticky=sticky, padx=padx, pady=(0, 12))
    tk.Label(box, text=label, bg=UI_SURFACE, fg="#374151", font=ui_font(9)).pack(anchor="w", pady=(0, 5))
    return box


def ui_field(parent, label: str, make_widget, row: int, column: int, sticky: str = "ew", padx: tuple[int, int] = (0, 12)):
    box = ui_field_box(parent, label, row, column, sticky, padx)
    widget = make_widget(box)
    widget.pack(fill="x")
    return widget


def ask_start_options(cfg: ExportConfig) -> bool:
    import tkinter as tk
    from tkinter import filedialog, messagebox

    def region_text() -> str:
        if not cfg.region:
            return "尚未选择聊天区域"
        x, y, w, h = cfg.region
        return f"x={x}, y={y}, 宽={w}, 高={h}"

    def output_text() -> str:
        return str(Path(cfg.output_dir).resolve())

    set_dpi_awareness()
    root = tk.Tk()
    set_app_icon(root)
    root.title(f"ChatScreenExporter {APP_VERSION}")
    root.configure(bg=UI_BG)
    root.minsize(1080, 720)

    topbar = tk.Frame(root, bg=UI_PANEL, highlightbackground=UI_BORDER, highlightthickness=1, height=36)
    topbar.pack(fill=tk.X)
    topbar.pack_propagate(False)
    tk.Label(topbar, text="ChatScreenExporter", bg=UI_PANEL, fg=UI_TEXT, font=ui_font(12, "bold")).pack(side=tk.LEFT, padx=(14, 8))
    tk.Label(topbar, text=APP_VERSION, bg=UI_PANEL, fg=UI_PRIMARY, font=ui_font(9, "bold")).pack(side=tk.LEFT)
    ui_button(topbar, "开始导出 TXT", lambda: begin(), primary=True, width=18).pack(side=tk.RIGHT, padx=14, pady=4)

    shell = tk.Frame(root, bg=UI_BG)
    shell.pack(fill=tk.BOTH, expand=True)
    page_pack = {"side": tk.LEFT, "fill": tk.BOTH, "expand": True, "padx": 22, "pady": 18}
    pages = {}
    page_refreshers = {}
    nav_items = {}

    def refresh_nav(active_key: str) -> None:
        for key, parts in nav_items.items():
            item, bar, icon_label, text_label = parts
            active = key == active_key
            bg = "#e5e7eb" if active else UI_PANEL
            item.configure(bg=bg)
            bar.configure(bg=UI_PRIMARY if active else bg)
            icon_label.configure(bg=bg, fg=UI_PRIMARY if active else UI_TEXT)
            text_label.configure(bg=bg, font=ui_font(10, "bold" if active else "normal"))

    def show_page(page_key: str) -> None:
        page = pages.get(page_key)
        if page is None:
            return
        for frame in pages.values():
            frame.pack_forget()
        page.pack(**page_pack)
        refresh_nav(page_key)
        refresher = page_refreshers.get(page_key)
        if refresher:
            refresher()

    def make_nav_item(key: str, label: str, icon: str, active: bool = False) -> None:
        item = tk.Frame(sidebar, bg="#e5e7eb" if active else UI_PANEL, height=42, cursor="hand2")
        item.pack(fill=tk.X, pady=1)
        item.pack_propagate(False)
        bar = tk.Frame(item, bg=UI_PRIMARY if active else item["bg"], width=4)
        bar.pack(side=tk.LEFT, fill=tk.Y)
        icon_label = tk.Label(item, text=icon, bg=item["bg"], fg=UI_PRIMARY if active else UI_TEXT, font=ui_font(14), cursor="hand2")
        icon_label.pack(side=tk.LEFT, padx=(12, 10))
        text_label = tk.Label(item, text=label, bg=item["bg"], fg=UI_TEXT, font=ui_font(10, "bold" if active else "normal"), cursor="hand2")
        text_label.pack(side=tk.LEFT)
        nav_items[key] = (item, bar, icon_label, text_label)
        for widget in (item, bar, icon_label, text_label):
            widget.bind("<Button-1>", lambda _event, page_key=key: show_page(page_key))

    sidebar = tk.Frame(shell, bg=UI_PANEL, width=210, highlightbackground=UI_BORDER, highlightthickness=1)
    sidebar.pack(side=tk.LEFT, fill=tk.Y)
    sidebar.pack_propagate(False)
    tk.Label(sidebar, text="Tools", bg=UI_PANEL, fg=UI_PRIMARY, font=ui_font(17, "bold")).pack(anchor="w", padx=16, pady=(20, 0))
    tk.Label(sidebar, text=APP_VERSION, bg=UI_PANEL, fg=UI_MUTED, font=ui_font(9)).pack(anchor="w", padx=16, pady=(0, 20))
    make_nav_item("export", "Export Settings", "⚙", True)
    make_nav_item("history", "Chat History", "▤")
    make_nav_item("format", "File Format", "▧")
    make_nav_item("preferences", "Preferences", "☷")
    author_box = tk.Frame(sidebar, bg=UI_PANEL, highlightbackground=UI_BORDER, highlightthickness=1)
    author_box.pack(side=tk.BOTTOM, fill=tk.X, padx=14, pady=14, ipady=8)
    tk.Label(author_box, text="作者：SU", bg=UI_PANEL, fg=UI_PRIMARY, font=ui_font(10, "bold")).pack(anchor="w", padx=10)
    tk.Label(author_box, text="ChatScreenExporter", bg=UI_PANEL, fg=UI_MUTED, font=ui_font(8)).pack(anchor="w", padx=10, pady=(2, 0))

    main = tk.Frame(shell, bg=UI_BG)
    main.pack(**page_pack)
    pages["export"] = main
    main.grid_columnconfigure(0, weight=1)
    main.grid_columnconfigure(1, weight=1)

    tk.Label(main, text="聊天记录导出工具", bg=UI_BG, fg=UI_TEXT, font=ui_font(20, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
    tk.Label(main, text="自动截屏、翻页、AI识别，并导出 TXT 聊天记录", bg=UI_BG, fg="#374151", font=ui_font(10)).grid(row=1, column=0, columnspan=2, sticky="w", pady=(3, 14))

    self_var = tk.StringVar(value=cfg.self_name)
    other_var = tk.StringVar(value=cfg.other_name)
    region_var = tk.StringVar(value=region_text())
    output_var = tk.StringVar(value=output_text())
    preset_var = tk.StringVar(value=cfg.speed_preset if cfg.speed_preset in SPEED_PRESETS else "快速")
    pipeline_var = tk.BooleanVar(value=cfg.ai_pipeline)
    ai_enabled_var = tk.BooleanVar(value=cfg.recognition_mode == "ai")
    concurrency_var = tk.StringVar(value=str(cfg.ai_concurrency))
    batch_var = tk.StringVar(value=str(cfg.ai_batch_size))
    image_width_var = tk.StringVar(value=str(cfg.ai_image_max_width))
    overlap_var = tk.StringVar(value=str(cfg.page_overlap_clicks))
    max_errors_var = tk.StringVar(value=str(cfg.max_page_errors))
    max_pages_var = tk.StringVar(value=str(cfg.max_pages))
    base_url_var = tk.StringVar(value=cfg.ai_base_url)
    model_var = tk.StringVar(value=cfg.ai_model)
    api_key_var = tk.StringVar(value="")

    scroll_labels = {"page": "整屏翻页", "wheel": "滚轮滚动"}
    scroll_values = {value: key for key, value in scroll_labels.items()}
    scroll_var = tk.StringVar(value=scroll_labels.get(cfg.scroll_mode, "整屏翻页"))
    format_labels = {"responses": "OpenAI Responses", "chat_completions": "Chat Completions"}
    format_values = {value: key for key, value in format_labels.items()}
    format_var = tk.StringVar(value=format_labels.get(cfg.ai_api_format, "Chat Completions"))

    people_card, people = ui_card(main, "人物信息", "♙")
    people_card.grid(row=2, column=0, sticky="nsew", padx=(0, 10), pady=(0, 14))
    people.grid_columnconfigure(0, weight=1)
    people.grid_columnconfigure(1, weight=1)
    ui_field(people, "我的名字", lambda box: ui_input(box, self_var), 0, 0)
    ui_field(people, "对方名字", lambda box: ui_input(box, other_var), 0, 1, padx=(0, 0))
    hint = tk.Label(people, text="💡 右侧气泡默认为我，左侧气泡默认为对方", bg="#eef2f7", fg="#374151", font=ui_font(9), anchor="w")
    hint.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 2))

    region_card, region_body = ui_card(main, "读取范围", "□")
    region_card.grid(row=2, column=1, sticky="nsew", padx=(10, 0), pady=(0, 14))
    region_box = tk.Frame(region_body, bg=UI_SURFACE, highlightbackground=UI_BORDER, highlightthickness=1)
    region_box.pack(fill=tk.X, pady=(2, 10), ipady=12)
    tk.Label(region_box, textvariable=region_var, bg=UI_SURFACE, fg=UI_TEXT, font=ui_font(11, "bold")).pack(side=tk.LEFT, padx=14)

    def choose_region() -> None:
        root.withdraw()
        root.update_idletasks()
        time.sleep(0.15)
        try:
            cfg.region = select_region()
            region_var.set(region_text())
        except SystemExit:
            pass
        finally:
            root.deiconify()
            root.attributes("-topmost", True)
            root.lift()

    ui_button(region_box, "▣ 框选聊天范围", choose_region, primary=True, width=16).pack(side=tk.RIGHT, padx=14)
    tk.Label(region_body, text="提示：只框选聊天消息列表，不要包含输入框或左侧会话列表", bg=UI_SURFACE, fg="#374151", font=ui_font(9, "italic")).pack(anchor="w")

    ai_card, ai = ui_card(main, "AI 识别设置", "⚙")
    ai_card.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(0, 14))
    ai.grid_columnconfigure(0, weight=1)
    ai.grid_columnconfigure(1, weight=1)
    ai.grid_columnconfigure(2, weight=1)
    tk.Checkbutton(
        ai,
        text="启用 AI 识别",
        variable=ai_enabled_var,
        bg=UI_SURFACE,
        fg=UI_TEXT,
        activebackground=UI_SURFACE,
        selectcolor=UI_SURFACE,
        font=ui_font(10),
    ).grid(row=0, column=2, sticky="e", pady=(0, 8))
    tk.Checkbutton(
        ai,
        text="AI流水线加速",
        variable=pipeline_var,
        bg=UI_SURFACE,
        fg=UI_TEXT,
        activebackground=UI_SURFACE,
        selectcolor=UI_SURFACE,
        font=ui_font(10),
    ).grid(row=0, column=0, sticky="w", pady=(0, 8))
    ui_field(ai, "API 地址", lambda box: ui_input(box, base_url_var, width=38), 1, 0)
    ui_field(ai, "模型名称", lambda box: ui_input(box, model_var, width=26), 1, 1)
    ui_field(ai, "API Key", lambda box: ui_input(box, api_key_var, width=26, show="*"), 1, 2, padx=(0, 0))
    ui_field(ai, "接口格式", lambda box: ui_option(box, format_var, list(format_values.keys()), width=20), 2, 0)
    ui_field(ai, "AI 并发数", lambda box: ui_spin(box, concurrency_var, 1, 8), 2, 1)
    row2right = tk.Frame(ai, bg=UI_SURFACE)
    row2right.grid(row=2, column=2, sticky="ew", padx=(0, 0), pady=(0, 12))
    row2right.grid_columnconfigure(0, weight=1)
    row2right.grid_columnconfigure(1, weight=1)
    ui_field(row2right, "AI 合并屏数", lambda box: ui_spin(box, batch_var, 1, 6), 0, 0)
    ui_field(row2right, "上传宽度(px)", lambda box: ui_spin(box, image_width_var, 600, 2400, 80), 0, 1, padx=(0, 0))
    warn = tk.Label(ai, text="⚠  照片、表情包、文件会跳过，不写入 TXT", bg=UI_DANGER_BG, fg=UI_DANGER, font=ui_font(9), anchor="w", highlightbackground="#f4b8b8", highlightthickness=1)
    warn.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(0, 2), ipady=7)

    speed_card, speed = ui_card(main, "翻页与速度", "▸")
    speed_card.grid(row=4, column=0, sticky="nsew", padx=(0, 10), pady=(0, 14))
    speed.grid_columnconfigure(0, weight=1)
    speed.grid_columnconfigure(1, weight=1)
    ui_field(speed, "翻页方式", lambda box: ui_option(box, scroll_var, list(scroll_values.keys()), width=18), 0, 0)
    ui_field(speed, "速度模式", lambda box: ui_option(box, preset_var, list(SPEED_PRESETS.keys()), width=18), 0, 1, padx=(0, 0))
    ui_field(speed, "防漏重叠", lambda box: ui_spin(box, overlap_var, 0, 10), 1, 0)
    ui_field(speed, "异常容错批次", lambda box: ui_spin(box, max_errors_var, 1, 999), 1, 1, padx=(0, 0))
    ui_field(speed, "最多扫描屏数", lambda box: ui_spin(box, max_pages_var, 1, 20000, 50, 10), 2, 0)
    tk.Label(speed, text="💡 整屏翻页使用 PageUp/PageDown，速度最快", bg=UI_SURFACE, fg="#374151", font=ui_font(9, "italic")).grid(row=3, column=0, columnspan=2, sticky="w")

    save_card, save_body = ui_card(main, "保存位置", "▱")
    save_card.grid(row=4, column=1, sticky="nsew", padx=(10, 0), pady=(0, 14))
    save_row = tk.Frame(save_body, bg=UI_SURFACE)
    save_row.pack(fill=tk.X, pady=(0, 14))
    path_label = tk.Label(save_row, textvariable=output_var, bg="#eef2f7", fg=UI_TEXT, font=("Consolas", 10), anchor="w", justify="left", highlightbackground=UI_BORDER, highlightthickness=1)
    path_label.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=8)

    def choose_output_dir() -> None:
        selected = filedialog.askdirectory(parent=root, title="选择 TXT 保存文件夹", initialdir=output_text())
        if selected:
            cfg.output_dir = selected
            output_var.set(output_text())

    ui_button(save_row, "选择保存位置", choose_output_dir, width=14).pack(side=tk.LEFT, padx=(10, 0))
    preview = tk.Label(save_body, text="OUTPUT PREVIEW", bg="#d7dde2", fg="#6b7280", font=ui_font(10, "bold"), height=4)
    preview.pack(fill=tk.X)

    def open_output_dir() -> None:
        try:
            Path(cfg.output_dir).mkdir(parents=True, exist_ok=True)
            os.startfile(str(Path(cfg.output_dir).resolve()))
        except Exception as exc:
            messagebox.showerror("无法打开文件夹", str(exc), parent=root)

    history_page = tk.Frame(shell, bg=UI_BG)
    history_page.grid_columnconfigure(0, weight=1)
    pages["history"] = history_page
    tk.Label(history_page, text="聊天记录", bg=UI_BG, fg=UI_TEXT, font=ui_font(20, "bold")).grid(row=0, column=0, sticky="w")
    tk.Label(history_page, text="查看最近导出的 TXT 文件，并确认当前识别规则", bg=UI_BG, fg="#374151", font=ui_font(10)).grid(row=1, column=0, sticky="w", pady=(3, 14))
    history_card, history_body = ui_card(history_page, "最近导出", "▤")
    history_card.grid(row=2, column=0, sticky="nsew", pady=(0, 14))
    history_list = tk.Listbox(
        history_body,
        height=8,
        bg="#ffffff",
        fg=UI_TEXT,
        relief=tk.FLAT,
        highlightthickness=1,
        highlightbackground=UI_BORDER,
        font=("Consolas", 10),
    )
    history_list.pack(fill=tk.BOTH, expand=True)
    history_files: list[Path] = []

    def refresh_history_files() -> None:
        history_files.clear()
        history_list.delete(0, tk.END)
        folder = Path(cfg.output_dir)
        if not folder.exists():
            history_list.insert(tk.END, "保存文件夹还不存在，导出后会自动创建。")
            return
        files = sorted(folder.glob("chat_export_*.txt"), key=lambda path: path.stat().st_mtime, reverse=True)[:12]
        if not files:
            history_list.insert(tk.END, "还没有导出的聊天记录。")
            return
        for file_path in files:
            history_files.append(file_path)
            size_kb = max(1, file_path.stat().st_size // 1024)
            history_list.insert(tk.END, f"{file_path.name}    {size_kb} KB")

    def open_selected_history() -> None:
        selection = history_list.curselection()
        if not selection or selection[0] >= len(history_files):
            messagebox.showinfo("选择 TXT", "请先选中一个已导出的 TXT 文件。", parent=root)
            return
        try:
            os.startfile(str(history_files[selection[0]].resolve()))
        except Exception as exc:
            messagebox.showerror("无法打开 TXT", str(exc), parent=root)

    history_buttons = tk.Frame(history_body, bg=UI_SURFACE)
    history_buttons.pack(fill=tk.X, pady=(12, 0))
    ui_button(history_buttons, "刷新列表", refresh_history_files, width=12).pack(side=tk.LEFT)
    ui_button(history_buttons, "打开 TXT", open_selected_history, primary=True, width=12).pack(side=tk.LEFT, padx=(10, 0))
    ui_button(history_buttons, "打开文件夹", open_output_dir, width=12).pack(side=tk.LEFT, padx=(10, 0))
    rule_card, rule_body = ui_card(history_page, "识别规则", "□")
    rule_card.grid(row=3, column=0, sticky="ew")
    for line in [
        "右侧聊天气泡会写成“我的名字”，左侧聊天气泡会写成“对方名字”。",
        "微信中间显示的时间会向下继承，连续消息不会强行生成假时间。",
        "照片、表情包、文件消息会跳过，不写入 TXT。",
    ]:
        tk.Label(rule_body, text=line, bg=UI_SURFACE, fg=UI_TEXT, font=ui_font(10), anchor="w").pack(fill=tk.X, pady=3)
    page_refreshers["history"] = refresh_history_files

    format_page = tk.Frame(shell, bg=UI_BG)
    format_page.grid_columnconfigure(0, weight=1)
    pages["format"] = format_page
    tk.Label(format_page, text="文件格式", bg=UI_BG, fg=UI_TEXT, font=ui_font(20, "bold")).grid(row=0, column=0, sticky="w")
    tk.Label(format_page, text="TXT 会按时间、发送人、消息内容逐行保存", bg=UI_BG, fg="#374151", font=ui_font(10)).grid(row=1, column=0, sticky="w", pady=(3, 14))
    format_card, format_body = ui_card(format_page, "TXT 输出预览", "▧")
    format_card.grid(row=2, column=0, sticky="nsew", pady=(0, 14))
    format_preview = tk.Text(
        format_body,
        height=10,
        bg="#ffffff",
        fg=UI_TEXT,
        relief=tk.FLAT,
        highlightthickness=1,
        highlightbackground=UI_BORDER,
        font=("Consolas", 10),
        wrap="word",
    )
    format_preview.pack(fill=tk.BOTH, expand=True)

    def refresh_format_preview() -> None:
        mine = self_var.get().strip() or "我"
        other = other_var.get().strip() or "对方"
        sample = (
            f"[2026-05-28 20:00] {other}: 示例消息会这样写入 TXT\\n"
            f"[2026-05-28 20:00] {mine}: 连续消息会继承上一条可见时间\\n"
            f"[未知时间] {other}: 如果截图里没有可用时间，就会标记为未知时间\\n"
            "\\n"
            "照片、表情包、文件：跳过，不写入 TXT\\n"
            "文件名：chat_export_YYYYMMDD_HHMMSS.txt"
        )
        format_preview.configure(state="normal")
        format_preview.delete("1.0", tk.END)
        format_preview.insert("1.0", sample)
        format_preview.configure(state="disabled")

    format_rule_card, format_rule_body = ui_card(format_page, "保存规则", "✓")
    format_rule_card.grid(row=3, column=0, sticky="ew")
    for line in [
        "每条文本消息单独占一行，便于搜索、复制和长期保存。",
        "导出完成后会弹出结果窗口，可以直接打开 TXT 或打开保存文件夹。",
        "保存位置可以在 Export Settings 页随时修改。",
    ]:
        tk.Label(format_rule_body, text=line, bg=UI_SURFACE, fg=UI_TEXT, font=ui_font(10), anchor="w").pack(fill=tk.X, pady=3)
    page_refreshers["format"] = refresh_format_preview

    preferences_page = tk.Frame(shell, bg=UI_BG)
    preferences_page.grid_columnconfigure(0, weight=1)
    preferences_page.grid_columnconfigure(1, weight=1)
    pages["preferences"] = preferences_page
    tk.Label(preferences_page, text="偏好设置", bg=UI_BG, fg=UI_TEXT, font=ui_font(20, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
    tk.Label(preferences_page, text="保存常用运行方式，或一键切换推荐参数", bg=UI_BG, fg="#374151", font=ui_font(10)).grid(row=1, column=0, columnspan=2, sticky="w", pady=(3, 14))
    run_card, run_body = ui_card(preferences_page, "运行偏好", "☷")
    run_card.grid(row=2, column=0, sticky="nsew", padx=(0, 10), pady=(0, 14))
    tk.Checkbutton(run_body, text="启用 AI 识别", variable=ai_enabled_var, bg=UI_SURFACE, fg=UI_TEXT, activebackground=UI_SURFACE, selectcolor=UI_SURFACE, font=ui_font(10)).pack(anchor="w", pady=3)
    tk.Checkbutton(run_body, text="启用 AI 流水线加速", variable=pipeline_var, bg=UI_SURFACE, fg=UI_TEXT, activebackground=UI_SURFACE, selectcolor=UI_SURFACE, font=ui_font(10)).pack(anchor="w", pady=3)
    tk.Label(run_body, text="屏幕边界停止：已启用", bg=UI_DANGER_BG, fg=UI_DANGER, font=ui_font(10, "bold"), anchor="w", highlightbackground="#f4b8b8", highlightthickness=1).pack(fill=tk.X, pady=(10, 0), ipady=7)
    preset_card, preset_body = ui_card(preferences_page, "推荐参数", "▸")
    preset_card.grid(row=2, column=1, sticky="nsew", padx=(10, 0), pady=(0, 14))

    def use_fast_recommendation() -> None:
        scroll_var.set("整屏翻页")
        preset_var.set("快速")
        pipeline_var.set(True)
        concurrency_var.set("3")
        batch_var.set("3")
        image_width_var.set("1280")
        overlap_var.set("3")
        max_errors_var.set("20")
        messagebox.showinfo("已切换", "已切换为快速推荐参数。", parent=root)

    def use_stable_recommendation() -> None:
        scroll_var.set("整屏翻页")
        preset_var.set("稳定")
        pipeline_var.set(True)
        concurrency_var.set("2")
        batch_var.set("2")
        image_width_var.set("1280")
        overlap_var.set("4")
        max_errors_var.set("30")
        messagebox.showinfo("已切换", "已切换为稳定推荐参数。", parent=root)

    ui_button(preset_body, "快速推荐", use_fast_recommendation, primary=True, width=14).pack(side=tk.LEFT)
    ui_button(preset_body, "稳定推荐", use_stable_recommendation, width=14).pack(side=tk.LEFT, padx=(10, 0))
    tk.Label(preset_body, text="快速推荐适合 AI 接口稳定时使用；稳定推荐会降低并发、增加容错。", bg=UI_SURFACE, fg="#374151", font=ui_font(9), anchor="w", wraplength=360).pack(fill=tk.X, pady=(14, 0))
    manage_card, manage_body = ui_card(preferences_page, "维护", "□")
    manage_card.grid(row=3, column=0, columnspan=2, sticky="ew")

    def open_config_dir() -> None:
        try:
            os.startfile(str(Path(CONFIG_FILE).resolve().parent))
        except Exception as exc:
            messagebox.showerror("无法打开目录", str(exc), parent=root)

    ui_button(manage_body, "保存配置", lambda: apply_form(True), primary=True, width=12).pack(side=tk.LEFT)
    ui_button(manage_body, "打开配置目录", open_config_dir, width=14).pack(side=tk.LEFT, padx=(10, 0))
    ui_button(manage_body, "返回导出设置", lambda: show_page("export"), width=14).pack(side=tk.LEFT, padx=(10, 0))
    refresh_nav("export")

    result = {"start": False}

    def apply_form(show_message: bool = False) -> bool:
        self_name = self_var.get().strip()
        other_name = other_var.get().strip()
        if not self_name or not other_name:
            messagebox.showwarning("需要填写名字", "请把两个人的名字都填上。", parent=root)
            return False
        if not cfg.region:
            messagebox.showinfo("选择聊天范围", "请先框选聊天消息区域。只选消息列表，不要包含底部输入框。", parent=root)
            choose_region()
            if not cfg.region:
                return False
        try:
            cfg.max_pages = max(1, int(max_pages_var.get().strip()))
            cfg.ai_concurrency = min(8, max(1, int(concurrency_var.get().strip())))
            cfg.ai_batch_size = min(6, max(1, int(batch_var.get().strip())))
            cfg.ai_image_max_width = min(2400, max(600, int(image_width_var.get().strip())))
            cfg.page_overlap_clicks = min(10, max(0, int(overlap_var.get().strip())))
            cfg.max_page_errors = max(1, int(max_errors_var.get().strip()))
        except ValueError:
            messagebox.showwarning("数字无效", "并发数、合并屏数、上传宽度、防漏重叠、异常容错和扫描屏数都需要填写数字。", parent=root)
            return False
        cfg.self_name = self_name
        cfg.other_name = other_name
        cfg.recognition_mode = "ai" if ai_enabled_var.get() else "ocr"
        cfg.ai_pipeline = bool(pipeline_var.get())
        cfg.scroll_mode = scroll_values.get(scroll_var.get(), "page")
        cfg.ai_api_format = format_values.get(format_var.get(), "chat_completions")
        cfg.ai_base_url = base_url_var.get().strip() or "https://api.openai.com/v1"
        cfg.ai_model = model_var.get().strip() or "gpt-4.1-mini"
        if cfg.recognition_mode == "ai" and api_key_var.get().strip():
            os.environ["AI_API_KEY"] = api_key_var.get().strip()
            os.environ["OPENAI_API_KEY"] = api_key_var.get().strip()
        cfg.speed_preset = preset_var.get()
        preset = SPEED_PRESETS.get(cfg.speed_preset, SPEED_PRESETS["快速"])
        cfg.scroll_clicks = int(preset["scroll_clicks"])
        cfg.page_delay = float(preset["page_delay"])
        cfg.ocr_scale = float(preset["ocr_scale"])
        if show_message:
            messagebox.showinfo("配置已应用", "配置已写入本次运行设置，开始导出后会保存到 config.json。", parent=root)
        return True

    def begin() -> None:
        if not apply_form():
            return
        result["start"] = True
        root.destroy()

    def cancel() -> None:
        root.destroy()

    footer = tk.Frame(root, bg=UI_PANEL, highlightbackground=UI_BORDER, highlightthickness=1, height=68)
    footer.pack(fill=tk.X, side=tk.BOTTOM)
    footer.pack_propagate(False)
    tk.Label(footer, text="READY", bg=UI_PANEL, fg=UI_PRIMARY, font=ui_font(9, "bold")).pack(side=tk.LEFT, padx=(18, 20))
    tk.Label(footer, text="Logs   Support   Documentation", bg=UI_PANEL, fg="#374151", font=ui_font(9)).pack(side=tk.LEFT)
    ui_button(footer, "开始导出 TXT", begin, primary=True, width=20, height=2).pack(side=tk.RIGHT, padx=(8, 18), pady=10)
    ui_button(footer, "保存配置", lambda: apply_form(True), width=12).pack(side=tk.RIGHT, padx=8, pady=10)
    ui_button(footer, "取消", cancel, width=10).pack(side=tk.RIGHT, padx=8, pady=10)

    root.bind("<Return>", lambda _event: begin())
    root.bind("<Escape>", lambda _event: cancel())
    ui_center(root, 1180, 760)
    root.mainloop()
    return result["start"]


class ScanControlWindow:
    def __init__(self, max_pages: int) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.root = tk.Tk()
        set_app_icon(self.root)
        self.root.title(f"正在导出 {APP_VERSION}")
        self.root.attributes("-topmost", True)
        self.root.configure(bg=UI_BG)
        self.root.resizable(False, False)
        self.paused = False
        self.stop_requested = False
        self.stop_reason = ""
        self.max_pages = max_pages

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except self.tk.TclError:
            pass
        style.configure("Modern.Horizontal.TProgressbar", troughcolor="#dbe4ee", background=UI_PRIMARY, bordercolor="#dbe4ee", lightcolor=UI_PRIMARY, darkcolor=UI_PRIMARY)

        card = tk.Frame(self.root, bg=UI_SURFACE, highlightbackground=UI_BORDER, highlightthickness=1)
        card.pack(fill=tk.BOTH, expand=True, padx=14, pady=14)

        header = tk.Frame(card, bg=UI_SURFACE)
        header.pack(fill=tk.X, padx=18, pady=(14, 10))
        tk.Label(header, text="↻", bg=UI_SURFACE, fg=UI_PRIMARY, font=ui_font(13, "bold")).pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(header, text="正在导出", bg=UI_SURFACE, fg=UI_TEXT, font=ui_font(13, "bold")).pack(side=tk.LEFT)

        body = tk.Frame(card, bg=UI_SURFACE)
        body.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 14))
        self.status_var = tk.StringVar(value="准备开始...")
        self.detail_var = tk.StringVar(value=f"最多扫描 {max_pages} 屏")
        self.progress_var = tk.DoubleVar(value=0)
        tk.Label(body, textvariable=self.status_var, bg=UI_SURFACE, fg=UI_TEXT, font=ui_font(10, "bold")).pack(anchor="w", pady=(2, 6))
        self.progress = ttk.Progressbar(body, variable=self.progress_var, maximum=100, style="Modern.Horizontal.TProgressbar")
        self.progress.pack(fill=tk.X, pady=(0, 12))
        stat = tk.Frame(body, bg="#eef2f7", highlightbackground=UI_LINE, highlightthickness=1)
        stat.pack(fill=tk.X, pady=(0, 12), ipady=8)
        tk.Label(stat, text="▣ 实时统计", bg="#eef2f7", fg="#374151", font=ui_font(9, "bold")).pack(anchor="w", padx=12)
        tk.Label(stat, textvariable=self.detail_var, bg="#eef2f7", fg=UI_TEXT, font=ui_font(10)).pack(anchor="w", padx=12, pady=(4, 0))
        tk.Label(body, text="ⓘ 停止后会保留已识别内容", bg=UI_SURFACE, fg="#374151", font=ui_font(9)).pack(anchor="w")
        tk.Label(
            body,
            text="⚠ 将鼠标移到屏幕边界即可停止",
            bg=UI_DANGER_BG,
            fg=UI_DANGER,
            font=ui_font(10, "bold"),
            anchor="w",
            highlightbackground="#f4b8b8",
            highlightthickness=1,
        ).pack(fill=tk.X, pady=(8, 0), ipady=7, padx=0)

        buttons = tk.Frame(card, bg=UI_PANEL)
        buttons.pack(fill=tk.X, side=tk.BOTTOM, pady=(0, 0), ipady=10)
        self.pause_button = ui_button(buttons, "暂停", self.toggle_pause, width=10)
        self.pause_button.pack(side=tk.RIGHT, padx=(8, 8))
        ui_button(buttons, "停止并保存", self.request_stop, primary=True, width=14).pack(side=tk.RIGHT, padx=(8, 18))

        self.root.protocol("WM_DELETE_WINDOW", self.request_stop)
        ui_center(self.root, 430, 310)
        self.safe_update()

    def _set_progress_from_text(self, status: str) -> None:
        match = re.search(r"(?:第\s*)?(\d+)\s*/\s*(\d+)", status)
        if match:
            current = int(match.group(1))
            total = max(1, int(match.group(2)))
            self.progress_var.set(min(100, current / total * 100))

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self.pause_button.configure(text="继续" if self.paused else "暂停")
        self.safe_update()

    def request_stop(self) -> None:
        self.stop_requested = True
        if not self.stop_reason:
            self.stop_reason = "用户停止"
        self.status_var.set("正在停止并保存已识别内容...")
        self.safe_update()

    def trigger_edge_stop(self) -> None:
        if self.stop_requested:
            return
        self.stop_requested = True
        self.stop_reason = "鼠标移动到屏幕边界，触发安全停止"
        self.status_var.set("已触发屏幕边界安全停止")
        self.detail_var.set("正在停止并保存已识别内容...")

    def check_screen_edge_stop(self) -> None:
        if self.stop_requested:
            return
        try:
            import pyautogui  # type: ignore

            x, y = pyautogui.position()
            width, height = pyautogui.size()
        except Exception as exc:
            if exc.__class__.__name__ == "FailSafeException":
                self.trigger_edge_stop()
            return
        margin = SCREEN_EDGE_STOP_MARGIN
        if x <= margin or y <= margin or x >= width - 1 - margin or y >= height - 1 - margin:
            self.trigger_edge_stop()

    def set_text(self, status: str, detail: str) -> None:
        self.status_var.set(status)
        self.detail_var.set(detail)
        self._set_progress_from_text(status)
        self.safe_update()

    def set_status(self, page_number: int, max_pages: int, page_messages: int, page_count: int, total_messages: int) -> None:
        self.status_var.set(f"正在识别：第 {page_number}/{max_pages} 屏")
        self.detail_var.set(f"本页 {page_messages} 条，已处理 {page_count} 屏，已识别 {total_messages} 条消息")
        self.progress_var.set(min(100, page_number / max(1, max_pages) * 100))
        self.safe_update()

    def safe_update(self) -> None:
        try:
            self.check_screen_edge_stop()
            self.root.update_idletasks()
            self.root.update()
        except self.tk.TclError:
            self.stop_requested = True

    def wait_if_paused(self) -> None:
        while self.paused and not self.stop_requested:
            self.safe_update()
            time.sleep(0.12)

    def sleep(self, seconds: float) -> None:
        end_at = time.time() + max(0.0, seconds)
        while time.time() < end_at and not self.stop_requested:
            self.wait_if_paused()
            self.safe_update()
            time.sleep(0.05)

    def close(self) -> None:
        try:
            self.root.destroy()
        except self.tk.TclError:
            pass


def show_result_dialog(output_path: Path, message_count: int, page_count: int, stopped: bool, reason: str = "") -> None:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    set_app_icon(root)
    root.title(f"导出结果 {APP_VERSION}")
    root.attributes("-topmost", True)
    root.configure(bg=UI_BG)
    root.resizable(False, False)

    card = tk.Frame(root, bg=UI_SURFACE, highlightbackground=UI_BORDER, highlightthickness=1)
    card.pack(fill=tk.BOTH, expand=True, padx=18, pady=18)

    title = "已停止并保存" if stopped else "导出完成"
    tk.Label(card, text="✓", bg="#e6f3f6", fg=UI_PRIMARY, font=ui_font(28, "bold"), width=3).pack(pady=(28, 10))
    tk.Label(card, text="TXT 文件已保存" if not stopped else "已保存当前识别结果", bg=UI_SURFACE, fg=UI_TEXT, font=ui_font(18, "bold")).pack()
    tk.Label(card, text=title, bg=UI_SURFACE, fg="#374151", font=ui_font(10)).pack(pady=(6, 20))

    path_box = tk.Frame(card, bg="#eef2f7", highlightbackground=UI_BORDER, highlightthickness=1)
    path_box.pack(fill=tk.X, padx=28, pady=(0, 16))
    tk.Label(path_box, text="输出路径", bg="#eef2f7", fg="#374151", font=ui_font(9, "bold")).pack(anchor="w", padx=12, pady=(9, 2))
    tk.Label(path_box, text=str(output_path.resolve()), bg="#eef2f7", fg=UI_PRIMARY, font=("Consolas", 10), wraplength=460, justify="left").pack(anchor="w", padx=12, pady=(0, 10))

    stats = tk.Frame(card, bg=UI_SURFACE, highlightbackground=UI_LINE, highlightthickness=1)
    stats.pack(fill=tk.X, padx=28, pady=(0, 14))
    for label, value, color in [
        ("扫描屏数", str(page_count), UI_TEXT),
        ("导出消息", str(message_count), UI_TEXT),
        ("状态", "停止" if stopped else "完成", UI_DANGER if stopped else UI_PRIMARY),
    ]:
        cell = tk.Frame(stats, bg=UI_SURFACE)
        cell.pack(side=tk.LEFT, expand=True, fill=tk.X, pady=10)
        tk.Label(cell, text=label, bg=UI_SURFACE, fg=UI_MUTED, font=ui_font(9)).pack()
        tk.Label(cell, text=value, bg=UI_SURFACE, fg=color, font=ui_font(13, "bold")).pack()
    if reason:
        tk.Label(card, text=f"停止原因：{reason}", bg=UI_SURFACE, fg=UI_MUTED, wraplength=520, justify="left", font=ui_font(9)).pack(anchor="w", padx=28, pady=(0, 10))

    def open_file() -> None:
        try:
            os.startfile(str(output_path.resolve()))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("无法打开文件", str(exc), parent=root)

    def open_folder() -> None:
        try:
            os.startfile(str(output_path.resolve().parent))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("无法打开文件夹", str(exc), parent=root)

    buttons = tk.Frame(card, bg=UI_SURFACE)
    buttons.pack(fill=tk.X, padx=28, pady=(6, 24))
    ui_button(buttons, "打开 TXT", open_file, primary=True, width=14).pack(side=tk.LEFT)
    ui_button(buttons, "打开文件夹", open_folder, width=14).pack(side=tk.LEFT, padx=(12, 0))
    ui_button(buttons, "关闭", root.destroy, width=10).pack(side=tk.RIGHT)

    ui_center(root, 520, 500)
    root.mainloop()


def advance_chat(pyautogui, cfg: ExportConfig, center_x: int, center_y: int, focus_x: int, focus_y: int) -> None:
    if cfg.scroll_mode == "page":
        pyautogui.click(focus_x, focus_y)
        pyautogui.press("pageup" if cfg.direction == "up" else "pagedown")
        if cfg.page_overlap_clicks > 0:
            overlap_amount = -cfg.page_overlap_clicks if cfg.direction == "up" else cfg.page_overlap_clicks
            pyautogui.scroll(overlap_amount)
    else:
        pyautogui.moveTo(center_x, center_y, duration=0)
        scroll_amount = cfg.scroll_clicks if cfg.direction == "up" else -cfg.scroll_clicks
        pyautogui.scroll(scroll_amount)


def capture_screens(pyautogui, cfg: ExportConfig, control: ScanControlWindow, region_tuple, center_x: int, center_y: int, focus_x: int, focus_y: int) -> tuple[list[ScreenshotCapture], str, bool]:
    captures: list[ScreenshotCapture] = []
    previous_hash = ""
    stable_count = 0
    stop_reason = ""
    normal_auto_stop = False

    for page_number in range(1, cfg.max_pages + 1):
        control.wait_if_paused()
        if control.stop_requested:
            stop_reason = "用户停止"
            break

        screenshot = pyautogui.screenshot(region=region_tuple)
        shot_hash = image_hash(screenshot)
        if previous_hash and shot_hash == previous_hash:
            stable_count += 1
            control.set_text(
                f"正在快速截屏：第 {page_number}/{cfg.max_pages} 屏",
                f"画面未变化 {stable_count}/{cfg.stable_stop_pages}，已截屏 {len(captures)} 屏",
            )
            if stable_count >= cfg.stable_stop_pages:
                stop_reason = "连续多页画面未变化"
                normal_auto_stop = True
                break
            advance_chat(pyautogui, cfg, center_x, center_y, focus_x, focus_y)
            control.sleep(cfg.ai_capture_delay if cfg.recognition_mode == "ai" and cfg.ai_pipeline else cfg.page_delay)
            continue
        else:
            stable_count = 0
        previous_hash = shot_hash

        captures.append(ScreenshotCapture(page_number=page_number, image_hash=shot_hash, image=screenshot))
        control.set_text(f"正在快速截屏：第 {page_number}/{cfg.max_pages} 屏", f"已截屏 {len(captures)} 屏，稍后并发识别")

        advance_chat(pyautogui, cfg, center_x, center_y, focus_x, focus_y)
        control.sleep(cfg.ai_capture_delay if cfg.recognition_mode == "ai" and cfg.ai_pipeline else cfg.page_delay)

    return captures, stop_reason, normal_auto_stop


def recognize_capture(capture: ScreenshotCapture, cfg: ExportConfig, backend: OCRBackend | None) -> PageCapture:
    if cfg.recognition_mode == "ai":
        messages = extract_messages_with_ai(capture.image, cfg)
    else:
        assert backend is not None
        ocr_lines = backend.read_lines(capture.image)
        messages = extract_messages(ocr_lines, capture.image.size, cfg)  # type: ignore[attr-defined]
    return PageCapture(page_number=capture.page_number, image_hash=capture.image_hash, messages=messages)


def chunked_captures(captures: Sequence[ScreenshotCapture], size: int) -> list[list[ScreenshotCapture]]:
    size = max(1, int(size))
    return [list(captures[index : index + size]) for index in range(0, len(captures), size)]


def recognize_capture_batch(
    captures: Sequence[ScreenshotCapture],
    cfg: ExportConfig,
    backend: OCRBackend | None,
    control: ScanControlWindow | None = None,
) -> list[PageCapture]:
    if cfg.recognition_mode == "ai" and len(captures) > 1:
        try:
            return extract_pages_with_ai_batch(captures, cfg)
        except Exception as exc:
            if is_fatal_recognition_error(exc):
                raise
            if is_timeout_recognition_error(exc):
                raise RuntimeError(
                    f"批量识别超时，已跳过第 {captures[0].page_number}-{captures[-1].page_number} 屏，避免继续逐屏超时：{exc}"
                ) from exc
            if control and control.stop_requested:
                return empty_pages_for(captures)
            print(f"批量识别失败，改用单屏重试：{exc}")

    pages: list[PageCapture] = []
    for capture in captures:
        if control and control.stop_requested:
            pages.append(PageCapture(page_number=capture.page_number, image_hash=capture.image_hash, messages=[]))
            continue
        try:
            pages.append(recognize_capture(capture, cfg, backend))
        except Exception as exc:
            if is_fatal_recognition_error(exc):
                raise
            print(f"第 {capture.page_number} 屏识别异常，已保留空页继续：{exc}")
            pages.append(PageCapture(page_number=capture.page_number, image_hash=capture.image_hash, messages=[]))
    return pages


def recognize_captures(captures: list[ScreenshotCapture], cfg: ExportConfig, backend: OCRBackend | None, control: ScanControlWindow) -> tuple[list[PageCapture], str]:
    pages_by_number: dict[int, PageCapture] = {}
    stop_reason = ""
    consecutive_errors = 0
    batch_size = cfg.ai_batch_size if cfg.recognition_mode == "ai" else 1
    batches = chunked_captures(captures, batch_size)

    if cfg.recognition_mode == "ai" and cfg.ai_pipeline:
        max_workers = min(max(1, cfg.ai_concurrency), max(1, len(batches)))
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        futures: dict[concurrent.futures.Future, list[ScreenshotCapture]] = {}
        next_batch_index = 0
        completed = 0
        fast_shutdown = False

        def submit_more() -> None:
            nonlocal next_batch_index
            while (
                next_batch_index < len(batches)
                and len(futures) < max_workers
                and not control.stop_requested
                and not stop_reason
            ):
                batch = batches[next_batch_index]
                next_batch_index += 1
                futures[executor.submit(recognize_capture_batch, batch, cfg, backend, control)] = batch

        try:
            submit_more()
            while futures:
                control.safe_update()
                control.wait_if_paused()
                if control.stop_requested:
                    stop_reason = control.stop_reason or "用户停止"
                    fast_shutdown = True
                    for pending_future in futures:
                        pending_future.cancel()
                    break
                done, _pending = concurrent.futures.wait(
                    list(futures),
                    timeout=0.15,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                if not done:
                    continue
                for future in done:
                    batch = futures.pop(future)
                    if future.cancelled():
                        continue
                    try:
                        pages = future.result()
                        consecutive_errors = 0
                    except Exception as exc:
                        if is_fatal_recognition_error(exc):
                            raise
                        consecutive_errors += 1
                        pages = empty_pages_for(batch)
                        print(f"第 {batch[0].page_number}-{batch[-1].page_number} 屏识别异常，已保留空页继续：{exc}")
                        if consecutive_errors >= cfg.max_page_errors:
                            stop_reason = f"连续 {consecutive_errors} 个批次识别异常"
                            fast_shutdown = True
                    for page in pages:
                        pages_by_number[page.page_number] = page
                    completed += len(batch)
                    total_messages = sum(len(page.messages) for page in pages)
                    control.set_text(
                        f"正在并发识别：{completed}/{len(captures)} 屏",
                        f"本次第 {batch[0].page_number}-{batch[-1].page_number} 屏识别 {total_messages} 条",
                    )
                    if stop_reason:
                        for pending_future in futures:
                            pending_future.cancel()
                        break
                if stop_reason:
                    break
                submit_more()
        finally:
            executor.shutdown(wait=not fast_shutdown, cancel_futures=fast_shutdown)
        return [pages_by_number[number] for number in sorted(pages_by_number)], stop_reason

    for batch in batches:
        control.wait_if_paused()
        if control.stop_requested:
            stop_reason = control.stop_reason or "用户停止"
            break
        try:
            pages = recognize_capture_batch(batch, cfg, backend, control)
            consecutive_errors = 0
        except Exception as exc:
            if is_fatal_recognition_error(exc):
                raise
            consecutive_errors += 1
            pages = empty_pages_for(batch)
            print(f"第 {batch[0].page_number}-{batch[-1].page_number} 屏识别异常，已保留空页继续：{exc}")
            if consecutive_errors >= cfg.max_page_errors:
                stop_reason = f"连续 {consecutive_errors} 个批次识别异常"
                for page in pages:
                    pages_by_number[page.page_number] = page
                break
        for page in pages:
            pages_by_number[page.page_number] = page
        last_page = pages[-1] if pages else batch[-1]
        total_messages = sum(len(item.messages) for item in pages_by_number.values())
        batch_messages = sum(len(page.messages) for page in pages)
        control.set_status(last_page.page_number, cfg.max_pages, batch_messages, len(pages_by_number), total_messages)
    return [pages_by_number[number] for number in sorted(pages_by_number)], stop_reason


def run_export(args: argparse.Namespace) -> None:
    set_dpi_awareness()
    config_path = Path(args.config)
    cfg = apply_cli_overrides(load_config(config_path), args)
    if cfg.show_start_dialog and not args.no_start_dialog:
        if not ask_start_options(cfg):
            raise SystemExit("已取消导出。")
        save_config(config_path, cfg)

    if args.select_region or not cfg.region:
        cfg.region = select_region()
        save_config(config_path, cfg)
        print(f"已保存区域到 {config_path.resolve()}: {cfg.region}")

    if not cfg.region:
        raise SystemExit("没有聊天区域。请先运行 calibrate，或使用 --select-region。")
    if cfg.direction not in ("up", "down"):
        raise SystemExit("--direction 只能是 up 或 down。")
    if cfg.recognition_mode not in ("ocr", "ai"):
        raise SystemExit("--recognition-mode 只能是 ocr 或 ai。")
    if cfg.ai_api_format not in ("responses", "chat_completions"):
        raise SystemExit("--ai-api-format 只能是 responses 或 chat_completions。")
    if cfg.scroll_mode not in ("page", "wheel"):
        raise SystemExit("--scroll-mode 只能是 page 或 wheel。")
    cfg.ai_concurrency = min(8, max(1, int(cfg.ai_concurrency)))
    cfg.ai_batch_size = min(6, max(1, int(cfg.ai_batch_size)))
    cfg.ai_capture_delay = max(0.03, float(cfg.ai_capture_delay))
    cfg.ai_image_max_width = min(2400, max(600, int(cfg.ai_image_max_width)))
    cfg.ai_image_quality = min(95, max(55, int(cfg.ai_image_quality)))

    import pyautogui  # type: ignore

    backend = make_backend(cfg) if cfg.recognition_mode == "ocr" else None
    control = ScanControlWindow(cfg.max_pages)
    stop_reason = ""
    normal_auto_stop = False

    if backend:
        print(f"版本：{APP_VERSION}；OCR 后端：{backend.name}")
    else:
        print(f"版本：{APP_VERSION}；识别模式：AI识别；接口：{cfg.ai_api_format}；模型：{cfg.ai_model}")
        print(f"AI加速：合并 {cfg.ai_batch_size} 屏/请求；并发 {cfg.ai_concurrency}；上传宽度 {cfg.ai_image_max_width}px；质量 {cfg.ai_image_quality}")
    print(f"速度：{cfg.speed_preset}；滚动方式：{cfg.scroll_mode}；滚轮格数：{cfg.scroll_clicks}；滚动等待：{cfg.page_delay:.2f} 秒")
    print(f"{cfg.start_delay:.1f} 秒后开始。请把鼠标和聊天窗口保持在当前状态；运行中可用控制窗口暂停或停止。")

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.02
    region_tuple = tuple(cfg.region)
    x, y, w, h = cfg.region
    center_x = x + w // 2
    center_y = y + h // 2
    focus_x = x + max(1, w - 6)
    focus_y = center_y
    pages: list[PageCapture] = []

    try:
        control.status_var.set(f"{cfg.start_delay:.1f} 秒后开始，请保持聊天窗口可见。")
        control.sleep(max(0, cfg.start_delay))
        if control.stop_requested:
            stop_reason = control.stop_reason or "用户停止"

        if not stop_reason:
            captures, capture_reason, capture_auto_stop = capture_screens(
                pyautogui,
                cfg,
                control,
                region_tuple,
                center_x,
                center_y,
                focus_x,
                focus_y,
            )
            stop_reason = capture_reason
            normal_auto_stop = capture_auto_stop
            if control.stop_requested and not stop_reason:
                stop_reason = control.stop_reason or "用户停止"
            if captures and not control.stop_requested:
                recognized_pages, recognition_reason = recognize_captures(captures, cfg, backend, control)
                pages.extend(recognized_pages)
                if recognition_reason:
                    stop_reason = recognition_reason
                    normal_auto_stop = False
                for page in pages:
                    print(f"第 {page.page_number}/{cfg.max_pages} 页：识别 {len(page.messages)} 条消息")
    except KeyboardInterrupt:
        stop_reason = "Ctrl+C 中断"
        print("收到 Ctrl+C，正在整理已识别内容。")
    except pyautogui.FailSafeException:
        stop_reason = "鼠标移动到屏幕边界，触发安全停止"
        print("触发 pyautogui 安全停止，正在整理已识别内容。")
    except Exception as exc:
        stop_reason = f"异常停止：{exc}"
        print(stop_reason)
    finally:
        control.close()

    messages = flatten_pages(pages, cfg.direction, cfg.dedupe_window)
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = Path(cfg.output_dir) / f"chat_export_{timestamp}.txt"
    write_txt(messages, cfg, output_path, len(pages))
    save_config(config_path, cfg)
    print(f"完成：{output_path.resolve()}")
    print(f"共导出 {len(messages)} 条文本消息。")
    show_result_dialog(output_path, len(messages), len(pages), stopped=bool(stop_reason and not normal_auto_stop), reason=stop_reason)


def run_calibrate(args: argparse.Namespace) -> None:
    config_path = Path(args.config)
    cfg = apply_cli_overrides(load_config(config_path), args)
    cfg.region = select_region()
    save_config(config_path, cfg)
    print(f"已保存配置：{config_path.resolve()}")
    print(f"聊天区域：{cfg.region}")


def run_doctor(_args: argparse.Namespace) -> None:
    print(f"Python: {sys.version.split()[0]}")
    checks = [
        ("Pillow", "PIL"),
        ("pyautogui", "pyautogui"),
        ("pytesseract", "pytesseract"),
        ("paddleocr", "paddleocr"),
    ]
    for label, module_name in checks:
        try:
            __import__(module_name)
            print(f"[OK] {label}")
        except ImportError:
            print(f"[缺少] {label}")

    try:
        import pytesseract  # type: ignore

        tesseract_cmd = find_tesseract_cmd()
        tessdata_dir = resolve_tessdata_dir()
        if tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
            print(f"Tesseract 程序：{tesseract_cmd}")
        if tessdata_dir:
            print(f"Tesseract 语言包目录：{tessdata_dir}")
        print(f"Tesseract: {pytesseract.get_tesseract_version()}")
        try:
            langs = list_tesseract_languages(tesseract_cmd, tessdata_dir)
            print("Tesseract 语言包：" + ", ".join(langs))
            if "chi_sim" not in langs:
                print("提示：未发现 chi_sim，中文识别效果会很差。")
        except Exception as exc:
            print(f"无法读取 Tesseract 语言包：{exc}")
    except Exception as exc:
        print(f"Tesseract 不可用：{exc}")


def run_ocr_image(args: argparse.Namespace) -> None:
    from PIL import Image  # type: ignore

    cfg = apply_cli_overrides(load_config(Path(args.config)), args)
    backend = make_backend(cfg)
    image = Image.open(args.image)
    lines = backend.read_lines(image)
    for line in lines:
        print(f"{line.confidence:5.1f} ({line.x1},{line.y1},{line.x2},{line.y2}) {line.text}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="自动滚动并 OCR 导出聊天记录为 TXT。")
    parser.add_argument("--config", default=CONFIG_FILE, help="配置文件路径，默认 config.json")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common_options(target: argparse.ArgumentParser) -> None:
        target.add_argument("--self-name", help="右侧消息的发送者名称，默认：我")
        target.add_argument("--other-name", help="左侧消息的发送者名称，默认：对方")
        target.add_argument("--recognition-mode", choices=["ocr", "ai"], help="识别模式：ocr=本机OCR，ai=AI识别(GPT)")
        target.add_argument("--backend", choices=["auto", "tesseract", "paddle"], help="OCR 后端")
        target.add_argument("--tesseract-cmd", help="tesseract.exe 路径，通常不需要手动填")
        target.add_argument("--tessdata-dir", help="Tesseract 语言包目录，默认使用工具目录下的 tessdata")
        target.add_argument("--tesseract-lang", help="Tesseract 语言，例如 chi_sim+eng")
        target.add_argument("--ai-model", help="AI识别使用的 OpenAI 模型，默认 gpt-4.1-mini")
        target.add_argument("--ai-base-url", help="AI API 基础地址，例如 https://api.openai.com/v1 或国内兼容接口地址")
        target.add_argument("--ai-api-format", choices=["responses", "chat_completions"], help="AI API 格式：responses 或 chat_completions")
        target.add_argument("--ai-image-detail", choices=["low", "high", "auto"], help="发送给 AI 的图片细节等级")
        target.add_argument("--ai-timeout", type=float, help="AI 请求超时时间，单位秒")
        target.add_argument("--ai-pipeline", action="store_true", default=None, help="AI模式先快速截屏再并发识别")
        target.add_argument("--no-ai-pipeline", dest="ai_pipeline", action="store_false", default=None, help="关闭AI流水线加速")
        target.add_argument("--ai-concurrency", type=int, help="AI并发识别请求数，默认 3")
        target.add_argument("--ai-batch-size", type=int, help="每次AI请求合并识别几张截图，默认 3")
        target.add_argument("--ai-capture-delay", type=float, help="AI流水线截屏翻页后的等待秒数")
        target.add_argument("--ai-image-max-width", type=int, help="发给AI前把截图压缩到的最大宽度，默认 1280")
        target.add_argument("--ai-image-quality", type=int, help="发给AI的JPEG质量，默认 82")
        target.add_argument("--max-page-errors", type=int, help="连续多少个批次识别异常后停止，默认 20")
        target.add_argument("--output-dir", help="导出目录")
        target.add_argument("--direction", choices=["up", "down"], help="滚动方向，up=向上翻旧记录")
        target.add_argument("--max-pages", type=int, help="最多扫描多少屏")
        target.add_argument("--speed-preset", choices=list(SPEED_PRESETS.keys()), help="速度预设：稳定、快速、极速")
        target.add_argument("--scroll-mode", choices=["page", "wheel"], help="滚动方式：page=整屏翻页，wheel=滚轮滚动")
        target.add_argument("--page-overlap-clicks", type=int, help="整屏翻页后反向补一点重叠，减少交界处漏消息，默认 3")
        target.add_argument("--scroll-clicks", type=int, help="每次滚轮格数")
        target.add_argument("--page-delay", type=float, help="每次滚动后的等待秒数")
        target.add_argument("--start-delay", type=float, help="开始前等待秒数")
        target.add_argument("--stable-stop-pages", type=int, help="连续多少页画面不变后停止")
        target.add_argument("--show-start-dialog", dest="show_start_dialog", action="store_true", default=None, help="运行前显示名字确认窗口")
        target.add_argument("--hide-start-dialog", dest="show_start_dialog", action="store_false", default=None, help="以后运行时不显示名字确认窗口")
        target.add_argument("--min-confidence", type=float, help="OCR 最低置信度")
        target.add_argument("--ocr-scale", type=float, help="Tesseract 识别前放大倍率")

    calibrate = subparsers.add_parser("calibrate", help="框选聊天区域并保存 config.json")
    add_common_options(calibrate)
    calibrate.set_defaults(func=run_calibrate)

    run = subparsers.add_parser("run", help="开始自动滚动 OCR 导出")
    add_common_options(run)
    run.add_argument("--select-region", action="store_true", help="运行前重新框选聊天区域")
    run.add_argument("--no-start-dialog", action="store_true", help="本次运行跳过名字确认窗口")
    run.set_defaults(func=run_export)

    doctor = subparsers.add_parser("doctor", help="检查依赖和 OCR 环境")
    doctor.set_defaults(func=run_doctor)

    ocr_image = subparsers.add_parser("ocr-image", help="调试：识别一张本地图片")
    add_common_options(ocr_image)
    ocr_image.add_argument("image", help="图片路径")
    ocr_image.set_defaults(func=run_ocr_image)

    return parser


def main() -> None:
    configure_stdio()
    if len(sys.argv) == 1:
        sys.argv.append("run")
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
