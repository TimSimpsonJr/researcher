"""
detect_tier.py — Detect available infrastructure and recommend tier.

Checks for Ollama, SearXNG, yt-dlp, Whisper. Provides hardware-aware
model recommendations for Ollama.
"""

import shutil
import subprocess
import platform
import time
from pathlib import Path

import requests

import fetch_playwright
import pdf_extract


def check_ollama() -> dict:
    """Check if Ollama is installed and running."""
    result = {"installed": False, "running": False, "recommended_model": None, "models": []}
    if shutil.which("ollama") is None:
        return result
    result["installed"] = True
    try:
        proc = subprocess.run(
            ["ollama", "list"], capture_output=True, text=True, timeout=10
        )
        if proc.returncode == 0:
            result["running"] = True
            lines = proc.stdout.strip().splitlines()[1:]  # skip header
            result["models"] = [line.split()[0] for line in lines if line.strip()]
    except Exception:
        result["running"] = False
    return result


def recommend_model(ram_gb: float, vram_gb: float) -> dict:
    """Recommend an Ollama model based on hardware."""
    if ram_gb < 8 and vram_gb < 4:
        return {"recommendation": "skip", "model": None,
                "reason": "Insufficient RAM/VRAM for useful local models"}
    if vram_gb >= 12:
        return {"recommendation": "use", "model": "qwen2.5:14b",
                "reason": f"Strong fit — {vram_gb}GB VRAM handles 14B models well"}
    if vram_gb >= 6:
        return {"recommendation": "use", "model": "qwen2.5:7b",
                "reason": f"{vram_gb}GB VRAM suits 7B models"}
    if ram_gb >= 16:
        return {"recommendation": "use", "model": "qwen2.5:7b",
                "reason": f"{ram_gb}GB RAM can run 7B models on CPU (slower than GPU)"}
    if ram_gb >= 8:
        return {"recommendation": "use", "model": "qwen2.5:7b",
                "reason": f"{ram_gb}GB RAM is minimal — expect slow inference"}
    return {"recommendation": "skip", "model": None,
            "reason": "Hardware below minimum for useful local inference"}


def check_searxng(url: str | None) -> dict:
    """Check if SearXNG is reachable and its JSON API is enabled."""
    if not url:
        return {"available": False, "url": None}
    base = url.rstrip("/")
    try:
        resp = requests.get(f"{base}/healthz", timeout=5)
        if resp.status_code != 200:
            return {"available": False, "url": url, "error": f"healthz returned {resp.status_code}"}
    except Exception as e:
        return {"available": False, "url": url, "error": str(e)}
    # Verify JSON format is enabled (SearXNG returns 403 if json not in formats)
    try:
        resp = requests.get(f"{base}/search", params={"q": "test", "format": "json"}, timeout=10)
        if resp.status_code == 403:
            return {"available": False, "url": url, "error": "JSON format disabled in SearXNG settings. Add 'json' to search.formats in settings.yml"}
        return {"available": True, "url": url}
    except Exception:
        return {"available": True, "url": url}  # healthz passed, JSON check inconclusive


def check_ytdlp() -> dict:
    """Check if yt-dlp is installed."""
    return {"installed": shutil.which("yt-dlp") is not None}


def check_whisper() -> dict:
    """Check if Whisper is available (via whisper CLI or Python module)."""
    if shutil.which("whisper") is not None:
        return {"installed": True, "backend": "cli"}
    try:
        import whisper as _w  # noqa: F401
        return {"installed": True, "backend": "python"}
    except ImportError:
        return {"installed": False, "backend": None}


PLAYWRIGHT_INSTALL = ('PLAYWRIGHT_BROWSERS_PATH="<plugin root>/.cache/ms-playwright" '
                      'uv run --frozen --project "<plugin root>" playwright install chromium')
TESSERACT_INSTALL = "winget install UB-Mannheim.TesseractOCR"


def check_playwright() -> dict:
    """Playwright importable AND its Chromium build downloaded."""
    fetch_playwright.use_repo_browsers()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"status": "missing", "reason": "playwright package not installed"}
    try:
        with sync_playwright() as p:
            exe = Path(p.chromium.executable_path)
    except Exception as e:
        return {"status": "error", "reason": f"playwright failed to start: {e}"}
    if not exe.exists():
        return {"status": "missing", "reason": "Chromium not downloaded", "install": PLAYWRIGHT_INSTALL}
    return {"status": "ok", "executable": str(exe)}


def check_tesseract() -> dict:
    """Optional OCR for image-only PDFs. Never changes the tier."""
    if shutil.which("tesseract") is not None or pdf_extract.tesseract_available():
        return {"status": "ok"}
    return {"status": "missing", "optional": True, "install": TESSERACT_INSTALL}


def get_platform_info() -> dict:
    """Get basic platform info for install recommendations."""
    return {
        "os": platform.system().lower(),
        "arch": platform.machine(),
        "is_wsl": "microsoft" in platform.uname().release.lower()
                  if platform.system() == "Linux" else False,
    }


def get_hardware_info() -> dict:
    """Total RAM and NVIDIA VRAM in GB, for the setup wizard's model pick.

    Pieces that can't be detected stay 0 / "unknown"; never raises.
    """
    info = {"total_ram_gb": 0, "gpu_vram_gb": 0, "gpu_name": "unknown"}
    system = platform.system()
    try:
        if system == "Windows":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(stat)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            info["total_ram_gb"] = round(stat.ullTotalPhys / (1024 ** 3), 1)
        elif system == "Darwin":
            out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True)
            info["total_ram_gb"] = round(int(out.strip()) / (1024 ** 3), 1)
        else:
            with open("/proc/meminfo", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        info["total_ram_gb"] = round(int(line.split()[1]) / (1024 ** 2), 1)
                        break
    except Exception:
        pass
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.total,name", "--format=csv,noheader,nounits"],
            text=True, timeout=5,
        )
        parts = out.strip().splitlines()[0].split(",")
        info["gpu_vram_gb"] = round(int(parts[0].strip()) / 1024, 1)
        info["gpu_name"] = parts[1].strip()
    except Exception:
        pass  # no NVIDIA GPU or nvidia-smi not installed
    return info


