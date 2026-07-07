"""Adapters for high-fidelity external PDF translation engines."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys

from ..errors import ConfigurationError, PipelineError
from ..settings import Settings


@dataclass(frozen=True)
class ProfessionalPdfMode:
    engine: str
    service: str
    variant: str
    babeldoc: bool = False

    @property
    def sibling_variant(self) -> str:
        return "dual" if self.variant == "mono" else "mono"

    def with_variant(self, variant: str) -> str:
        prefix = "babeldoc" if self.babeldoc else "pdf2zh"
        return f"{prefix}_{self.service}_{variant}"


_SERVICES = ("bing", "google", "openailiked", "ollama", "deepseek", "minimax")

PROFESSIONAL_PDF_MODES: dict[str, ProfessionalPdfMode] = {
    f"{engine}_{service}_{variant}": ProfessionalPdfMode(
        engine=engine,
        service=service,
        variant=variant,
        babeldoc=(engine == "babeldoc"),
    )
    for engine in ("pdf2zh", "babeldoc")
    for service in _SERVICES
    for variant in ("mono", "dual")
}

PROFESSIONAL_PDF_OUTPUT_MODES = set(PROFESSIONAL_PDF_MODES)

_NUMPY_SITE_CUSTOMIZE = '''\
"""Runtime compatibility shim for BabelDOC with NumPy 2.x."""
try:
    import numpy as _np
    _old_fromstring = _np.fromstring

    def _compat_fromstring(string, dtype=float, count=-1, sep='', *, like=None):
        if sep == '' and isinstance(string, (bytes, bytearray, memoryview)):
            return _np.frombuffer(string, dtype=dtype, count=count)
        if like is None:
            return _old_fromstring(string, dtype=dtype, count=count, sep=sep)
        return _old_fromstring(
            string,
            dtype=dtype,
            count=count,
            sep=sep,
            like=like,
        )

    _np.fromstring = _compat_fromstring
except Exception:
    pass
'''


def is_professional_pdf_mode(mode: str) -> bool:
    return mode in PROFESSIONAL_PDF_OUTPUT_MODES


def _language_code(target_language: str) -> str:
    lowered = target_language.lower()
    if "繁" in target_language or "traditional" in lowered:
        return "zh-TW"
    if "日" in target_language or "japanese" in lowered:
        return "ja"
    if "韩" in target_language or "korean" in lowered:
        return "ko"
    if "英" in target_language or "english" in lowered:
        return "en"
    return "zh"


def _safe_tail(text: str, limit: int = 2400) -> str:
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[-limit:]


def _uv_binary(settings: Settings) -> str:
    candidates = [
        Path(sys.executable).with_name("uv"),
        Path(settings.runtime_dir).parent / ".venv" / "bin" / "uv",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("uv")
    if found:
        return found

    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "uv",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ConfigurationError(
            "专业 PDF 引擎需要 uv 来隔离运行 pdf2zh。"
            "自动安装 uv 失败，请检查网络后重试。"
        ) from exc
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("uv")
    if found:
        return found
    raise ConfigurationError("uv 已安装但找不到可执行文件。")


def _babeldoc_sitecustomize(settings: Settings) -> Path:
    directory = settings.runtime_dir / "pdf2zh" / "sitecustomize"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "sitecustomize.py"
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if current != _NUMPY_SITE_CUSTOMIZE:
        path.write_text(_NUMPY_SITE_CUSTOMIZE, encoding="utf-8")
    return directory


def _ensure_v1_url(value: str) -> str:
    stripped = value.rstrip("/")
    return stripped if stripped.endswith("/v1") else f"{stripped}/v1"


def _strip_v1_url(value: str) -> str:
    stripped = value.rstrip("/")
    return stripped[:-3] if stripped.endswith("/v1") else stripped


def _env_for_service(
    settings: Settings,
    *,
    service: str,
) -> dict[str, str]:
    env = os.environ.copy()
    if service in {"bing", "google"}:
        return env

    key = settings.translator_api_key or ""
    model = settings.translator_model or ""
    base_url = settings.translator_base_url or ""
    if service == "openailiked":
        if not base_url or not model:
            raise ConfigurationError(
                "OpenAI-compatible 专业 PDF 引擎需要接口地址和模型名。"
            )
        if not key:
            raise ConfigurationError(
                "OpenAI-compatible 专业 PDF 引擎需要 API Key。"
                "可以在网页里保存到 Keychain 后重试。"
            )
        env.update(
            {
                "OPENAILIKED_BASE_URL": _ensure_v1_url(base_url),
                "OPENAILIKED_API_KEY": key,
                "OPENAILIKED_MODEL": model,
            }
        )
    elif service == "ollama":
        env.update(
            {
                "OLLAMA_HOST": _strip_v1_url(
                    base_url or "http://127.0.0.1:11434"
                ),
                "OLLAMA_MODEL": model or "qwen3:8b",
            }
        )
    elif service == "deepseek":
        if not key:
            raise ConfigurationError("DeepSeek 专业 PDF 引擎需要 API Key。")
        env.update(
            {
                "DEEPSEEK_API_KEY": key,
                "DEEPSEEK_MODEL": model or "deepseek-chat",
            }
        )
    elif service == "minimax":
        if not key:
            raise ConfigurationError("MiniMax 专业 PDF 引擎需要 API Key。")
        env.update(
            {
                "MINIMAX_API_KEY": key,
                "MINIMAX_MODEL": model or "MiniMax-M2.7",
            }
        )
    else:
        raise ConfigurationError(f"未知专业 PDF 翻译服务：{service}")
    return env


def _find_outputs(directory: Path, stem: str) -> dict[str, Path]:
    pdfs = list(directory.glob("*.pdf"))
    mono = [
        path
        for path in pdfs
        if "mono" in path.stem.lower() or path.stem.endswith("-zh")
    ]
    dual = [
        path
        for path in pdfs
        if "dual" in path.stem.lower() or "bilingual" in path.stem.lower()
    ]
    exact_mono = [
        directory / f"{stem}-mono.pdf",
        directory / f"{stem}.zh.mono.pdf",
        directory / f"{stem}-zh.pdf",
    ]
    exact_dual = [
        directory / f"{stem}-dual.pdf",
        directory / f"{stem}.zh.dual.pdf",
    ]
    result: dict[str, Path] = {}
    for candidate in exact_mono + mono:
        if candidate.is_file():
            result["mono"] = candidate
            break
    for candidate in exact_dual + dual:
        if candidate.is_file():
            result["dual"] = candidate
            break
    return result


def _copy_if_available(source: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, output)


def _process_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def render_professional_pdf(
    source: Path,
    selected_output: Path,
    *,
    mode: str,
    settings: Settings,
    job_dir: Path,
    outputs_dir: Path,
    title: str,
    book_id: str,
    target_language: str,
) -> tuple[Path, list[str], dict[str, str], str]:
    spec = PROFESSIONAL_PDF_MODES.get(mode)
    if not spec:
        raise PipelineError(f"未知专业 PDF 排版模式：{mode}")
    if source.suffix.lower() != ".pdf":
        raise PipelineError("专业 PDF 引擎仅支持 PDF。")

    uv = _uv_binary(settings)
    run_dir = job_dir / "professional-pdf" / mode
    run_dir.mkdir(parents=True, exist_ok=True)
    for old in run_dir.glob("*.pdf"):
        old.unlink()

    env = _env_for_service(settings, service=spec.service)
    if spec.babeldoc:
        sitecustomize = str(_babeldoc_sitecustomize(settings))
        env["PYTHONPATH"] = (
            sitecustomize
            + os.pathsep
            + env.get("PYTHONPATH", "")
        ).rstrip(os.pathsep)

    command = [
        uv,
        "tool",
        "run",
        "--python",
        "3.12",
        "pdf2zh",
    ]
    if spec.babeldoc:
        command.append("--babeldoc")
    command.extend(
        [
            str(source),
            "-li",
            "en",
            "-lo",
            _language_code(target_language),
            "-s",
            spec.service,
            "-t",
            "1",
            "-o",
            str(run_dir),
        ]
    )
    log_path = job_dir / f"professional-pdf-{mode}.log"
    try:
        result = subprocess.run(
            command,
            cwd=job_dir,
            env=env,
            text=True,
            capture_output=True,
            timeout=max(600.0, settings.translator_timeout_seconds * 6),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        log_path.write_text(
            _process_text(exc.stdout) + "\n" + _process_text(exc.stderr),
            encoding="utf-8",
        )
        raise PipelineError(
            "专业 PDF 引擎超时。可先用页数较少的论文测试，"
            "或提高翻译超时时间。"
        ) from exc
    except OSError as exc:
        raise PipelineError(f"启动专业 PDF 引擎失败：{exc}") from exc

    log_text = (
        "$ "
        + " ".join(command)
        + "\n\nSTDOUT:\n"
        + result.stdout
        + "\n\nSTDERR:\n"
        + result.stderr
    )
    log_path.write_text(log_text, encoding="utf-8")

    outputs = _find_outputs(run_dir, source.stem)
    if result.returncode != 0 or spec.variant not in outputs:
        detail = _safe_tail(log_text)
        raise PipelineError(
            "专业 PDF 引擎没有生成可用输出。"
            f"日志：{log_path}\n{detail}"
        )

    generated: dict[str, str] = {}
    selected_mode = spec.with_variant(spec.variant)
    sibling_mode = spec.with_variant(spec.sibling_variant)
    selected_path = selected_output
    _copy_if_available(outputs[spec.variant], selected_path)
    generated[selected_mode] = str(selected_path)

    if spec.sibling_variant in outputs:
        sibling_output = outputs_dir / (
            f"{title}-zh-{sibling_mode}-{book_id[:8]}.pdf"
        )
        _copy_if_available(outputs[spec.sibling_variant], sibling_output)
        generated[sibling_mode] = str(sibling_output)

    warnings = [
        (
            "已使用 PDFMathTranslate/BabelDOC 专业引擎；"
            "该路径会绕过内置 blocks.json 翻译缓存，由专业引擎自行解析、"
            "翻译并重排 PDF。"
        )
    ]
    if spec.babeldoc:
        warnings.append(
            "BabelDOC 后端已启用 NumPy 2 兼容层；若遇到上游版本变更，"
            "请检查 professional-pdf 日志。"
        )
    return selected_path, warnings, generated, str(log_path)
