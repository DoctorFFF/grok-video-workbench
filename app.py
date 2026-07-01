from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from fastapi import BackgroundTasks, Body, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("WORKBENCH_DATA_DIR", str(ROOT / "data"))).resolve()
VIDEOS_DIR = Path(os.getenv("WORKBENCH_VIDEOS_DIR", str(ROOT / "videos"))).resolve()
STATIC_DIR = ROOT / "static"
TOOLS_DIR = ROOT / "tools"
SETTINGS_PATH = DATA_DIR / "settings.json"
TASKS_PATH = DATA_DIR / "tasks.json"
AUTH_PATH = DATA_DIR / "auth.json"
LOCAL_MAX_DOWNLOAD_THREADS = max(1, os.cpu_count() or 1)
RECOMMENDED_DOWNLOAD_THREADS = max(1, int(LOCAL_MAX_DOWNLOAD_THREADS * 0.75))

for folder in (DATA_DIR, VIDEOS_DIR, STATIC_DIR, TOOLS_DIR):
    folder.mkdir(parents=True, exist_ok=True)

DEFAULT_SETTINGS = {
    "cpa_base_url": os.getenv("CPA_BASE_URL", ""),
    "cpa_api_key": os.getenv("CPA_API_KEY", ""),
    "cpa_management_key": os.getenv("CPA_MANAGEMENT_KEY", ""),
    "image_host_base_url": os.getenv("IMAGE_HOST_BASE_URL", "https://img.remit.ee"),
    "image_host_selected_url": os.getenv("IMAGE_HOST_SELECTED_URL", os.getenv("IMAGE_HOST_BASE_URL", "https://img.remit.ee")),
    "image_host_options": [item.strip() for item in os.getenv("IMAGE_HOST_OPTIONS", "https://img.remit.ee").split(",") if item.strip()],
    "image_host_upload_path": os.getenv("IMAGE_HOST_UPLOAD_PATH", "/api/upload"),
    "poll_interval_seconds": int(os.getenv("POLL_INTERVAL_SECONDS", "5")),
    "max_parallel_generations": 8,
    "download_parallelism": 4,
    "download_thread_count": RECOMMENDED_DOWNLOAD_THREADS,
}

VIDEO_MODELS = [
    {
        "id": "grok-imagine-video",
        "label": "Grok Imagine Video",
        "text": True,
        "image": True,
        "resolutions": ["480p", "720p"],
    },
    {
        "id": "grok-imagine-video-1.5-preview",
        "label": "Grok Imagine Video 1.5 Preview",
        "text": False,
        "image": True,
        "resolutions": ["480p", "720p", "1080p"],
    },
]

RESOLUTIONS = ["480p", "720p"]
IMAGE_RESOLUTIONS = ["480p", "720p", "1080p"]
ASPECT_RATIOS = ["16:9", "9:16", "1:1", "4:3", "3:4", "3:2", "2:3"]
DURATIONS = list(range(1, 16))
MAX_REFERENCE_IMAGES = 7
MAX_REFERENCE_DURATION = 10
GENERATION_START_ATTEMPTS = 3
GENERATION_POLL_ERROR_RETRIES = 8
GENERATION_RETRY_BASE_SECONDS = 3
ALLOWED_UPLOAD_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv"}
ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
MAX_IMAGE_UPLOAD_BYTES = 25 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 64 * 1024
AUTH_COOKIE_NAME = "gvw_auth"
AUTH_COOKIE_MAX_AGE_SECONDS = 12 * 60 * 60
AUTH_HASH_ITERATIONS = 600_000
AUTH_MIN_KEY_LENGTH = 12
AUTH_FAILURE_WINDOW_SECONDS = 5 * 60
AUTH_MAX_FAILURES = 8
AUTH_PUBLIC_PATHS = {
    "/",
    "/favicon.ico",
    "/api/auth/status",
    "/api/auth/setup",
    "/api/auth/login",
    "/api/auth/logout",
}
TEST_IMAGE_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6360000002000100ffff03000006000557bfab3d00000000"
    "49454e44ae426082"
)

json_lock = asyncio.Lock()
generation_semaphore = asyncio.Semaphore(DEFAULT_SETTINGS["max_parallel_generations"])
download_semaphore = asyncio.Semaphore(DEFAULT_SETTINGS["download_parallelism"])
running_scene_jobs: set[str] = set()
BUSY_STATUSES = {"queued", "submitting", "polling"}
auth_file_lock = threading.Lock()
auth_failure_lock = threading.Lock()
auth_failures: dict[str, list[float]] = {}

ffmpeg_state = {
    "status": "unknown",
    "ffmpeg": "",
    "ffprobe": "",
    "message": "",
    "download_url": "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
}
ffmpeg_download_in_progress = False

app = FastAPI(title="Grok Video Workbench", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class SceneInput(BaseModel):
    prompt: str = Field(default="")
    image_url: str = Field(default="")
    model: str | None = None
    duration: int | None = None
    resolution: str | None = None
    aspect_ratio: str | None = None


class TaskInput(BaseModel):
    name: str = ""
    global_params: dict[str, Any] = Field(default_factory=dict)


class SubmitInput(BaseModel):
    scene_ids: list[int] | None = None
    submit_interval_seconds: float | None = None


class DownloadInput(BaseModel):
    scene_ids: list[int] | None = None
    thread_count: int | None = None


class MergeInput(BaseModel):
    scene_ids: list[int]
    normalize: bool = False
    resolution: str = "720p"
    aspect_ratio: str = "16:9"
    output_name: str = ""


class ManualUrlInput(BaseModel):
    video_url: str


class SettingsInput(BaseModel):
    cpa_base_url: str
    cpa_api_key: str
    cpa_management_key: str
    image_host_base_url: str = "https://img.remit.ee"
    image_host_selected_url: str = "https://img.remit.ee"
    image_host_options: list[str] = Field(default_factory=lambda: ["https://img.remit.ee"])
    image_host_upload_path: str = "/api/upload"
    poll_interval_seconds: int = 5
    download_thread_count: int | None = None


class GlobalParamsInput(BaseModel):
    model: str = "grok-imagine-video"
    duration: int = 8
    resolution: str = "720p"
    aspect_ratio: str = "16:9"
    submit_interval_seconds: float = 5


class DisabledInput(BaseModel):
    disabled: bool


class SceneIdInput(BaseModel):
    id: int


class ReorderScenesInput(BaseModel):
    scene_ids: list[int]


class ImageHostTestInput(BaseModel):
    image_host_url: str = ""


class AuthSetupInput(BaseModel):
    key: str = Field(default="")
    confirm_key: str = Field(default="")


class AuthLoginInput(BaseModel):
    key: str = Field(default="")


@dataclass
class ApiFailure(Exception):
    message: str
    status_code: int | None = None
    data: dict[str, Any] | None = None
    raw: str = ""

    def __str__(self) -> str:
        return self.message


def now_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ts_to_epoch(value: Any) -> float | None:
    if not value:
        return None
    text = str(value).replace("T", " ").split(".")[0].strip()
    for fmt, length in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return time.mktime(time.strptime(text[:length], fmt))
        except ValueError:
            pass
    return None


def within_hours(value: Any, hours: int = 24) -> bool:
    epoch = ts_to_epoch(value)
    return epoch is not None and 0 <= time.time() - epoch <= hours * 3600


def slug(value: str, fallback: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text)
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"_+", "_", text)[:120].strip("._- ")
    return text or fallback


def strip_video_extension(value: str) -> str:
    text = str(value or "").strip()
    if text.lower().endswith(".mp4"):
        return text[:-4]
    return text


def append_final_label(value: str) -> str:
    base = strip_video_extension(value).strip()
    return base if re.search(r"(^|[._\-\s])final$", base, flags=re.IGNORECASE) else f"{base}_final"


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        backup = path.with_suffix(path.suffix + f".broken-{int(time.time())}")
        shutil.copy2(path, backup)
        return default


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(value: Any) -> bytes:
    text = str(value or "")
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode((text + padding).encode("ascii"))


def load_auth_config() -> tuple[dict[str, Any], str]:
    if not AUTH_PATH.exists():
        return {}, ""
    try:
        data = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}, "认证配置读取失败，服务已拒绝开放。请检查 data/auth.json。"
    if not isinstance(data, dict):
        return {}, "认证配置格式无效，服务已拒绝开放。"
    required = {"version", "password_salt", "password_hash", "hash_iterations", "cookie_secret"}
    if data.get("version") != 1 or not required.issubset(data):
        return {}, "认证配置不完整，服务已拒绝开放。"
    return data, ""


def write_auth_config(data: dict[str, Any]) -> None:
    write_json_atomic(AUTH_PATH, data)
    try:
        os.chmod(AUTH_PATH, 0o600)
    except OSError:
        pass


def validate_auth_key(key: str, confirm_key: str | None = None) -> str:
    if len(key) < AUTH_MIN_KEY_LENGTH:
        return f"授权密钥至少需要 {AUTH_MIN_KEY_LENGTH} 个字符。"
    if key != key.strip():
        return "授权密钥首尾不能包含空格。"
    if confirm_key is not None and key != confirm_key:
        return "两次输入的授权密钥不一致。"
    return ""


def hash_auth_key(key: str, salt: bytes, iterations: int = AUTH_HASH_ITERATIONS) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", key.encode("utf-8"), salt, iterations)
    return b64url_encode(digest)


def create_auth_config(key: str) -> dict[str, Any]:
    salt = secrets.token_bytes(32)
    return {
        "version": 1,
        "password_salt": b64url_encode(salt),
        "password_hash": hash_auth_key(key, salt),
        "hash_iterations": AUTH_HASH_ITERATIONS,
        "cookie_secret": secrets.token_urlsafe(48),
        "created_at": now_ts(),
    }


def ensure_env_auth_config() -> None:
    key = os.getenv("WORKBENCH_AUTH_KEY", "")
    if not key:
        return
    config, error = load_auth_config()
    if error:
        raise RuntimeError(error)
    if config:
        return
    validation_error = validate_auth_key(key)
    if validation_error:
        raise RuntimeError(f"WORKBENCH_AUTH_KEY 无效：{validation_error}")
    with auth_file_lock:
        config, error = load_auth_config()
        if error:
            raise RuntimeError(error)
        if not config:
            write_auth_config(create_auth_config(key))


def verify_auth_key(key: str, config: dict[str, Any]) -> bool:
    try:
        salt = b64url_decode(config["password_salt"])
        expected = str(config["password_hash"])
        iterations = int(config["hash_iterations"])
    except Exception:
        return False
    candidate = hash_auth_key(key, salt, iterations)
    return hmac.compare_digest(candidate, expected)


def sign_auth_cookie_body(body: str, config: dict[str, Any]) -> str:
    secret = str(config["cookie_secret"]).encode("utf-8")
    digest = hmac.new(secret, body.encode("utf-8"), hashlib.sha256).digest()
    return b64url_encode(digest)


def create_auth_cookie_value(config: dict[str, Any]) -> str:
    issued_at = int(time.time())
    nonce = secrets.token_urlsafe(32)
    body = f"{issued_at}.{nonce}"
    return f"{body}.{sign_auth_cookie_body(body, config)}"


def auth_cookie_valid(value: str | None, config: dict[str, Any]) -> bool:
    if not value:
        return False
    parts = value.split(".")
    if len(parts) != 3:
        return False
    issued_text, nonce, signature = parts
    if len(nonce) < 32:
        return False
    try:
        issued_at = int(issued_text)
    except ValueError:
        return False
    age = time.time() - issued_at
    if age < -30 or age > AUTH_COOKIE_MAX_AGE_SECONDS:
        return False
    body = f"{issued_text}.{nonce}"
    expected = sign_auth_cookie_body(body, config)
    return hmac.compare_digest(signature, expected)


def request_is_authenticated(request: Request, config: dict[str, Any] | None = None) -> bool:
    if config is None:
        config, error = load_auth_config()
        if error or not config:
            return False
    return auth_cookie_valid(request.cookies.get(AUTH_COOKIE_NAME), config)