BENCHMARK_PROMPT = "Summarize this in one sentence: The quick brown fox jumps over the lazy dog."


def benchmark_ollama(model: str, prompt: str = BENCHMARK_PROMPT) -> dict:
    """Time one short `ollama run` prompt: {elapsed_ms, output, success}."""
    start = time.time()
    try:
        proc = subprocess.run(
            ["ollama", "run", model, prompt],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"elapsed_ms": round((time.time() - start) * 1000),
                "output": str(e)[:200], "success": False}
    return {
        "elapsed_ms": round((time.time() - start) * 1000),
        "output": proc.stdout.strip()[:200],
        "success": proc.returncode == 0,
    }


def ensure_searxng(url: str | None, repo_root: Path | None = None) -> dict:
    """Try to start SearXNG if configured but not running.

    Checks if SearXNG is reachable. If not, attempts to start it via
    docker compose using the docker-compose.yml in the plugin repo.
    Polls for up to 15 seconds after starting.
    """
    result = check_searxng(url)
    if result.get("available"):
        return result

    if not url:
        return {"available": False, "auto_started": False, "error": "No SearXNG URL configured"}

    # Find docker-compose.yml
    compose_file = None
    if repo_root:
        candidate = repo_root / "docker" / "docker-compose.yml"
        if candidate.exists():
            compose_file = candidate

    if not compose_file:
        return {**result, "auto_started": False,
                "error": f"SearXNG not reachable and no docker-compose.yml found"}

    # Check if docker is available
    if shutil.which("docker") is None:
        return {**result, "auto_started": False,
                "error": "SearXNG not reachable and docker is not installed"}

    # Try to start the container
    try:
        proc = subprocess.run(
            ["docker", "compose", "-f", str(compose_file.name), "up", "-d"],
            capture_output=True, text=True, timeout=30,
            cwd=str(compose_file.parent)
        )
        if proc.returncode != 0:
            return {**result, "auto_started": False,
                    "error": f"docker compose up failed: {proc.stderr.strip()}"}
    except Exception as e:
        return {**result, "auto_started": False,
                "error": f"Failed to run docker compose: {e}"}

    # Poll for availability
    for _ in range(7):  # 7 attempts × 2s = 14s
        time.sleep(2)
        check = check_searxng(url)
        if check.get("available"):
            return {**check, "auto_started": True}

    final = check_searxng(url)
    return {**final, "auto_started": True,
            "error": final.get("error", "SearXNG started but not reachable after 15s")}


def build_tier_report(searxng_url: str | None, repo_root: Path | None = None) -> dict:
    """Build a comprehensive tier report with auto-start for SearXNG.

    Checks all components, attempts to auto-start SearXNG if needed,
    and returns a report suitable for user-facing tier alerts.
    """
    ollama = check_ollama()
    searxng = ensure_searxng(searxng_url, repo_root)
    ytdlp = check_ytdlp()
    whisper = check_whisper()

    # Build component status
    components = {
        "ollama": {
            "status": "ok" if ollama.get("running") else "missing",
            "installed": ollama.get("installed", False),
            "models": ollama.get("models", []),
        },
        "searxng": {
            "status": "ok" if searxng.get("available") else "missing",
            "url": searxng.get("url"),
            "auto_started": searxng.get("auto_started", False),
        },
        "ytdlp": {"status": "ok" if ytdlp.get("installed") else "missing"},
        "whisper": {"status": "ok" if whisper.get("installed") else "missing"},
        "playwright": check_playwright(),
        "tesseract": check_tesseract(),
    }

    if ollama.get("running") and ollama.get("models"):
        components["ollama"]["model"] = ollama["models"][0]
    if not searxng.get("available") and searxng.get("error"):
        components["searxng"]["error"] = searxng["error"]

    # Determine tier
    ollama_ok = ollama.get("running", False)
    searxng_ok = searxng.get("available", False)

    if ollama_ok and searxng_ok:
        tier = "full"
    elif ollama_ok:
        tier = "mid"
    else:
        tier = "base"

    # Build missing lists
    missing_for_full = []
    missing_for_mid = []

    if not searxng_ok:
        msg = f"SearXNG not reachable at {searxng_url or '(not configured)'}"
        if searxng.get("auto_started"):
            msg += f" (auto-start attempted: {searxng.get('error', 'unknown error')})"
        elif searxng.get("error"):
            msg += f" ({searxng['error']})"
        missing_for_full.append(msg)

    if not ollama_ok:
        if ollama.get("installed"):
            missing_for_mid.append("Ollama installed but not running — start with: ollama serve")
            missing_for_full.append("Ollama installed but not running — start with: ollama serve")
        else:
            missing_for_mid.append("Ollama not installed — see https://ollama.com")
            missing_for_full.append("Ollama not installed — see https://ollama.com")

    return {
        "tier": tier,
        "max_tier": "full",
        "degraded": tier != "full",
        "components": components,
        "missing_for_full": missing_for_full,
        "missing_for_mid": missing_for_mid,
    }


def detect_tier(searxng_url: str | None) -> str:
    """Detect the highest available tier."""
    ollama = check_ollama()
    searxng = check_searxng(searxng_url)
    if ollama.get("running") and searxng.get("available"):
        return "full"
    if ollama.get("running"):
        return "mid"
    return "base"
