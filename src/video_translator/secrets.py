"""Store reusable API secrets in the macOS login Keychain."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import uuid

from .errors import ConfigurationError, PipelineError


SECRET_REF_PATTERN = re.compile(r"^[a-f0-9]{32}$")
KEYCHAIN_ACCOUNT = "video-translator"
KEYCHAIN_SERVICE_PREFIX = "video-translator.template."


class KeychainSecretStore:
    """A write/read-by-reference wrapper that never exposes secrets via API."""

    def _security(self) -> str:
        executable = shutil.which("security")
        if sys.platform != "darwin" or not executable:
            raise ConfigurationError(
                "保存模板 API Key 目前需要 macOS Keychain。"
            )
        return executable

    @staticmethod
    def _service(reference: str) -> str:
        if not SECRET_REF_PATTERN.fullmatch(reference):
            raise ConfigurationError("API Key 引用无效。")
        return f"{KEYCHAIN_SERVICE_PREFIX}{reference}"

    def save(
        self,
        value: str,
        *,
        reference: str | None = None,
    ) -> str:
        secret = value.strip()
        if not secret:
            raise ConfigurationError("API Key 不能为空。")
        selected = reference or uuid.uuid4().hex
        service = self._service(selected)
        result = subprocess.run(
            [
                self._security(),
                "add-generic-password",
                "-U",
                "-a",
                KEYCHAIN_ACCOUNT,
                "-s",
                service,
                "-w",
                secret,
            ],
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            raise PipelineError(
                "无法把 API Key 写入 macOS Keychain："
                f"{result.stderr.strip()[-500:]}"
            )
        return selected

    def read(self, reference: str) -> str:
        service = self._service(reference)
        result = subprocess.run(
            [
                self._security(),
                "find-generic-password",
                "-a",
                KEYCHAIN_ACCOUNT,
                "-s",
                service,
                "-w",
            ],
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        value = result.stdout.strip()
        if result.returncode != 0 or not value:
            raise ConfigurationError(
                "模板引用的 API Key 不存在或 Keychain 无权访问；"
                "请重新填写并保存模板。"
            )
        return value

    def delete(self, reference: str) -> None:
        service = self._service(reference)
        result = subprocess.run(
            [
                self._security(),
                "delete-generic-password",
                "-a",
                KEYCHAIN_ACCOUNT,
                "-s",
                service,
            ],
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        if result.returncode not in {0, 44}:
            raise PipelineError(
                "无法从 macOS Keychain 删除 API Key："
                f"{result.stderr.strip()[-500:]}"
            )