def should_use_secure_cookie(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    host = request.headers.get("host", "").split(":")[0].lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return False
    return request.url.scheme == "https" or forwarded_proto == "https" or host.endswith(".hf.space")


def auth_cookie_samesite(request: Request) -> str:
    host = request.headers.get("host", "").split(":")[0].lower()
    if host.endswith(".hf.space") and should_use_secure_cookie(request):
        return "none"
    return "lax"


def request_origin_allowed(request: Request) -> bool:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return True
    origin = request.headers.get("origin")
    if not origin:
        return True
    parsed_origin = urllib.parse.urlparse(origin)
    origin_host = (parsed_origin.hostname or "").lower()
    request_host = request.headers.get("host", "").split(":")[0].lower()
    if not origin_host or not request_host:
        return False
    return origin_host == request_host


def set_auth_cookie(response: Response, request: Request, config: dict[str, Any]) -> None:
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=create_auth_cookie_value(config),
        max_age=AUTH_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        secure=should_use_secure_cookie(request),
        samesite=auth_cookie_samesite(request),
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"


def clear_auth_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        key=AUTH_COOKIE_NAME,
        httponly=True,
        secure=should_use_secure_cookie(request),
        samesite=auth_cookie_samesite(request),
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"


def auth_client_id(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if forwarded_for:
        return forwarded_for
    return request.client.host if request.client else "unknown"


def auth_rate_limited(request: Request) -> bool:
    now = time.time()
    client_id = auth_client_id(request)
    with auth_failure_lock:
        recent = [ts for ts in auth_failures.get(client_id, []) if now - ts <= AUTH_FAILURE_WINDOW_SECONDS]
        auth_failures[client_id] = recent
        return len(recent) >= AUTH_MAX_FAILURES


def record_auth_failure(request: Request) -> None:
    now = time.time()
    client_id = auth_client_id(request)
    with auth_failure_lock:
        recent = [ts for ts in auth_failures.get(client_id, []) if now - ts <= AUTH_FAILURE_WINDOW_SECONDS]
        recent.append(now)
        auth_failures[client_id] = recent


def clear_auth_failures(request: Request) -> None:
    client_id = auth_client_id(request)
    with auth_failure_lock:
        auth_failures.pop(client_id, None)


def auth_public_path(path: str) -> bool:
    return path in AUTH_PUBLIC_PATHS or path.startswith("/static/")


def auth_block_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": {"code": code, "message": message}},
        headers={"Cache-Control": "no-store"},
    )


def recover_interrupted_jobs() -> None:
    data = tasks_data()
    changed = False
    for task in data.get("tasks", []):
        for scene in task.get("scenes", []):
            if scene.get("status") in {"queued", "submitting", "polling"}:
                scene["status"] = "failed"
                scene["error"] = {
                    "type": "interrupted",
                    "label": "服务中断",
                    "zh": "上次服务在生成过程中停止。没有确认最终结果，请重新执行该分镜；如果你已有视频 URL，可手动填入。",
                    "raw": "Recovered on startup",
                }
                scene["updated_at"] = now_ts()
                changed = True
            if scene.get("download_status") == "downloading":
                scene["download_status"] = "failed"
                scene["download_error"] = "上次服务在下载过程中停止。远程视频 URL 已保留，可重试下载或手动下载。"
                scene["updated_at"] = now_ts()
                changed = True
    if changed:
        write_json_atomic(TASKS_PATH, data)


def ensure_settings_file() -> dict[str, Any]:
    data = normalize_image_host_settings(DEFAULT_SETTINGS | read_json(SETTINGS_PATH, {}))
    write_json_atomic(SETTINGS_PATH, data)
    return data


def settings() -> dict[str, Any]:
    return normalize_image_host_settings(DEFAULT_SETTINGS | read_json(SETTINGS_PATH, {}))


def public_settings() -> dict[str, Any]:
    data = settings()
    return {k: v for k, v in data.items() if k not in {"cpa_base_url", "cpa_api_key", "cpa_management_key"}}


def api_base() -> str:
    return settings()["cpa_base_url"].rstrip("/") + "/v1"


def management_base() -> str:
    return settings()["cpa_base_url"].rstrip("/") + "/v0/management"


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings()['cpa_api_key']}", "Content-Type": "application/json"}


def management_headers() -> dict[str, str]:
    key = settings()["cpa_management_key"]
    return {"Authorization": f"Bearer {key}", "X-Management-Key": key}


def normalize_https_base_url(value: Any, fallback: str = "https://img.remit.ee") -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        text = fallback
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme != "https" or not parsed.netloc:
        raise HTTPException(status_code=400, detail="图床地址必须是完整 HTTPS 地址，例如 https://img.remit.ee")
    return f"https://{parsed.netloc}{parsed.path.rstrip('/')}"


def normalize_image_host_settings(raw: dict[str, Any]) -> dict[str, Any]:
    data = dict(raw)
    base_url = normalize_https_base_url(data.get("image_host_base_url", DEFAULT_SETTINGS["image_host_base_url"]))
    selected_url = normalize_https_base_url(data.get("image_host_selected_url") or base_url, base_url)
    options: list[str] = []
    for value in [base_url, selected_url, *list(data.get("image_host_options") or [])]:
        try:
            url = normalize_https_base_url(value, base_url)
        except HTTPException:
            continue
        if url not in options:
            options.append(url)
    upload_path = str(data.get("image_host_upload_path") or DEFAULT_SETTINGS["image_host_upload_path"]).strip()
    if not upload_path.startswith("/"):
        upload_path = "/" + upload_path
    data["image_host_base_url"] = base_url
    data["image_host_selected_url"] = selected_url if selected_url in options else base_url
    data["image_host_options"] = options or [base_url]
    data["image_host_upload_path"] = upload_path
    data["download_thread_count"] = normalize_download_thread_count(data.get("download_thread_count"), fallback=RECOMMENDED_DOWNLOAD_THREADS)
    return data


def normalize_download_thread_count(value: Any = None, *, fallback: int | None = None) -> int:
    if fallback is None:
        fallback = RECOMMENDED_DOWNLOAD_THREADS
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = int(fallback)
    return max(1, min(LOCAL_MAX_DOWNLOAD_THREADS, count))


def download_thread_info(default_value: Any = None) -> dict[str, int]:
    return {
        "max": LOCAL_MAX_DOWNLOAD_THREADS,
        "recommended": RECOMMENDED_DOWNLOAD_THREADS,
        "default": normalize_download_thread_count(default_value),
    }


def selected_download_threads(thread_count: int | None = None) -> int:
    if thread_count is not None:
        return normalize_download_thread_count(thread_count)
    return normalize_download_thread_count(settings().get("download_thread_count"), fallback=RECOMMENDED_DOWNLOAD_THREADS)


def image_host_upload_url(base_url: str) -> str:
    upload_path = str(settings().get("image_host_upload_path") or "/api/upload").strip()
    if not upload_path.startswith("/"):
        upload_path = "/" + upload_path
    return normalize_https_base_url(base_url) + upload_path


def public_image_url(base_url: str, response_url: str) -> str:
    url = str(response_url or "").strip()
    if not url:
        raise HTTPException(status_code=502, detail="图床上传成功但响应中没有 url 字段。")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in {"http", "https"}:
        return url
    if not url.startswith("/"):
        url = "/" + url
    return normalize_https_base_url(base_url) + url


def tasks_data() -> dict[str, Any]:
    return read_json(TASKS_PATH, {"tasks": []})


async def save_tasks(data: dict[str, Any]) -> None:
    async with json_lock:
        write_json_atomic(TASKS_PATH, data)


def find_task(data: dict[str, Any], task_id: str) -> dict[str, Any]:
    for task in data["tasks"]:
        if task["id"] == task_id:
            return task
    raise HTTPException(status_code=404, detail="任务不存在")


def find_scene(task: dict[str, Any], scene_id: int) -> dict[str, Any]:
    for scene in task["scenes"]:
        if scene["id"] == scene_id:
            return scene
    raise HTTPException(status_code=404, detail="分镜不存在")


def validate_scene_ids(task: dict[str, Any], scene_ids: list[int]) -> None:
    existing = {scene["id"] for scene in task.get("scenes", [])}
    missing = [scene_id for scene_id in scene_ids if scene_id not in existing]
    if missing:
        raise HTTPException(status_code=404, detail=f"分镜不存在：{', '.join(map(str, missing))}")


def scene_busy(scene: dict[str, Any]) -> bool:
    return scene.get("status") in BUSY_STATUSES or scene.get("download_status") == "downloading"


def reject_if_scene_busy(scene: dict[str, Any]) -> None:
    if scene_busy(scene):
        raise HTTPException(status_code=409, detail=f"分镜 {scene['id']} 正在生成或下载，请完成后再操作。")


def archive_scene_result(scene: dict[str, Any], reason: str) -> None:
    if not any(scene.get(key) for key in ("request_id", "video_url", "local_video", "local_video_url")):
        return
    history = scene.setdefault("previous_results", [])
    history.append({
        "archived_at": now_ts(),
        "reason": reason,
        "status": scene.get("status", ""),
        "request_id": scene.get("request_id", ""),
        "video_url": scene.get("video_url", ""),
        "local_video": scene.get("local_video", ""),
        "local_video_url": scene.get("local_video_url", ""),
        "manual_upload": bool(scene.get("manual_upload")),
        "manual_url": bool(scene.get("manual_url")),
    })
    del history[:-5]


def clear_scene_result(scene: dict[str, Any], reason: str) -> None:
    archive_scene_result(scene, reason)
    scene.update({
        "status": "draft",
        "progress": 0,
        "request_id": "",
        "video_url": "",
        "local_video": "",
        "local_video_url": "",
        "download_status": "not_downloaded",
        "download_error": "",
        "download_progress": 0,
        "download_bytes_done": 0,
        "download_bytes_total": 0,
        "download_attempt": 0,
        "error": None,
        "manual_upload": False,
        "manual_url": False,
    })
    for key in (
        "raw_start",
        "raw_result",
        "finished_at",
        "execution_started_at",
        "request_id_created_at",
        "video_url_created_at",
        "manual_url_created_at",
        "manual_uploaded_at",
        "upload_original_name",
        "uploaded_video_spec",
        "download_started_at",
        "download_finished_at",
    ):
        scene.pop(key, None)


def scene_request_time(scene: dict[str, Any]) -> str:
    return scene.get("request_id_created_at") or scene.get("started_at") or scene.get("updated_at") or ""


def scene_download_time(scene: dict[str, Any]) -> str:
    if scene.get("manual_url") and not scene.get("request_id"):
        return scene.get("manual_url_created_at") or scene.get("updated_at") or ""
    return (
        scene.get("execution_started_at")
        or scene.get("request_id_created_at")
        or scene.get("started_at")
        or scene.get("video_url_created_at")
        or scene.get("finished_at")
        or ""
    )


def scene_request_fresh(scene: dict[str, Any]) -> bool:
    return bool(scene.get("request_id")) and within_hours(scene_request_time(scene), 24)


def scene_video_url_fresh(scene: dict[str, Any]) -> bool:
    return bool(scene.get("video_url")) and within_hours(scene_download_time(scene), 24)


def delete_scene_local_video(scene: dict[str, Any]) -> list[str]:
    deleted: list[str] = []
    for value in (str(scene.get("local_video") or ""),):
        if not value:
            continue
        path = (ROOT / value).resolve()
        if ROOT.resolve() in path.parents and path.exists() and path.is_file():
            path.unlink()
            deleted.append(path.name)
    return deleted


def delete_scene_related_videos(scene: dict[str, Any]) -> list[str]:
    values = [str(scene.get("local_video") or "")]
    values.extend(str(item.get("local_video") or "") for item in scene.get("previous_results", []) if isinstance(item, dict))
    deleted: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        path = (ROOT / value).resolve()
        if ROOT.resolve() in path.parents and path.exists() and path.is_file():
            path.unlink()
            deleted.append(path.name)
    return deleted


def scene_dir(task_id: str) -> Path:
    path = VIDEOS_DIR / slug(task_id, "task")
    path.mkdir(parents=True, exist_ok=True)
    return path


def public_video_url(task_id: str, filename: str) -> str:
    return f"/api/tasks/{urllib.parse.quote(task_id)}/files/{urllib.parse.quote(filename)}"


def scene_video_filename(task: dict[str, Any], scene_id: int) -> str:
    name_base = slug(task.get("name", task.get("id", "")), task.get("id", "task"))
    return f"{name_base}_scene_{scene_id:03d}.mp4"


def default_merge_name(task: dict[str, Any]) -> str:
    task_id = task.get("id", "task")
    name = str(task.get("name") or "").strip()
    base = name if name and name != task_id else task_id
    return slug(append_final_label(base), f"{task_id}_final")


def unique_video_stem(folder: Path, desired_stem: str, existing_names: set[str] | None = None) -> str:
    existing_names = existing_names or set()
    base = slug(strip_video_extension(desired_stem), "final")
    candidate = base
    index = 2
    while (folder / f"{candidate}.mp4").exists() or (folder / f"{candidate}.mp4").name in existing_names:
        candidate = f"{base}_{index}"
        index += 1
    return candidate


