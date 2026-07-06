"""OCR helpers for scanned PDF books."""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ..errors import ConfigurationError, PipelineError
from ..settings import Settings


@dataclass(frozen=True)
class OcrResult:
    pdf_path: Path
    sidecar_path: Path
    log_path: Path


@dataclass(frozen=True)
class OcrEnvironment:
    ocrmypdf: str | None
    tesseract: str | None
    ghostscript: str | None
    languages: list[str]
    missing_languages: list[str]

    @property
    def available(self) -> bool:
        return bool(
            self.ocrmypdf
            and self.tesseract
            and self.ghostscript
            and not self.missing_languages
        )


def ocr_install_hint() -> str:
    return (
        "缺少 OCRmyPDF/Tesseract。macOS 可执行：\n"
        "  brew install tesseract tesseract-lang ghostscript\n"
        "  ./scripts/bootstrap.sh\n"
        "或手动安装：.venv/bin/python -m pip install 'ocrmypdf>=17,<18'"
    )


def _ocrmypdf_command(settings: Settings) -> list[str]:
    candidates = [
        Path(sys.executable).resolve().parent / "ocrmypdf",
        shutil.which("ocrmypdf"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_file() and path.exists():
            return [str(path)]
        located = shutil.which(str(candidate))
        if located:
            return [located]
    try:
        import ocrmypdf  # noqa: F401
    except ImportError as exc:
        raise ConfigurationError(ocr_install_hint()) from exc
    return [sys.executable, "-m", "ocrmypdf"]


def check_ocr_environment(
    settings: Settings,
    *,
    required_languages: tuple[str, ...] = ("eng",),
) -> OcrEnvironment:
    try:
        ocrmypdf = " ".join(_ocrmypdf_command(settings))
    except ConfigurationError:
        ocrmypdf = None
    tesseract = shutil.which("tesseract")
    ghostscript = shutil.which("gs")
    languages: list[str] = []
    if tesseract:
        try:
            result = subprocess.run(
                [tesseract, "--list-langs"],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            if result.returncode == 0:
                languages = [
                    line.strip()
                    for line in result.stdout.splitlines()
                    if line.strip()
                    and not line.lower().startswith("list of")
                ]
        except (OSError, subprocess.TimeoutExpired):
            languages = []
    required = {
        part
        for item in required_languages
        for part in item.split("+")
        if part
    }
    missing = sorted(required.difference(languages))
    return OcrEnvironment(
        ocrmypdf=ocrmypdf,
        tesseract=tesseract,
        ghostscript=ghostscript,
        languages=languages,
        missing_languages=missing,
    )


def run_ocrmypdf(
    source: Path,
    job_dir: Path,
    *,
    settings: Settings,
    languages: str,
    force: bool = False,
) -> OcrResult:
    """Create a searchable PDF with OCRmyPDF and keep logs/sidecar text."""

    ocr_dir = job_dir / "ocr"
    ocr_dir.mkdir(parents=True, exist_ok=True)
    target = ocr_dir / "source-ocr.pdf"
    sidecar = ocr_dir / "source-ocr.txt"
    log_path = ocr_dir / "ocrmypdf.log"
    command = [
        *_ocrmypdf_command(settings),
        "--output-type",
        "pdf",
        "--deskew",
        "--rotate-pages",
        "--sidecar",
        str(sidecar),
        "-l",
        languages,
    ]
    if force:
        command.append("--force-ocr")
    else:
        command.append("--skip-text")
    command.extend([str(source), str(target)])

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    log_path.write_text(
        "COMMAND: " + " ".join(command) + "\n\n"
        "STDOUT:\n" + (result.stdout or "") + "\n\n"
        "STDERR:\n" + (result.stderr or ""),
        encoding="utf-8",
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if "tesseract" in detail.lower():
            detail = f"{detail}\n\n{ocr_install_hint()}"
        raise PipelineError(
            "OCRmyPDF 执行失败"
            f"（退出码 {result.returncode}）：\n{detail[-4000:]}"
        )
    if not target.is_file() or target.stat().st_size == 0:
        raise PipelineError("OCRmyPDF 没有生成有效 PDF。")
    return OcrResult(target, sidecar, log_path)
