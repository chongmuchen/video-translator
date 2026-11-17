"""Bootstrap project-local runtime dependencies."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

import httpx

from .errors import ConfigurationError
from .pipeline.kokoro import MODEL_DIR, MODEL_FILENAME, PACKAGE_DIR, REPO_ID, check_runtime
from .runtime import resolve_media_binaries
from .settings import get_settings


MODEL_REVISION = "01e7505bd6a7a2ac4975463114c3a7650a9f7218"
MODEL_ASSETS = {
    "config.json": "bc333efa5ce4ceff433c8c8e5d027a1eca0166001e4e4a62bea2d26ff7a46890",
    MODEL_FILENAME: "b1d8410fa44dfb5c15471fd6c4225ea6b4e9ac7fa03c98e8bea47a9928476e2b",
    "voices/zm_010.pt": "d2eeba86192eee269f600ca6821038034abd017532a1fe68ff7b0e86c2983b2a",
    "voices/zm_009.pt": "eff75f26089f9f986b547985a420f901661057a951088ac7c7d8473a8d6327bd",
    "voices/zm_011.pt": "978f00b0485869b2461249235429352767c661b0eeef65c37ae393a5c1531f46",
    "voices/zf_001.pt": "9bdc9a87e13e9bb1ea3e7803259c2ecbfebaeeb2ff80b5d0c76df1a464c1c962",
    "voices/zf_002.pt": "2c8cf221ff2e0915fc807cac5f233f42798ee8e2bd58bc5ad0259fd95e405a26",
    "voices/zf_017.pt": "e507bb858a75f0518f08918827740530a656a008e3c057435a17b3a95f267624",
}
PACKAGE_REQUIREMENTS = ("kokoro==0.9.4", "misaki[zh]==0.9.4", "soundfile==0.14.0")


def _packages_ready(directory: Path) -> bool:
    if not directory.is_dir():
        return False
    # Check imports in a clean process so another installation cannot mask a partial target.
    result = subprocess.run(
        [sys.executable, "-I", "-c", """
import sys
from importlib.metadata import version
from pathlib import Path

directory = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(directory))
import kokoro, misaki, soundfile, torch
from misaki import zh
from misaki.espeak import EspeakFallback

for module in (kokoro, misaki, soundfile, torch):
    if not Path(module.__file__).resolve().is_relative_to(directory):
        raise SystemExit(1)
for package in sys.argv[2:]:
    requirement, required_version = package.split('==')
    if version(requirement.split('[')[0]) != required_version:
        raise SystemExit(1)
EspeakFallback(british=False, version='2.0')
""", str(directory), *PACKAGE_REQUIREMENTS],
        capture_output=True,
    )
    return result.returncode == 0


def _install_packages() -> None:
    if _packages_ready(PACKAGE_DIR):
        return

    PACKAGE_DIR.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".kokoro-install-", dir=PACKAGE_DIR.parent) as temporary:
        staging = Path(temporary)
        packages = staging / "packages"
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--target", str(packages), *PACKAGE_REQUIREMENTS],
            check=True,
        )
        if not _packages_ready(packages):
            raise ConfigurationError("Kokoro 运行时安装后无法加载，请检查 Python 版本和安装输出。")

        previous = staging / "previous"
        if PACKAGE_DIR.exists():
            PACKAGE_DIR.rename(previous)
        try:
            packages.rename(PACKAGE_DIR)
        except OSError:
            if previous.exists() and not PACKAGE_DIR.exists():
                previous.rename(PACKAGE_DIR)
            raise


def _sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _install_model_assets() -> None:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for relative_path, expected_hash in MODEL_ASSETS.items():
        target = MODEL_DIR / Path(relative_path).name
        if target.is_file() and _sha256(target) == expected_hash:
            continue

        url = f"https://huggingface.co/{REPO_ID}/resolve/{MODEL_REVISION}/{relative_path}"
        print(f"Downloading Kokoro asset: {relative_path}", flush=True)
        with NamedTemporaryFile(dir=MODEL_DIR, prefix=f".{target.name}-", delete=False) as temporary:
            staged = Path(temporary.name)
        try:
            with httpx.stream("GET", url, follow_redirects=True, timeout=60) as response:
                response.raise_for_status()
                with staged.open("wb") as output:
                    for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                        output.write(chunk)
            if _sha256(staged) != expected_hash:
                raise ConfigurationError(f"Kokoro 文件校验失败：{relative_path}")
            staged.replace(target)
        except httpx.HTTPError as exc:
            raise ConfigurationError(f"Kokoro 文件下载失败：{relative_path}。{exc}") from exc
        finally:
            staged.unlink(missing_ok=True)


def main() -> None:
    if not (3, 10) <= sys.version_info[:2] <= (3, 12):
        raise SystemExit("Kokoro 运行时需要 Python 3.10–3.12，请运行 ./scripts/bootstrap.sh。")
    settings = get_settings()
    binaries = resolve_media_binaries(settings, allow_download=True)
    print(f"ffmpeg:  {binaries.ffmpeg}")
    print(f"ffprobe: {binaries.ffprobe}")
    _install_packages()
    _install_model_assets()
    check_runtime(settings.tts_voice)
    print(f"Kokoro:  {PACKAGE_DIR}")
    print(f"Voices:  {', '.join(Path(name).stem for name in MODEL_ASSETS if name.startswith('voices/'))}")


if __name__ == "__main__":
    main()