def repair_legacy_merge_names() -> None:
    data = tasks_data()
    changed = False
    for task in data.get("tasks", []):
        task_id = str(task.get("id") or "")
        task_name = str(task.get("name") or "").strip()
        if not task_id or not task_name or task_name == task_id:
            continue
        task_folder = (VIDEOS_DIR / slug(task_id, "task")).resolve()
        if not task_folder.exists():
            continue
        for merge in task.get("merges", []):
            old_file = Path(str(merge.get("filename") or merge.get("file") or "")).name
            old_stem = strip_video_extension(old_file)
            if old_stem != f"{task_id}_final":
                continue
            file_value = str(merge.get("file") or "")
            current_path = (ROOT / file_value).resolve() if file_value else task_folder / old_file
            if current_path.parent != task_folder or not current_path.exists():
                continue
            existing_names = {
                Path(str(item.get("filename") or item.get("file") or "")).name
                for item in task.get("merges", [])
                if item is not merge
            }
            new_stem = unique_video_stem(task_folder, default_merge_name(task), existing_names)
            new_path = task_folder / f"{new_stem}.mp4"
            if new_path == current_path:
                continue
            current_path.rename(new_path)
            video_url = public_video_url(task_id, new_path.name)
            merge.update({
                "file": str(new_path.relative_to(ROOT)).replace("\\", "/"),
                "filename": new_path.name,
                "url": video_url,
                "download_url": f"{video_url}?download=1",
            })
            merge.setdefault("params", {})["output_name"] = new_stem
            changed = True
    if changed:
        write_json_atomic(TASKS_PATH, data)


def resolve_model(requested_model: str, image_url: str, image_count: int = 0) -> tuple[str, str]:
    requested_model = requested_model or "grok-imagine-video"
    caps = {m["id"]: m for m in VIDEO_MODELS}
    if requested_model not in caps:
        return "grok-imagine-video", "所选模型不在当前支持列表中，已切换到 Grok Imagine Video。"
    if image_count > 1:
        return "grok-imagine-video", f"检测到多张图片，已切换到 Reference-to-Video 模式；这些图片会作为 reference_images 一起提交，最多 {MAX_REFERENCE_IMAGES} 张。"
    if image_url:
        if caps.get(requested_model, {}).get("image"):
            return requested_model, "用户选择的模型支持图生视频，保持不变。"
        return "grok-imagine-video", "检测到图片 URL，已自动切换到支持图片输入的模型。"
    if caps.get(requested_model, {}).get("text"):
        return requested_model, "用户选择的模型支持文生视频，保持不变。"
    return "grok-imagine-video", "未提供图片 URL，已自动切换到文生视频模型。"


def parse_image_urls(value: Any) -> list[str]:
    parts = re.split(r"[\r\n,，]+", str(value or ""))
    urls: list[str] = []
    for part in parts:
        url = part.strip()
        if not url or url in urls:
            continue
        urls.append(url)
    return urls


def validate_scene_image_urls(value: Any) -> list[str]:
    image_urls = parse_image_urls(value)
    if len(image_urls) > MAX_REFERENCE_IMAGES:
        raise HTTPException(status_code=400, detail=f"只能上传 {MAX_REFERENCE_IMAGES} 个，请先删除部分图片再上传")
    return image_urls


def normalize_duration(value: Any) -> int:
    try:
        duration = int(value)
    except (TypeError, ValueError):
        return 8
    return duration if duration in DURATIONS else 8


def normalize_aspect_ratio(value: Any) -> str:
    return value if value in ASPECT_RATIOS else "16:9"


def normalize_resolution(model: str, value: Any) -> str:
    caps = {m["id"]: m for m in VIDEO_MODELS}
    allowed = caps.get(model, {}).get("resolutions") or RESOLUTIONS
    if value in allowed:
        return value
    return "720p" if "720p" in allowed else allowed[0]


def even_dimension(value: float) -> int:
    number = max(2, int(round(value)))
    return number if number % 2 == 0 else number + 1


def merge_dimensions(resolution: str, aspect_ratio: str) -> tuple[int, int]:
    base = {"480p": 480, "720p": 720, "1080p": 1080}.get(resolution, 720)
    ratios = {
        "16:9": (16, 9),
        "9:16": (9, 16),
        "1:1": (1, 1),
        "4:3": (4, 3),
        "3:4": (3, 4),
        "3:2": (3, 2),
        "2:3": (2, 3),
    }
    width_ratio, height_ratio = ratios.get(aspect_ratio, (16, 9))
    if width_ratio >= height_ratio:
        return even_dimension(base * width_ratio / height_ratio), base
    return base, even_dimension(base * height_ratio / width_ratio)


def scene_effective_params(task: dict[str, Any], scene: dict[str, Any]) -> dict[str, Any]:
    global_params = task.get("global_params", {})
    params = scene.get("params", {})
    image_urls = validate_scene_image_urls(scene.get("image_url", ""))
    image_url = image_urls[0] if image_urls else ""
    requested_model = params.get("model") or global_params.get("model") or "grok-imagine-video"
    model, model_note = resolve_model(requested_model, image_url, len(image_urls))
    resolution = normalize_resolution(model, params.get("resolution") or global_params.get("resolution") or "720p")
    duration = normalize_duration(params.get("duration") or global_params.get("duration") or 8)
    if len(image_urls) > 1 and duration > MAX_REFERENCE_DURATION:
        duration = MAX_REFERENCE_DURATION
        model_note = f"{model_note} Reference-to-Video 最长支持 {MAX_REFERENCE_DURATION}s，已自动按 {MAX_REFERENCE_DURATION}s 提交。"
    prompt = scene.get("prompt", "").strip()
    return {
        "model": model,
        "requested_model": requested_model,
        "model_note": model_note,
        "prompt": prompt.strip(),
        "image_url": image_url,
        "image_urls": image_urls,
        "reference_images": [{"url": url} for url in image_urls],
        "duration": duration,
        "resolution": resolution,
        "aspect_ratio": normalize_aspect_ratio(params.get("aspect_ratio") or global_params.get("aspect_ratio") or "16:9"),
    }


def build_generation_payload(params: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "model": params["model"],
        "prompt": params["prompt"],
        "duration": params["duration"],
        "aspect_ratio": params["aspect_ratio"],
        "resolution": params["resolution"],
    }
    image_urls = params.get("image_urls", [])
    if len(image_urls) > 1:
        payload["reference_images"] = params["reference_images"]
    elif params["image_url"]:
        payload["image"] = {"url": params["image_url"]}
    return payload


def empty_prompt_error() -> dict[str, str]:
    return {
        "type": "invalid",
        "label": "参数问题",
        "zh": "提示词不能为空。请先填写并保存分镜提示词。",
        "raw": "Prompt cannot be empty. Please provide a prompt.",
    }


def video_url_from_result(result: dict[str, Any]) -> str:
    video = result.get("video") if isinstance(result, dict) else None
    if isinstance(video, dict):
        return str(video.get("url") or "")
    return str(result.get("video_url") or result.get("url") or "")


def progress_from_result(result: dict[str, Any]) -> int:
    try:
        return int(result.get("progress", 0) or 0)
    except (TypeError, ValueError):
        return 0


def pending_result_error(result: dict[str, Any]) -> dict[str, Any]:
    status = result.get("status", "pending")
    progress = progress_from_result(result)
    return {
        "type": "timeout",
        "label": "等待中",
        "zh": "上游仍在生成或 CPA 暂未返回视频 URL。已保留任务 ID，可稍后点击“拉取结果”。",
        "raw": result,
        "message": f"status={status} progress={progress}",
    }


def update_scene_from_video_result(scene: dict[str, Any], result: dict[str, Any], *, pending_as_failed: bool = False) -> None:
    status = result.get("status", "pending")
    progress = progress_from_result(result)
    video_url = video_url_from_result(result)
    scene["raw_result"] = result
    if status == "done" and video_url:
        scene.update({
            "status": "succeeded",
            "progress": 100,
            "video_url": video_url,
            "finished_at": now_ts(),
            "error": None,
        })
        return
    if status == "done" and not video_url:
        scene.update({
            "status": "failed",
            "progress": 100,
            "error": {
                "type": "invalid",
                "label": "结果缺失",
                "zh": "上游显示已完成，但没有返回视频 URL。请稍后点击“拉取结果”再试。",
                "raw": result,
            },
        })
        return
    if status in ("failed", "expired"):
        info = classify_error(data=result)
        scene.update({"status": "failed", "progress": progress, "error": info | {"raw": result}})
        return
    scene.update({
        "status": "failed" if pending_as_failed else "polling",
        "progress": progress,
        "error": pending_result_error(result) if pending_as_failed else None,
    })


def classify_error(exc: Exception | ApiFailure | None = None, data: dict[str, Any] | None = None, status_code: int | None = None) -> dict[str, str]:
    raw = ""
    if isinstance(exc, ApiFailure):
        data = data or exc.data or {}
        status_code = status_code or exc.status_code
        raw = exc.raw or exc.message
    elif isinstance(exc, HTTPException):
        status_code = status_code or exc.status_code
        data = data or {"detail": exc.detail}
        raw = str(exc.detail)
    elif exc:
        raw = str(exc)
    if data:
        raw = " ".join(str(data.get(k, "")) for k in ("error", "message", "code", "detail")) + " " + raw
    lower = raw.lower()
    if "content moderation" in lower or "rejected" in lower:
        return {"type": "rejected", "label": "被拒绝", "zh": "生成结果触发内容审核，任务不会再产出视频。"}
    if status_code == 404 or "not found" in lower:
        return {"type": "not_found", "label": "找不到", "zh": "请求 ID、接口路径、账号或模型不存在。"}
    if status_code in (401, 403) or "unauthorized" in lower or "forbidden" in lower or "permission" in lower:
        return {"type": "auth", "label": "认证/权限", "zh": "CPA key、管理密码、xAI 登录状态或账号权限不可用。"}
    if status_code == 429 or "rate limit" in lower or "quota" in lower:
        return {"type": "quota", "label": "限流/额度", "zh": "账号额度、速率限制或冷却状态触发。"}
    if status_code == 400 or "invalid" in lower or "bad request" in lower:
        return {"type": "invalid", "label": "参数问题", "zh": "模型、输入模式或请求字段不兼容。"}
    if isinstance(exc, TimeoutError) or "timed out" in lower or "timeout" in lower:
        return {"type": "timeout", "label": "超时", "zh": "请求超过等待时间，可能是生成、下载、CPA 或上游网络响应过慢。"}
    if "connection" in lower or "network" in lower:
        return {"type": "network", "label": "网络", "zh": "CPA、代理或上游网络连接异常。"}
    return {"type": "unknown", "label": "未知", "zh": "未预料错误，已保留原始返回，可手动处理。"}


def cpa_request(method: str, url: str, *, headers: dict[str, str], json_body: Any = None, timeout: int = 60) -> Any:
    try:
        response = requests.request(method, url, headers=headers, json=json_body, timeout=timeout)
        text = response.text
        if not response.ok:
            data = {}
            try:
                data = response.json()
            except Exception:
                pass
            raise ApiFailure(data.get("error") or data.get("message") or text or response.reason, response.status_code, data, text)
        if not text:
            return {}
        return response.json()
    except requests.RequestException as exc:
        raise ApiFailure(f"Network error: {exc}") from exc


async def call_cpa(method: str, url: str, *, headers: dict[str, str], json_body: Any = None, timeout: int = 60) -> Any:
    return await asyncio.to_thread(cpa_request, method, url, headers=headers, json_body=json_body, timeout=timeout)


def is_xai_auth_file(item: dict[str, Any]) -> bool:
    provider = str(item.get("provider", "")).lower()
    name = str(item.get("name", item.get("id", ""))).lower()
    path = str(item.get("path", "")).lower()
    return provider == "xai" or name.startswith("xai-") or "/xai-" in path.replace("\\", "/")


async def get_xai_auth_files() -> list[dict[str, Any]]:
    data = await call_cpa("GET", f"{management_base()}/auth-files", headers=management_headers(), timeout=30)
    files = data.get("files", []) if isinstance(data, dict) else []
    return [item for item in files if is_xai_auth_file(item)]


async def assert_xai_auth_name(name: str) -> dict[str, Any]:
    safe_name = Path(name).name
    if safe_name != name or not safe_name.endswith(".json"):
        raise HTTPException(status_code=400, detail="非法账号文件名")
    files = await get_xai_auth_files()
    for item in files:
        if item.get("name") == name:
            return item
    raise HTTPException(status_code=403, detail="只允许操作 xAI/Grok 账号，或账号不存在")


async def assert_xai_auth_index(auth_index: str) -> dict[str, Any]:
    files = await get_xai_auth_files()
    for item in files:
        if item.get("auth_index") == auth_index:
            return item
    raise HTTPException(status_code=403, detail="只允许操作 xAI/Grok auth_index，或账号不存在")


async def get_xai_auth_by_index(auth_index: str) -> dict[str, Any]:
    files = await get_xai_auth_files()
    for item in files:
        if str(item.get("auth_index", "")) == str(auth_index):
            return item
    raise HTTPException(status_code=404, detail="xAI/Grok 账号不存在或 auth_index 已变化，请刷新账号列表。")


def xai_cent_value(value: Any) -> int | None:
    if isinstance(value, dict):
        value = value.get("val")
    if value in (None, ""):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def xai_plan_name(monthly_limit_cents: int | None) -> str:
    if monthly_limit_cents == 15000:
        return "SuperGrok"
    if monthly_limit_cents:
        return "xAI 额度套餐"
    return ""


def summarize_xai_billing(api_call_result: dict[str, Any]) -> dict[str, Any]:
    status_code = int(api_call_result.get("status_code") or 0)
    body = api_call_result.get("body") or "{}"
    if status_code < 200 or status_code >= 300:
        raise ApiFailure("xAI billing 查询失败", status_code=status_code, raw=str(body)[:2000])
    try:
        payload = json.loads(body) if isinstance(body, str) else body
    except json.JSONDecodeError as exc:
        raise ApiFailure("xAI billing 返回不是有效 JSON", raw=str(body)[:2000]) from exc
    config = payload.get("config") if isinstance(payload, dict) else None
    if not isinstance(config, dict):
        raise ApiFailure("xAI billing 返回缺少 config 字段", raw=str(payload)[:2000])

    monthly_limit = xai_cent_value(config.get("monthlyLimit", config.get("monthly_limit")))
    used = xai_cent_value(config.get("used"))
    on_demand_cap = xai_cent_value(config.get("onDemandCap", config.get("on_demand_cap")))
    remaining = None
    used_percent = None
    remaining_percent = None
    if monthly_limit is not None and used is not None:
        remaining = max(monthly_limit - used, 0)
        if monthly_limit > 0:
            used_percent = max(0, min(100, round((used / monthly_limit) * 100)))
            remaining_percent = max(0, min(100, round((remaining / monthly_limit) * 100)))

    return {
        "plan": xai_plan_name(monthly_limit),
        "monthly_limit_cents": monthly_limit,
        "used_cents": used,
        "remaining_cents": remaining,
        "on_demand_cap_cents": on_demand_cap,
        "on_demand_enabled": bool(on_demand_cap and on_demand_cap > 0),
        "billing_period_start": config.get("billingPeriodStart") or config.get("billing_period_start") or "",
        "billing_period_end": config.get("billingPeriodEnd") or config.get("billing_period_end") or "",
        "used_percent": used_percent,
        "remaining_percent": remaining_percent,
        "source": "https://cli-chat-proxy.grok.com/v1/billing",
        "refreshed_at": now_ts(),
        "raw_config": config,
    }


async def get_xai_billing(auth_index: str) -> dict[str, Any]:
    await assert_xai_auth_index(auth_index)
    payload = {
        "authIndex": auth_index,
        "method": "GET",
        "url": "https://cli-chat-proxy.grok.com/v1/billing",
        "header": {"Authorization": "Bearer $TOKEN$"},
    }
    data = await call_cpa(
        "POST",
        f"{management_base()}/api-call",
        headers=management_headers() | {"Content-Type": "application/json"},
        json_body=payload,
        timeout=45,
    )
    return summarize_xai_billing(data)


async def download_xai_auth_json(name: str) -> dict[str, Any]:
    await assert_xai_auth_name(name)
    url = f"{management_base()}/auth-files/download?name={urllib.parse.quote(name)}"
    try:
        response = await asyncio.to_thread(requests.get, url, headers=management_headers(), timeout=30)
        if not response.ok:
            raise ApiFailure(response.text, response.status_code, raw=response.text)
        data = response.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="CPA 返回的 xAI 认证文件不是有效 JSON") from exc
    if "xai" not in name.lower() and "xai" not in json.dumps(data).lower():
        raise HTTPException(status_code=403, detail="下载内容未通过 xAI/Grok 二次校验，已拒绝修改")
    return data


async def upload_xai_auth_json(name: str, data: dict[str, Any]) -> dict[str, Any]:
    await assert_xai_auth_name(name)
    if "xai" not in name.lower() and "xai" not in json.dumps(data).lower():
        raise HTTPException(status_code=403, detail="上传内容未通过 xAI/Grok 二次校验，已拒绝修改")
    try:
        response = await asyncio.to_thread(
            requests.post,
            f"{management_base()}/auth-files?name={urllib.parse.quote(name)}",
            headers=management_headers() | {"Content-Type": "application/json"},
            data=json.dumps(data),
            timeout=30,
        )
        if not response.ok:
            raise ApiFailure(response.text, response.status_code, raw=response.text)
        return response.json() if response.text else {"status": "ok"}
    except ApiFailure:
        raise
    except requests.RequestException as exc:
        raise ApiFailure(f"Network error: {exc}") from exc


async def update_scene(task_id: str, scene_id: int, patch: dict[str, Any]) -> dict[str, Any]:
    data = tasks_data()
    task = find_task(data, task_id)
    scene = find_scene(task, scene_id)
    scene.update(patch)
    scene["updated_at"] = now_ts()
    task["updated_at"] = now_ts()
    await save_tasks(data)
    return scene


async def run_scene(task_id: str, scene_id: int) -> None:
    job_key = f"{task_id}:{scene_id}"
    if job_key in running_scene_jobs:
        return
    running_scene_jobs.add(job_key)
    async with generation_semaphore:
        try:
            data = tasks_data()
            task = find_task(data, task_id)
            scene = find_scene(task, scene_id)
            params = scene_effective_params(task, scene)
            if not params["prompt"]:
                scene.update({
                    "status": "failed",
                    "progress": 0,
                    "effective_params": params,
                    "error": empty_prompt_error(),
                    "updated_at": now_ts(),
                })
                task["updated_at"] = now_ts()
                await save_tasks(data)
                return
            started_at = now_ts()
            scene.update({
                "status": "submitting",
                "progress": 0,
                "effective_params": params,
                "error": None,
                "request_id": "",
                "video_url": "",
                "local_video": "",
                "local_video_url": "",
                "download_status": "not_downloaded",
                "download_error": "",
                "download_progress": 0,
                "download_bytes_done": 0,
                "download_bytes_total": 0,
                "download_attempt": 0,
                "manual_upload": False,
                "started_at": started_at,
                "execution_started_at": started_at,
            })
            for key in ("request_id_created_at", "video_url_created_at", "manual_url_created_at", "finished_at", "download_started_at", "download_finished_at"):
                scene.pop(key, None)
            await save_tasks(data)

            payload = build_generation_payload(params)

            start: dict[str, Any] | None = None
            last_start_error: ApiFailure | None = None
            for attempt in range(1, GENERATION_START_ATTEMPTS + 1):
                try:
                    await update_scene(task_id, scene_id, {
                        "status": "submitting",
                        "submit_attempt": attempt,
                        "error": None if attempt == 1 else scene.get("error"),
                    })
                    start = await call_cpa("POST", f"{api_base()}/videos/generations", headers=auth_headers(), json_body=payload, timeout=90)
                    break
                except ApiFailure as exc:
                    last_start_error = exc
                    info = classify_error(exc)
                    retrying = attempt < GENERATION_START_ATTEMPTS
                    await update_scene(task_id, scene_id, {
                        "status": "submitting" if retrying else "failed",
                        "progress": 0,
                        "submit_attempt": attempt,
                        "error": info | {
                            "raw": exc.raw,
                            "message": (
                                f"提交第 {attempt}/{GENERATION_START_ATTEMPTS} 次失败。"
                                f"{'系统将自动重试。' if retrying else '已停止自动提交，可重新执行。'}{exc.message}"
                            ),
                        },
                    })
                    if retrying:
                        await asyncio.sleep(GENERATION_RETRY_BASE_SECONDS * attempt)
            if start is None:
                if last_start_error:
                    raise last_start_error
                raise ApiFailure("CPA 提交失败，未返回结果")
            request_id = start.get("request_id")
            if not request_id:
                raise ApiFailure("CPA 没有返回 request_id", data=start)
            await update_scene(task_id, scene_id, {"status": "polling", "request_id": request_id, "request_id_created_at": scene.get("execution_started_at") or now_ts(), "raw_start": start})

            deadline = time.time() + 60 * 20
            last_data: dict[str, Any] = {}
            poll_error_count = 0
            while time.time() < deadline:
                try:
                    result = await call_cpa("GET", f"{api_base()}/videos/{request_id}", headers={"Authorization": auth_headers()["Authorization"]}, timeout=70)
                except ApiFailure as exc:
                    poll_error_count += 1
                    info = classify_error(exc)
                    retrying = poll_error_count < GENERATION_POLL_ERROR_RETRIES
                    await update_scene(task_id, scene_id, {
                        "status": "polling" if retrying else "failed",
                        "poll_retry_count": poll_error_count,
                        "error": info | {
                            "raw": exc.raw,
                            "message": (
                                f"拉取结果第 {poll_error_count}/{GENERATION_POLL_ERROR_RETRIES} 次失败。"
                                f"{'系统将自动重试。' if retrying else '已保留任务 ID，可稍后点击“拉取结果”。'}{exc.message}"
                            ),
                        },
                    })
                    if not retrying:
                        return
                    await asyncio.sleep(min(60, GENERATION_RETRY_BASE_SECONDS * poll_error_count))
                    continue
                poll_error_count = 0
                last_data = result
                data = tasks_data()
                task = find_task(data, task_id)
                scene = find_scene(task, scene_id)
                update_scene_from_video_result(scene, result, pending_as_failed=False)
                scene["poll_retry_count"] = 0
                scene["updated_at"] = now_ts()
                task["updated_at"] = now_ts()
                await save_tasks(data)
                if scene.get("status") == "succeeded":
                    return
                if scene.get("status") == "failed":
                    return
                await asyncio.sleep(int(settings().get("poll_interval_seconds", 5)))
            raise TimeoutError(f"等待生成超时，最后返回：{last_data}")
        except Exception as exc:
            info = classify_error(exc)
            await update_scene(task_id, scene_id, {"status": "failed", "error": info | {"raw": str(exc)}})
        finally:
            running_scene_jobs.discard(job_key)


async def schedule_task(task_id: str, scene_ids: list[int], interval: float) -> None:
    for scene_id in scene_ids:
        data = tasks_data()
        task = find_task(data, task_id)
        scene = find_scene(task, scene_id)
        if scene_busy(scene):
            continue
        scene.update({"status": "queued", "progress": 0, "error": None, "updated_at": now_ts()})
        task["updated_at"] = now_ts()
        await save_tasks(data)
        asyncio.create_task(run_scene(task_id, scene_id))
        await asyncio.sleep(max(0, interval))


def local_ffmpeg_candidates() -> tuple[str, str]:
    if os.name != "nt":
        return "", ""
    for ffmpeg in TOOLS_DIR.glob("ffmpeg/**/bin/ffmpeg.exe"):
        ffprobe = ffmpeg.with_name("ffprobe.exe")
        if ffprobe.exists():
            return str(ffmpeg), str(ffprobe)
    direct = TOOLS_DIR / "ffmpeg" / "ffmpeg.exe"
    if direct.exists() and direct.with_name("ffprobe.exe").exists():
        return str(direct), str(direct.with_name("ffprobe.exe"))
    return "", ""


def detect_ffmpeg() -> dict[str, str]:
    ffmpeg = shutil.which("ffmpeg") or ""
    ffprobe = shutil.which("ffprobe") or ""
    if ffmpeg and ffprobe:
        return {"status": "ready", "ffmpeg": ffmpeg, "ffprobe": ffprobe, "message": "使用系统 PATH 中的 ffmpeg。"}
    ffmpeg, ffprobe = local_ffmpeg_candidates()
    if ffmpeg:
        return {"status": "ready", "ffmpeg": ffmpeg, "ffprobe": ffprobe, "message": "使用项目目录内 ffmpeg。"}
    return {"status": "missing", "ffmpeg": "", "ffprobe": "", "message": "未检测到 ffmpeg。"}


def download_ffmpeg() -> None:
    global ffmpeg_download_in_progress
    if ffmpeg_download_in_progress:
        return
    if os.name != "nt":
        ffmpeg_state.update({"status": "failed", "message": "自动下载仅支持 Windows。本机/服务器请通过系统包管理器安装 ffmpeg，Docker 镜像会自动安装。"})
        return
    ffmpeg_download_in_progress = True
    ffmpeg_state.update({"status": "downloading", "message": "正在下载 ffmpeg..."})
    archive = TOOLS_DIR / "ffmpeg-release-essentials.zip"
    try:
        urllib.request.urlretrieve(ffmpeg_state["download_url"], archive)
        extract_dir = TOOLS_DIR / "ffmpeg"
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(extract_dir)
        detected = detect_ffmpeg()
        ffmpeg_state.update(detected)
        if detected["status"] != "ready":
            ffmpeg_state.update({"status": "failed", "message": "ffmpeg 已下载但未找到可执行文件，请手动检查 tools/ffmpeg。"})
    except Exception as exc:
        ffmpeg_state.update({"status": "failed", "message": f"自动下载 ffmpeg 失败：{exc}。可手动下载后放入 tools/ffmpeg。"})
    finally:
        ffmpeg_download_in_progress = False


async def ensure_ffmpeg_background() -> None:
    ffmpeg_state.update(detect_ffmpeg())
    if ffmpeg_state["status"] == "missing":
        await asyncio.to_thread(download_ffmpeg)


def video_spec(path: Path) -> dict[str, Any]:
    ffprobe = ffmpeg_state.get("ffprobe") or detect_ffmpeg().get("ffprobe", "")
    if not ffprobe:
        raise RuntimeError("ffprobe 不可用")
    cmd = [
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,codec_name",
        "-of", "json", str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    streams = json.loads(result.stdout).get("streams", [])
    if not streams:
        raise RuntimeError(f"无法读取视频规格：{path.name}")
    return streams[0]


def download_url_single_thread(url: str, output: Path, progress_callback=None) -> None:
    tmp = output.with_suffix(".mp4.tmp")
    range_total = probe_range_total(url)
    resume_from = tmp.stat().st_size if range_total > 0 and tmp.exists() else 0
    if range_total > 0 and resume_from > range_total:
        tmp.unlink()
        resume_from = 0
    if range_total > 0 and resume_from == range_total:
        os.replace(tmp, output)
        if progress_callback:
            progress_callback(output.stat().st_size, output.stat().st_size)
        return

    headers = {"Range": f"bytes={resume_from}-"} if resume_from > 0 else None
    with requests.get(url, headers=headers, stream=True, timeout=(20, 120)) as response:
        if resume_from > 0 and response.status_code != 206:
            tmp.unlink(missing_ok=True)
            resume_from = 0
            headers = None
            response.close()
            with requests.get(url, stream=True, timeout=(20, 120)) as restart_response:
                restart_response.raise_for_status()
                total = int(restart_response.headers.get("Content-Length") or 0)
                done = 0
                if progress_callback:
                    progress_callback(done, total)
                with tmp.open("wb") as fh:
                    for chunk in restart_response.iter_content(chunk_size=DOWNLOAD_CHUNK_BYTES):
                        if chunk:
                            fh.write(chunk)
                            done += len(chunk)
                            if progress_callback:
                                progress_callback(done, total)
            if tmp.stat().st_size <= 0:
                raise RuntimeError("downloaded file is empty")
            os.replace(tmp, output)
            if progress_callback:
                progress_callback(output.stat().st_size, output.stat().st_size)
            return
        response.raise_for_status()
        total = range_total or int(response.headers.get("Content-Length") or 0)
        done = resume_from
        if progress_callback:
            progress_callback(done, total)
        with tmp.open("ab" if resume_from else "wb") as fh:
            for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_BYTES):
                if chunk:
                    fh.write(chunk)
                    done += len(chunk)
                    if progress_callback:
                        progress_callback(done, total)
        if tmp.stat().st_size <= 0:
            raise RuntimeError("downloaded file is empty")
        if total > 0 and tmp.stat().st_size != total:
            raise RuntimeError(f"下载未完成，已保留断点：{tmp.stat().st_size}/{total}")
        os.replace(tmp, output)
        if progress_callback:
            progress_callback(output.stat().st_size, output.stat().st_size)


def probe_range_total(url: str) -> int:
    head_total = 0
    try:
        with requests.head(url, allow_redirects=True, timeout=(20, 60)) as response:
            if response.ok:
                head_total = int(response.headers.get("Content-Length") or 0)
    except Exception:
        pass
    try:
        with requests.get(url, headers={"Range": "bytes=0-0"}, stream=True, timeout=(20, 60)) as response:
            if response.status_code != 206:
                return 0
            content_range = response.headers.get("Content-Range", "")
            match = re.search(r"/(\d+)$", content_range)
            if match:
                return int(match.group(1))
            return head_total
    except Exception:
        return 0


def download_ranges(total: int, thread_count: int) -> list[tuple[int, int]]:
    workers = max(1, min(thread_count, total))
    chunk_size = (total + workers - 1) // workers
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < total:
        end = min(total - 1, start + chunk_size - 1)
        ranges.append((start, end))
        start = end + 1
    return ranges


def download_resume_manifest(output: Path) -> Path:
    return output.with_name(f".{output.name}.resume.json")


def download_part_paths(output: Path, count: int) -> list[Path]:
    return [output.with_name(f".{output.name}.part{index}") for index in range(count)]


def cleanup_download_parts(output: Path, count: int) -> None:
    output.with_suffix(".mp4.tmp").unlink(missing_ok=True)
    download_resume_manifest(output).unlink(missing_ok=True)
    for part in download_part_paths(output, count):
        part.unlink(missing_ok=True)


def cleanup_download_cache(output: Path) -> list[str]:
    deleted: list[str] = []
    candidates = [output.with_suffix(".mp4.tmp"), download_resume_manifest(output)]
    candidates.extend(output.parent.glob(f".{output.name}.part*"))
    for path in candidates:
        if path.exists() and path.is_file():
            path.unlink()
            deleted.append(path.name)
    return deleted


def download_url_multi_thread(url: str, output: Path, thread_count: int, progress_callback=None) -> None:
    total = probe_range_total(url)
    if total <= 0:
        download_url_single_thread(url, output, progress_callback)
        return
    ranges = download_ranges(total, thread_count)
    if len(ranges) <= 1:
        download_url_single_thread(url, output, progress_callback)
        return

    tmp = output.with_suffix(".mp4.tmp")
    manifest_path = download_resume_manifest(output)
    serializable_ranges = [[start, end] for start, end in ranges]
    manifest = {
        "url": url,
        "total": total,
        "ranges": serializable_ranges,
        "part_count": len(ranges),
    }
    existing_manifest: dict[str, Any] = {}
    if manifest_path.exists():
        try:
            existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            existing_manifest = {}
    if existing_manifest != manifest:
        cleanup_download_parts(output, max(len(ranges), int(existing_manifest.get("part_count") or 0)))
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    part_paths = download_part_paths(output, len(ranges))
    part_done: list[int] = []
    for index, (start, end) in enumerate(ranges):
        expected = end - start + 1
        current = part_paths[index].stat().st_size if part_paths[index].exists() else 0
        if current > expected:
            part_paths[index].unlink()
            current = 0
        part_done.append(current)
    progress_lock = threading.Lock()
    if progress_callback:
        progress_callback(sum(part_done), total)

    def report_part(index: int, delta: int) -> None:
        if not progress_callback:
            return
        with progress_lock:
            part_done[index] += delta
            progress_callback(sum(part_done), total)

    def download_part(index: int, byte_range: tuple[int, int]) -> None:
        start, end = byte_range
        expected = end - start + 1
        existing = part_paths[index].stat().st_size if part_paths[index].exists() else 0
        if existing == expected:
            return
        if existing > expected:
            part_paths[index].unlink()
            existing = 0
        resume_start = start + existing
        headers = {"Range": f"bytes={resume_start}-{end}"}
        with requests.get(url, headers=headers, stream=True, timeout=(20, 120)) as response:
            if response.status_code != 206:
                raise RuntimeError(f"服务器未返回分段内容 HTTP {response.status_code}")
            with part_paths[index].open("ab" if existing else "wb") as fh:
                for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_BYTES):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    report_part(index, len(chunk))
        if part_paths[index].stat().st_size != expected:
            raise RuntimeError(f"分段 {index + 1} 未完成，已保留断点：{part_paths[index].stat().st_size}/{expected}")

    success = False
    try:
        with ThreadPoolExecutor(max_workers=len(ranges)) as executor:
            futures = [executor.submit(download_part, index, byte_range) for index, byte_range in enumerate(ranges)]
            for future in as_completed(futures):
                future.result()
        with tmp.open("wb") as output_file:
            for part in part_paths:
                with part.open("rb") as part_file:
                    shutil.copyfileobj(part_file, output_file, length=1024 * 1024)
        if tmp.stat().st_size != total:
            raise RuntimeError(f"合并后的文件大小不匹配：{tmp.stat().st_size}/{total}")
        os.replace(tmp, output)
        success = True
        if progress_callback:
            progress_callback(output.stat().st_size, output.stat().st_size)
    finally:
        if tmp.exists():
            tmp.unlink()
        if success:
            manifest_path.unlink(missing_ok=True)
            for part in part_paths:
                if part.exists():
                    part.unlink()


def download_url_to_file(url: str, output: Path, progress_callback=None, thread_count: int = 1) -> None:
    threads = normalize_download_thread_count(thread_count)
    if threads <= 1:
        download_url_single_thread(url, output, progress_callback)
        return
    download_url_multi_thread(url, output, threads, progress_callback)


def require_ffmpeg_ready() -> dict[str, str]:
    current = detect_ffmpeg()
    ffmpeg_state.update(current)
    if current["status"] != "ready":
        raise HTTPException(status_code=409, detail="ffmpeg/ffprobe 不可用，无法校验上传视频。请先点击检测/下载 ffmpeg。")
    return current


def probe_uploaded_video(path: Path) -> dict[str, Any]:
    try:
        return video_spec(path)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc))[-1200:]
        raise HTTPException(status_code=400, detail=f"上传文件不是可读取的视频，ffprobe 返回：{detail}") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"上传文件不是可读取的视频：{exc}") from exc


def transcode_video_to_mp4(source: Path, output: Path, ffmpeg: str) -> None:
    tmp_output = output.with_suffix(".transcode.tmp.mp4")
    cmd = [
        ffmpeg, "-y", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-movflags", "+faststart", str(tmp_output),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        if tmp_output.exists():
            tmp_output.unlink()
        raise HTTPException(status_code=400, detail={"message": "上传视频转为 MP4 失败", "stderr": result.stderr[-1600:]})
    os.replace(tmp_output, output)


async def write_upload_to_tmp(file: UploadFile, tmp: Path) -> int:
    size = 0
    with tmp.open("wb") as fh:
        while True:
            chunk = await file.read(UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="上传视频超过 2GB，建议先压缩或分段。")
            fh.write(chunk)
    return size


def image_host_headers(base_url: str) -> dict[str, str]:
    origin = normalize_https_base_url(base_url)
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Origin": origin,
        "Pragma": "no-cache",
        "Referer": origin + "/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
    }


def post_image_to_host(base_url: str, filename: str, content: bytes, content_type: str) -> dict[str, Any]:
    upload_url = image_host_upload_url(base_url)
    response = requests.post(
        upload_url,
        headers=image_host_headers(base_url),
        files={"file": (filename, content, content_type or "application/octet-stream")},
        timeout=45,
    )
    raw = response.text
    if not response.ok:
        raise RuntimeError(f"HTTP {response.status_code}: {raw[:500]}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"图床没有返回 JSON：{raw[:500]}") from exc
    if not payload.get("success"):
        raise RuntimeError(f"图床返回失败：{json.dumps(payload, ensure_ascii=False)[:500]}")
    payload["public_url"] = public_image_url(base_url, str(payload.get("url") or ""))
    payload["upload_url"] = upload_url
    return payload


async def upload_image_to_host_with_retries(base_url: str, filename: str, content: bytes, content_type: str) -> dict[str, Any]:
    last_error = ""
    for attempt in range(1, 4):
        try:
            payload = await asyncio.to_thread(post_image_to_host, base_url, filename, content, content_type)
            payload["attempt"] = attempt
            return payload
        except Exception as exc:
            last_error = str(exc)
            if attempt < 3:
                await asyncio.sleep(1.5 * attempt)
    raise HTTPException(
        status_code=502,
        detail={
            "message": "图床上传连续 3 次失败，可能是网络连接中断或图床临时限制。请重新选择图片，再次发起上传。",
            "last_error": last_error,
        },
    )


async def read_image_upload(file: UploadFile) -> tuple[str, bytes, str]:
    filename = Path(file.filename or "upload.png").name
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise HTTPException(status_code=400, detail="只允许上传 png、jpg、jpeg、webp、gif 图片。")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="上传图片为空。")
    if len(content) > MAX_IMAGE_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="上传图片超过 25MB，请压缩后重试。")
    return filename, content, file.content_type or "application/octet-stream"


@app.middleware("http")
async def require_auth_gate(request: Request, call_next):
    path = request.url.path
    if not request_origin_allowed(request):
        return auth_block_response(403, "origin_not_allowed", "请求来源不被允许。")
    if request.method == "OPTIONS" or auth_public_path(path):
        return await call_next(request)

    config, error = load_auth_config()
    if error:
        return auth_block_response(503, "auth_config_invalid", error)
    if not config:
        return auth_block_response(401, "auth_setup_required", "请先设置授权密钥。")
    if not auth_cookie_valid(request.cookies.get(AUTH_COOKIE_NAME), config):
        return auth_block_response(401, "auth_required", "请先完成授权密钥校验。")
    return await call_next(request)


@app.get("/api/auth/status")
async def auth_status(request: Request, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    config, error = load_auth_config()
    authenticated = bool(config) and not error and request_is_authenticated(request, config)
    return {
        "initialized": bool(config) or bool(error),
        "authenticated": authenticated,
        "locked": bool(error),
        "message": error,
        "min_key_length": AUTH_MIN_KEY_LENGTH,
        "session_seconds": AUTH_COOKIE_MAX_AGE_SECONDS,
    }


@app.post("/api/auth/setup")
async def auth_setup(payload: AuthSetupInput, request: Request, response: Response) -> dict[str, Any]:
    if auth_rate_limited(request):
        raise HTTPException(status_code=429, detail={"code": "too_many_attempts", "message": "尝试次数过多，请稍后再试。"})
    validation_error = validate_auth_key(payload.key, payload.confirm_key)
    if validation_error:
        record_auth_failure(request)
        raise HTTPException(status_code=400, detail={"code": "invalid_key", "message": validation_error})
    with auth_file_lock:
        config, error = load_auth_config()
        if error:
            raise HTTPException(status_code=503, detail={"code": "auth_config_invalid", "message": error})
        if config:
            raise HTTPException(status_code=409, detail={"code": "already_initialized", "message": "授权密钥已经初始化。"})
        config = create_auth_config(payload.key)
        write_auth_config(config)
    clear_auth_failures(request)
    set_auth_cookie(response, request, config)
    return {"status": "ok", "initialized": True, "authenticated": True}


@app.post("/api/auth/login")
async def auth_login(payload: AuthLoginInput, request: Request, response: Response) -> dict[str, Any]:
    if auth_rate_limited(request):
        raise HTTPException(status_code=429, detail={"code": "too_many_attempts", "message": "尝试次数过多，请稍后再试。"})
    config, error = load_auth_config()
    if error:
        raise HTTPException(status_code=503, detail={"code": "auth_config_invalid", "message": error})
    if not config:
        raise HTTPException(status_code=409, detail={"code": "setup_required", "message": "请先设置授权密钥。"})
    if not verify_auth_key(payload.key, config):
        record_auth_failure(request)
        raise HTTPException(status_code=401, detail={"code": "invalid_key", "message": "授权密钥错误。"})
    clear_auth_failures(request)
    set_auth_cookie(response, request, config)
    return {"status": "ok", "initialized": True, "authenticated": True}


@app.post("/api/auth/logout")
async def auth_logout(request: Request, response: Response) -> dict[str, str]:
    clear_auth_cookie(response, request)
    return {"status": "ok"}


@app.on_event("startup")
async def startup() -> None:
    ensure_env_auth_config()
    ensure_settings_file()
    if not TASKS_PATH.exists():
        write_json_atomic(TASKS_PATH, {"tasks": []})
    recover_interrupted_jobs()
    repair_legacy_merge_names()
    asyncio.create_task(ensure_ffmpeg_background())


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/bootstrap")
async def bootstrap() -> dict[str, Any]:
    return {
        "settings": public_settings(),
        "models": VIDEO_MODELS,
        "resolutions": RESOLUTIONS,
        "image_resolutions": IMAGE_RESOLUTIONS,
        "aspect_ratios": ASPECT_RATIOS,
        "durations": DURATIONS,
        "ffmpeg": ffmpeg_state,
        "download_threads": download_thread_info(settings().get("download_thread_count")),
    }


@app.get("/api/settings")
async def get_settings() -> dict[str, Any]:
    return public_settings()


@app.put("/api/settings")
async def put_settings(payload: SettingsInput) -> dict[str, Any]:
    current = settings()
    incoming = payload.model_dump()
    for secret_key in ("cpa_base_url", "cpa_api_key", "cpa_management_key"):
        if not str(incoming.get(secret_key) or "").strip():
            incoming[secret_key] = current.get(secret_key, "")
    data = normalize_image_host_settings(DEFAULT_SETTINGS | incoming)
    data["max_parallel_generations"] = 8
    data["download_parallelism"] = 4
    data["download_thread_count"] = normalize_download_thread_count(data.get("download_thread_count"), fallback=RECOMMENDED_DOWNLOAD_THREADS)
    write_json_atomic(SETTINGS_PATH, data)
    return {"status": "ok"}


@app.post("/api/image-host/test")
async def test_image_host(payload: ImageHostTestInput) -> dict[str, Any]:
    target = payload.image_host_url or settings().get("image_host_selected_url") or settings().get("image_host_base_url")
    base_url = normalize_https_base_url(target)
    result = await upload_image_to_host_with_retries(base_url, "healthcheck.png", TEST_IMAGE_PNG, "image/png")
    return {
        "status": "ok",
        "image_host_url": base_url,
        "public_url": result["public_url"],
        "attempt": result["attempt"],
        "raw_url": result.get("url", ""),
    }


@app.post("/api/image-host/upload")
async def upload_image_host(file: UploadFile = File(...), image_host_url: str = Form("")) -> dict[str, Any]:
    try:
        target = image_host_url or settings().get("image_host_selected_url") or settings().get("image_host_base_url")
        base_url = normalize_https_base_url(target)
        filename, content, content_type = await read_image_upload(file)
        result = await upload_image_to_host_with_retries(base_url, filename, content, content_type)
        return {
            "status": "ok",
            "image_host_url": base_url,
            "public_url": result["public_url"],
            "attempt": result["attempt"],
            "raw_url": result.get("url", ""),
        }
    finally:
        await file.close()


@app.get("/api/tasks")
async def list_tasks() -> dict[str, Any]:
    return tasks_data()


@app.post("/api/tasks")
async def create_task(payload: TaskInput) -> dict[str, Any]:
    data = tasks_data()
    task_id = f"task_{int(time.time())}_{len(data['tasks']) + 1}"
    name = payload.name.strip() or task_id
    task = {
        "id": task_id,
        "name": name,
        "created_at": now_ts(),
        "updated_at": now_ts(),
        "global_params": {
            "model": payload.global_params.get("model", "grok-imagine-video"),
            "duration": normalize_duration(payload.global_params.get("duration", 8)),
            "resolution": payload.global_params.get("resolution", "720p"),
            "aspect_ratio": payload.global_params.get("aspect_ratio", "16:9"),
            "submit_interval_seconds": float(payload.global_params.get("submit_interval_seconds", 5)),
        },
        "scenes": [],
        "merges": [],
    }
    data["tasks"].insert(0, task)
    await save_tasks(data)
    return task


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str) -> dict[str, Any]:
    return find_task(tasks_data(), task_id)


@app.put("/api/tasks/{task_id}/global-params")
async def update_global_params(task_id: str, payload: GlobalParamsInput) -> dict[str, Any]:
    data = tasks_data()
    task = find_task(data, task_id)
    params = payload.model_dump()
    params["duration"] = int(params["duration"])
    params["submit_interval_seconds"] = float(params["submit_interval_seconds"])
    task["global_params"] = params
    task["updated_at"] = now_ts()
    await save_tasks(data)
    return task


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str) -> dict[str, Any]:
    data = tasks_data()
    task = next((item for item in data["tasks"] if item["id"] == task_id), None)
    if task and any(scene_busy(scene) for scene in task.get("scenes", [])):
        raise HTTPException(status_code=409, detail="任务中有分镜正在生成或下载，请完成后再删除。")
    before = len(data["tasks"])
    data["tasks"] = [task for task in data["tasks"] if task["id"] != task_id]
    if len(data["tasks"]) == before:
        raise HTTPException(status_code=404, detail="任务不存在")
    await save_tasks(data)
    folder = (VIDEOS_DIR / slug(task_id, "task")).resolve()
    videos_root = VIDEOS_DIR.resolve()
    if folder != videos_root and videos_root in folder.parents and folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    return {"status": "ok"}


@app.post("/api/tasks/{task_id}/scenes")
async def add_scene(task_id: str, payload: SceneInput) -> dict[str, Any]:
    data = tasks_data()
    task = find_task(data, task_id)
    next_id = max([scene["id"] for scene in task["scenes"]] or [0]) + 1
    image_url = "\n".join(validate_scene_image_urls(payload.image_url))
    scene = {
        "id": next_id,
        "prompt": payload.prompt,
        "image_url": image_url,
        "params": {k: v for k, v in payload.model_dump().items() if k in {"model", "duration", "resolution", "aspect_ratio"} and v not in (None, "")},
        "status": "draft",
        "progress": 0,
        "request_id": "",
        "video_url": "",
        "local_video": "",
        "download_status": "not_downloaded",
        "error": None,
        "created_at": now_ts(),
        "updated_at": now_ts(),
    }
    task["scenes"].append(scene)
    task["updated_at"] = now_ts()
    await save_tasks(data)
    return scene


@app.put("/api/tasks/{task_id}/scenes/{scene_id}")
async def update_scene_api(task_id: str, scene_id: int, payload: SceneInput) -> dict[str, Any]:
    data = tasks_data()
    task = find_task(data, task_id)
    scene = find_scene(task, scene_id)
    reject_if_scene_busy(scene)
    next_prompt = payload.prompt
    next_image_url = "\n".join(validate_scene_image_urls(payload.image_url))
    next_params = {k: v for k, v in payload.model_dump().items() if k in {"model", "duration", "resolution", "aspect_ratio"} and v not in (None, "")}
    changed = scene.get("prompt", "") != next_prompt or scene.get("image_url", "") != next_image_url or scene.get("params", {}) != next_params
    if changed:
        clear_scene_result(scene, "用户修改了分镜输入或参数，旧结果已归档，避免误合并。")
    scene["prompt"] = next_prompt
    scene["image_url"] = next_image_url
    scene["params"] = next_params
    scene["updated_at"] = now_ts()
    task["updated_at"] = now_ts()
    await save_tasks(data)
    return scene


@app.patch("/api/tasks/{task_id}/scenes/{scene_id}/id")
async def update_scene_id(task_id: str, scene_id: int, payload: SceneIdInput) -> dict[str, Any]:
    if payload.id <= 0:
        raise HTTPException(status_code=400, detail="分镜 ID 必须是正整数")
    data = tasks_data()
    task = find_task(data, task_id)
    scene = find_scene(task, scene_id)
    reject_if_scene_busy(scene)
    if payload.id != scene_id and any(item["id"] == payload.id for item in task.get("scenes", [])):
        raise HTTPException(status_code=409, detail=f"分镜 ID {payload.id} 已存在，请换一个。")
    scene["id"] = payload.id
    scene["updated_at"] = now_ts()
    task["updated_at"] = now_ts()
    await save_tasks(data)
    return scene


@app.post("/api/tasks/{task_id}/scenes/reorder")
async def reorder_scenes(task_id: str, payload: ReorderScenesInput) -> dict[str, Any]:
    data = tasks_data()
    task = find_task(data, task_id)
    current_ids = [scene["id"] for scene in task.get("scenes", [])]
    if set(payload.scene_ids) != set(current_ids) or len(payload.scene_ids) != len(current_ids):
        raise HTTPException(status_code=400, detail="分镜顺序必须包含当前任务里的全部分镜 ID，且不能重复。")
    scene_by_id = {scene["id"]: scene for scene in task.get("scenes", [])}
    task["scenes"] = [scene_by_id[scene_id] for scene_id in payload.scene_ids]
    task["updated_at"] = now_ts()
    await save_tasks(data)
    return task


@app.delete("/api/tasks/{task_id}/scenes/{scene_id}")
async def delete_scene(task_id: str, scene_id: int) -> dict[str, Any]:
    data = tasks_data()
    task = find_task(data, task_id)
    scene = find_scene(task, scene_id)
    reject_if_scene_busy(scene)
    deleted = delete_scene_related_videos(scene)
    clear_scene_result(scene, "用户删除了该分镜的视频结果，分镜稿保留。")
    scene["updated_at"] = now_ts()
    task["updated_at"] = now_ts()
    await save_tasks(data)
    return {"status": "ok", "deleted": deleted}


@app.delete("/api/tasks/{task_id}/scenes/{scene_id}/remove")
async def remove_scene(task_id: str, scene_id: int) -> dict[str, Any]:
    data = tasks_data()
    task = find_task(data, task_id)
    scene = find_scene(task, scene_id)
    reject_if_scene_busy(scene)
    deleted = delete_scene_local_video(scene)
    before = len(task.get("scenes", []))
    task["scenes"] = [item for item in task.get("scenes", []) if item.get("id") != scene_id]
    if len(task["scenes"]) == before:
        raise HTTPException(status_code=404, detail="分镜不存在")
    for merge in task.get("merges", []):
        merge["scene_ids"] = [item for item in merge.get("scene_ids", []) if item != scene_id]
        merge["order_label"] = " -> ".join(str(item) for item in merge.get("scene_ids", []))
        merge["order_note"] = f"按分镜顺序合并：{merge['order_label']}" if merge.get("scene_ids") else "原分镜已删除"
    task["updated_at"] = now_ts()
    await save_tasks(data)
    return {"status": "ok", "scene_id": scene_id, "deleted": deleted}


@app.post("/api/tasks/{task_id}/submit")
async def submit_task(task_id: str, payload: SubmitInput, background: BackgroundTasks) -> dict[str, Any]:
    task = find_task(tasks_data(), task_id)
    scene_ids = payload.scene_ids or [scene["id"] for scene in task["scenes"]]
    validate_scene_ids(task, scene_ids)
    interval = payload.submit_interval_seconds
    if interval is None:
        interval = float(task.get("global_params", {}).get("submit_interval_seconds", 5))
    background.add_task(schedule_task, task_id, scene_ids, float(interval))
    return {"status": "scheduled", "scene_ids": scene_ids, "submit_interval_seconds": interval}


@app.post("/api/tasks/{task_id}/scenes/{scene_id}/run")
async def run_one_scene(task_id: str, scene_id: int, background: BackgroundTasks) -> dict[str, Any]:
    scene = find_scene(find_task(tasks_data(), task_id), scene_id)
    if scene_busy(scene):
        raise HTTPException(status_code=409, detail=f"分镜 {scene_id} 正在生成或下载，请完成后再执行。")
    background.add_task(run_scene, task_id, scene_id)
    return {"status": "scheduled", "scene_id": scene_id}


@app.post("/api/tasks/{task_id}/scenes/{scene_id}/refresh-result")
async def refresh_scene_result(task_id: str, scene_id: int) -> dict[str, Any]:
    data = tasks_data()
    task = find_task(data, task_id)
    scene = find_scene(task, scene_id)
    if scene.get("download_status") == "downloading":
        raise HTTPException(status_code=409, detail=f"分镜 {scene_id} 正在下载，请稍后再拉取生成结果。")
    request_id = scene.get("request_id", "").strip()
    if not request_id:
        raise HTTPException(status_code=400, detail="这个分镜还没有上游任务 ID。请重新执行，或手动填写视频 URL。")
    if not scene_request_fresh(scene):
        raise HTTPException(status_code=400, detail="旧任务 ID 已超过 24 小时，请先点击“执行”重新提交制作。")
    try:
        result = await call_cpa("GET", f"{api_base()}/videos/{request_id}", headers={"Authorization": auth_headers()["Authorization"]}, timeout=90)
    except ApiFailure as exc:
        info = classify_error(exc)
        scene.update({
            "status": "failed",
            "error": info | {
                "raw": exc.raw,
                "message": f"手动拉取任务 ID {request_id} 失败：{exc.message}",
            },
            "updated_at": now_ts(),
        })
        task["updated_at"] = now_ts()
        await save_tasks(data)
        raise HTTPException(status_code=502, detail=info | {"raw": exc.raw, "message": exc.message}) from exc

    update_scene_from_video_result(scene, result, pending_as_failed=True)
    scene["updated_at"] = now_ts()
    task["updated_at"] = now_ts()
    await save_tasks(data)
    return scene


@app.post("/api/tasks/{task_id}/scenes/{scene_id}/manual-url")
async def manual_url(task_id: str, scene_id: int, payload: ManualUrlInput) -> dict[str, Any]:
    video_url = payload.video_url.strip()
    if not video_url:
        raise HTTPException(status_code=400, detail="视频 URL 不能为空")
    data = tasks_data()
    task = find_task(data, task_id)
    scene = find_scene(task, scene_id)
    reject_if_scene_busy(scene)
    if scene.get("video_url") != video_url:
        archive_scene_result(scene, "用户更新了手动视频 URL，旧本地结果已归档，避免误合并。")
        scene["local_video"] = ""
        scene["local_video_url"] = ""
        scene["manual_upload"] = False
        scene.pop("manual_uploaded_at", None)
        scene.pop("upload_original_name", None)
        scene.pop("uploaded_video_spec", None)
    scene.update({
        "status": "succeeded",
        "progress": 100,
        "video_url": video_url,
        "finished_at": now_ts(),
        "download_status": "not_downloaded",
        "download_error": "",
        "download_progress": 0,
        "download_bytes_done": 0,
        "download_bytes_total": 0,
        "download_attempt": 0,
        "error": None,
        "manual_url": True,
        "manual_url_created_at": now_ts(),
        "updated_at": now_ts(),
    })
    task["updated_at"] = now_ts()
    await save_tasks(data)
    return scene


@app.post("/api/tasks/{task_id}/scenes/{scene_id}/upload-video")
async def upload_scene_video(task_id: str, scene_id: int, file: UploadFile = File(...)) -> dict[str, Any]:
    data = tasks_data()
    task = find_task(data, task_id)
    scene = find_scene(task, scene_id)
    reject_if_scene_busy(scene)

    original_name = Path(file.filename or "").name
    ext = Path(original_name).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(status_code=400, detail="只允许上传 mp4、mov、webm、mkv 视频文件。")

    current = require_ffmpeg_ready()
    folder = scene_dir(task_id)
    output = folder / scene_video_filename(task, scene_id)
    tmp = folder / f".{output.stem}.{int(time.time())}.upload{ext}"
    try:
        size = await write_upload_to_tmp(file, tmp)
        if size <= 0:
            raise HTTPException(status_code=400, detail="上传文件为空。")

        await asyncio.to_thread(probe_uploaded_video, tmp)
        if ext == ".mp4":
            os.replace(tmp, output)
        else:
            await asyncio.to_thread(transcode_video_to_mp4, tmp, output, current["ffmpeg"])
        spec = await asyncio.to_thread(probe_uploaded_video, output)

        data = tasks_data()
        task = find_task(data, task_id)
        scene = find_scene(task, scene_id)
        scene.update({
            "status": "succeeded",
            "progress": 100,
            "download_status": "downloaded",
            "download_error": "",
            "download_progress": 100,
            "download_bytes_done": output.stat().st_size,
            "download_bytes_total": output.stat().st_size,
            "download_attempt": 0,
            "download_finished_at": now_ts(),
            "local_video": str(output.relative_to(ROOT)).replace("\\", "/"),
            "local_video_url": public_video_url(task_id, output.name),
            "manual_upload": True,
            "manual_uploaded_at": now_ts(),
            "upload_original_name": original_name,
            "uploaded_video_spec": spec,
            "error": None,
            "updated_at": now_ts(),
        })
        task["updated_at"] = now_ts()
        await save_tasks(data)
        return scene
    except HTTPException:
        if tmp.exists():
            tmp.unlink()
        raise
    except Exception as exc:
        if tmp.exists():
            tmp.unlink()
        raise HTTPException(status_code=500, detail=f"上传视频处理失败：{exc}") from exc
    finally:
        await file.close()
        if tmp.exists():
            tmp.unlink()


async def download_scene_video(task_id: str, scene_id: int, thread_count: int | None = None) -> None:
    async with download_semaphore:
        threads = selected_download_threads(thread_count)
        data = tasks_data()
        task = find_task(data, task_id)
        scene = find_scene(task, scene_id)
        url = scene.get("video_url")
        if not url:
            await update_scene(task_id, scene_id, {
                "download_status": "failed",
                "download_progress": 0,
                "download_error": "没有视频 URL，可手动填写 URL。",
            })
            return
        await update_scene(task_id, scene_id, {
            "download_status": "pending",
            "download_progress": 0,
            "download_bytes_done": 0,
            "download_bytes_total": 0,
            "download_attempt": 0,
            "download_error": "",
            "download_thread_count": threads,
            "download_thread_max": LOCAL_MAX_DOWNLOAD_THREADS,
            "download_thread_recommended": RECOMMENDED_DOWNLOAD_THREADS,
            "download_started_at": now_ts(),
        })
        output = scene_dir(task_id) / scene_video_filename(task, scene_id)
        last_error = ""
        for attempt in range(1, 4):
            try:
                loop = asyncio.get_running_loop()
                last_report = {"ts": 0.0, "progress": -1}
                report_lock = threading.Lock()

                def report_progress(done: int, total: int) -> None:
                    with report_lock:
                        if total > 0:
                            progress = int(max(0, min(99, (done / total) * 100)))
                        else:
                            progress = int(max(0, min(95, done / (1024 * 1024) * 4)))
                        now = time.time()
                        if progress >= 99 or progress - last_report["progress"] >= 2 or now - last_report["ts"] >= 1:
                            last_report.update({"ts": now, "progress": progress})
                            future = asyncio.run_coroutine_threadsafe(update_scene(task_id, scene_id, {
                                "download_status": "downloading",
                                "download_progress": progress,
                                "download_bytes_done": done,
                                "download_bytes_total": total,
                                "download_attempt": attempt,
                                "download_thread_count": threads,
                                "download_thread_max": LOCAL_MAX_DOWNLOAD_THREADS,
                                "download_thread_recommended": RECOMMENDED_DOWNLOAD_THREADS,
                                "download_error": "",
                            }), loop)
                            try:
                                future.result(timeout=5)
                            except Exception:
                                pass

                await update_scene(task_id, scene_id, {
                    "download_status": "downloading",
                    "download_progress": 0,
                    "download_bytes_done": 0,
                    "download_bytes_total": 0,
                    "download_attempt": attempt,
                    "download_thread_count": threads,
                    "download_thread_max": LOCAL_MAX_DOWNLOAD_THREADS,
                    "download_thread_recommended": RECOMMENDED_DOWNLOAD_THREADS,
                    "download_error": "",
                })
                await asyncio.to_thread(download_url_to_file, url, output, report_progress, threads)
                await update_scene(task_id, scene_id, {
                    "download_status": "downloaded",
                    "download_progress": 100,
                    "download_bytes_done": output.stat().st_size,
                    "download_bytes_total": output.stat().st_size,
                    "download_thread_count": threads,
                    "download_thread_max": LOCAL_MAX_DOWNLOAD_THREADS,
                    "download_thread_recommended": RECOMMENDED_DOWNLOAD_THREADS,
                    "local_video": str(output.relative_to(ROOT)).replace("\\", "/"),
                    "local_video_url": public_video_url(task_id, output.name),
                    "download_error": "",
                    "download_finished_at": now_ts(),
                })
                return
            except Exception as exc:
                last_error = str(exc)
                await update_scene(task_id, scene_id, {
                    "download_status": "pending" if attempt < 3 else "downloading",
                    "download_error": f"第 {attempt} 次下载失败：{last_error}",
                    "download_attempt": attempt,
                })
                await asyncio.sleep(2 * attempt)
        deleted_cache = cleanup_download_cache(output)
        await update_scene(task_id, scene_id, {
            "download_status": "failed",
            "download_progress": 0,
            "download_error": (
                f"下载失败三次：{last_error}。远程视频 URL 可能需要境外网络才能访问；"
                f"已清除本次断点缓存{f'（{len(deleted_cache)} 个临时文件）' if deleted_cache else ''}。"
                "生成结果已保留，请在可访问该 URL 的网络中手动下载，或为本机配置代理后重试。"
            ),
            "download_cache_cleared_at": now_ts(),
        })


@app.post("/api/tasks/{task_id}/scenes/{scene_id}/download")
async def download_scene(task_id: str, scene_id: int, background: BackgroundTasks, payload: DownloadInput | None = Body(default=None)) -> dict[str, Any]:
    scene = find_scene(find_task(tasks_data(), task_id), scene_id)
    if scene_busy(scene):
        raise HTTPException(status_code=409, detail=f"分镜 {scene_id} 正在生成或下载，请稍后再下载。")
    if not scene.get("video_url"):
        raise HTTPException(status_code=400, detail="分镜还没有视频 URL，不能下载。请先拉取结果或手动填写 URL。")
    if not scene_video_url_fresh(scene):
        raise HTTPException(status_code=400, detail="本次执行时间已超过 24 小时，请重新执行，拿到新任务 ID 后再下载。")
    threads = selected_download_threads(payload.thread_count if payload else None)
    background.add_task(download_scene_video, task_id, scene_id, threads)
    return {"status": "scheduled", "thread_count": threads, "thread_max": LOCAL_MAX_DOWNLOAD_THREADS, "thread_recommended": RECOMMENDED_DOWNLOAD_THREADS}


@app.post("/api/tasks/{task_id}/download")
async def download_many(task_id: str, payload: DownloadInput, background: BackgroundTasks) -> dict[str, Any]:
    task = find_task(tasks_data(), task_id)
    scene_ids = payload.scene_ids or [scene["id"] for scene in task["scenes"] if scene.get("video_url")]
    validate_scene_ids(task, scene_ids)
    threads = selected_download_threads(payload.thread_count)
    for scene_id in scene_ids:
        scene = find_scene(task, scene_id)
        if scene_busy(scene):
            raise HTTPException(status_code=409, detail=f"分镜 {scene_id} 正在生成或下载，请稍后再下载。")
        if not scene.get("video_url"):
            raise HTTPException(status_code=400, detail=f"分镜 {scene_id} 还没有视频 URL，不能下载。")
        if not scene_video_url_fresh(scene):
            raise HTTPException(status_code=400, detail=f"分镜 {scene_id} 的本次执行时间已超过 24 小时，请重新执行，拿到新任务 ID 后再下载。")
        background.add_task(download_scene_video, task_id, scene_id, threads)
    return {"status": "scheduled", "scene_ids": scene_ids, "thread_count": threads, "thread_max": LOCAL_MAX_DOWNLOAD_THREADS, "thread_recommended": RECOMMENDED_DOWNLOAD_THREADS}


@app.post("/api/tasks/{task_id}/merge")
async def merge_videos(task_id: str, payload: MergeInput) -> dict[str, Any]:
    current = detect_ffmpeg()
    ffmpeg_state.update(current)
    if current["status"] != "ready":
        raise HTTPException(status_code=409, detail="ffmpeg 不可用，无法合并。请等待自动下载或手动放入 tools/ffmpeg。")
    data = tasks_data()
    task = find_task(data, task_id)
    scenes = [find_scene(task, scene_id) for scene_id in payload.scene_ids]
    paths = []
    for scene in scenes:
        local = scene.get("local_video")
        if not local:
            raise HTTPException(status_code=409, detail=f"分镜 {scene['id']} 尚未下载到本地，不能合并。")
        path = ROOT / local
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"分镜 {scene['id']} 本地视频不存在。")
        paths.append(path)

    specs = [video_spec(path) for path in paths]
    simple_keys = {(spec.get("width"), spec.get("height"), spec.get("r_frame_rate")) for spec in specs}
    if len(simple_keys) > 1 and not payload.normalize:
        raise HTTPException(status_code=409, detail={"message": "分镜视频规格不一致，请选择统一格式后再合并。", "specs": specs})

    folder = scene_dir(task_id)
    desired_name = slug(strip_video_extension(payload.output_name), default_merge_name(task)) if payload.output_name else default_merge_name(task)
    existing_merge_files = {Path(str(item.get("filename") or item.get("file") or "")).name for item in task.get("merges", [])}
    out_name = unique_video_stem(folder, desired_name, existing_merge_files)
    output = folder / f"{out_name}.mp4"
    list_file = folder / f"{out_name}_concat.txt"
    list_file.write_text("".join(f"file '{path.as_posix()}'\n" for path in paths), encoding="utf-8")
    ffmpeg = current["ffmpeg"]
    if payload.normalize:
        width, height = merge_dimensions(payload.resolution, payload.aspect_ratio)
        vf = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
        cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", str(output)]
    else:
        cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", str(output)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail={"message": "合并失败", "stderr": result.stderr[-2000:]})
    video_url = public_video_url(task_id, output.name)
    merge = {
        "id": f"merge_{int(time.time())}_{len(task.get('merges', [])) + 1}",
        "created_at": now_ts(),
        "scene_ids": payload.scene_ids,
        "order_label": " -> ".join(str(scene_id) for scene_id in payload.scene_ids),
        "order_note": f"按分镜顺序合并：{' -> '.join(str(scene_id) for scene_id in payload.scene_ids)}",
        "params": {
            "normalize": payload.normalize,
            "resolution": payload.resolution,
            "aspect_ratio": payload.aspect_ratio,
            "output_name": out_name,
            "source_specs": specs,
        },
        "file": str(output.relative_to(ROOT)).replace("\\", "/"),
        "filename": output.name,
        "url": video_url,
        "download_url": f"{video_url}?download=1",
        "size": output.stat().st_size,
    }
    task["merges"].append(merge)
    task["updated_at"] = now_ts()
    await save_tasks(data)
    return merge


@app.delete("/api/tasks/{task_id}/merges/{merge_id}")
async def delete_merge(task_id: str, merge_id: str) -> dict[str, Any]:
    data = tasks_data()
    task = find_task(data, task_id)
    merges = task.get("merges", [])
    merge = next((item for item in merges if str(item.get("id")) == merge_id), None)
    if not merge:
        raise HTTPException(status_code=404, detail="合并记录不存在")
    file_value = str(merge.get("file") or "")
    if not file_value:
        raise HTTPException(status_code=404, detail="合并记录缺少本地文件路径")
    path = (ROOT / file_value).resolve()
    task_folder = scene_dir(task_id).resolve()
    if path.parent != task_folder:
        raise HTTPException(status_code=400, detail="合并文件路径不在当前任务目录，已拒绝删除")
    if path.exists():
        path.unlink()
    task["merges"] = [item for item in merges if str(item.get("id")) != merge_id]
    task["updated_at"] = now_ts()
    await save_tasks(data)
    return {"status": "ok", "deleted": path.name}


@app.get("/api/tasks/{task_id}/files/{filename}")
async def task_file(task_id: str, filename: str, download: bool = False) -> FileResponse:
    safe = Path(filename).name
    path = scene_dir(task_id) / safe
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    if download:
        return FileResponse(path, media_type="video/mp4", filename=safe)
    return FileResponse(path)


@app.get("/api/tools/ffmpeg")
async def ffmpeg_status() -> dict[str, Any]:
    detected = detect_ffmpeg()
    if detected["status"] == "ready" or ffmpeg_state.get("status") != "downloading":
        ffmpeg_state.update(detected)
    return ffmpeg_state


@app.post("/api/tools/ffmpeg/download")
async def ffmpeg_download(background: BackgroundTasks) -> dict[str, str]:
    if ffmpeg_state.get("status") == "downloading":
        return {"status": "already_downloading"}
    background.add_task(download_ffmpeg)
    return {"status": "scheduled"}


@app.get("/api/cpa/xai-auth-files")
async def cpa_xai_auth_files() -> dict[str, Any]:
    try:
        return {"files": await get_xai_auth_files()}
    except ApiFailure as exc:
        info = classify_error(exc)
        raise HTTPException(status_code=502, detail=info | {"raw": exc.raw or exc.message})


@app.delete("/api/cpa/xai-auth-files/{name}")
async def cpa_delete_xai_auth_file(name: str) -> dict[str, Any]:
    item = await assert_xai_auth_name(name)
    try:
        return await call_cpa("DELETE", f"{management_base()}/auth-files?name={urllib.parse.quote(name)}", headers=management_headers(), timeout=30)
    except ApiFailure as exc:
        info = classify_error(exc)
        raise HTTPException(status_code=502, detail=info | {"raw": exc.raw or exc.message, "account": item.get("name", name)})


@app.get("/api/cpa/xai-auth-files/{auth_index}/refresh-view")
async def cpa_refresh_xai_auth_view(auth_index: str) -> dict[str, Any]:
    try:
        item = await get_xai_auth_by_index(auth_index)
        try:
            item["billing"] = await get_xai_billing(auth_index)
            status = "ok"
            billing_error = None
        except ApiFailure as exc:
            info = classify_error(exc)
            item["billing_error"] = info | {"raw": exc.raw or exc.message}
            status = "partial"
            billing_error = item["billing_error"]
        return {"status": status, "file": item, "billing_error": billing_error, "refreshed_at": now_ts()}
    except HTTPException:
        raise
    except ApiFailure as exc:
        info = classify_error(exc)
        raise HTTPException(status_code=502, detail=info | {"raw": exc.raw or exc.message})


@app.get("/api/cpa/xai-auth-files/{auth_index}/billing")
async def cpa_xai_billing(auth_index: str) -> dict[str, Any]:
    try:
        billing = await get_xai_billing(auth_index)
        return {"status": "ok", "billing": billing, "refreshed_at": now_ts()}
    except HTTPException:
        raise
    except ApiFailure as exc:
        info = classify_error(exc)
        raise HTTPException(status_code=502, detail=info | {"raw": exc.raw or exc.message})


@app.get("/api/cpa/xai-auth-files/{name}/download")
async def cpa_download_xai_auth_file(name: str) -> FileResponse:
    try:
        await assert_xai_auth_name(name)
        response = requests.get(
            f"{management_base()}/auth-files/download?name={urllib.parse.quote(name)}",
            headers=management_headers(),
            timeout=30,
        )
        if not response.ok:
            raise ApiFailure(response.text, response.status_code, raw=response.text)
        out = DATA_DIR / f"backup_{slug(name, 'xai')}"
        out.write_bytes(response.content)
        return FileResponse(out, filename=name)
    except ApiFailure as exc:
        info = classify_error(exc)
        raise HTTPException(status_code=502, detail=info | {"raw": exc.raw or exc.message})


@app.patch("/api/cpa/xai-auth-files/{name}/disabled")
async def cpa_toggle_xai_auth_file(name: str, payload: DisabledInput) -> dict[str, Any]:
    try:
        data = await download_xai_auth_json(name)
        data["disabled"] = payload.disabled
        result = await upload_xai_auth_json(name, data)
        return {"status": "ok", "disabled": payload.disabled, "upstream": result}
    except ApiFailure as exc:
        info = classify_error(exc)
        raise HTTPException(status_code=502, detail=info | {"raw": exc.raw or exc.message})


@app.post("/api/cpa/xai-auth-files/upload")
async def cpa_upload_xai_auth_file(file: UploadFile = File(...)) -> dict[str, Any]:
    name = Path(file.filename or "").name
    if not name.startswith("xai-") or not name.endswith(".json"):
        raise HTTPException(status_code=400, detail="只允许上传 xai-*.json")
    content = await file.read()
    try:
        parsed = json.loads(content.decode("utf-8"))
        if "xai" not in name.lower() and "xai" not in json.dumps(parsed).lower():
            raise HTTPException(status_code=400, detail="文件不像 xAI/Grok 认证文件")
        response = requests.post(
            f"{management_base()}/auth-files?name={urllib.parse.quote(name)}",
            headers=management_headers() | {"Content-Type": "application/json"},
            data=json.dumps(parsed),
            timeout=30,
        )
        if not response.ok:
            raise ApiFailure(response.text, response.status_code, raw=response.text)
        return response.json() if response.text else {"status": "ok"}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="JSON 文件格式错误")
    except ApiFailure as exc:
        info = classify_error(exc)
        raise HTTPException(status_code=502, detail=info | {"raw": exc.raw or exc.message})


@app.get("/api/cpa/xai-usage")
async def cpa_xai_usage(count: int = 30) -> dict[str, Any]:
    return {
        "records": [],
        "disabled": True,
        "note": "安全保护：CPA usage-queue 会弹出所有 provider 的记录，无法只读取 xAI，因此工作台不会调用它以免影响其它 AI 账号。",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8765")), reload=False)
