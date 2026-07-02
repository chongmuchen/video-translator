from pathlib import Path

from fastapi.testclient import TestClient

from video_translator.api import create_app
from video_translator.models import JobStatus, PipelineOptions
from video_translator.pipeline.stepwise import PipelineStep
from video_translator.settings import Settings


def test_local_console_lists_and_creates_staged_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data"))
    manager = app.state.manager
    submitted = {}

    def fake_submit_step(job_id, step, *, force=False, settings=None):
        submitted.update(
            {
                "job_id": job_id,
                "step": step,
                "force": force,
                "settings": settings,
            }
        )
        return manager.store.get(job_id)

    monkeypatch.setattr(manager, "submit_step", fake_submit_step)
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "本地流水线控制台" in page.text

        response = client.post(
            "/api/jobs/staged",
            json={
                "url": "https://www.youtube.com/watch?v=example",
                "options": {"target_language": "简体中文"},
                "settings": {
                    "max_download_height": 720,
                    "download_backend": "native",
                },
            },
        )
        assert response.status_code == 202
        created = response.json()
        assert created["next_step"] == "download"
        assert created["directory_name"].endswith(f"--{created['id']}")
        assert created["directory_path"].endswith(created["directory_name"])
        assert created["manifest_path"].endswith("manifest.json")
        assert created["log_path"].endswith("pipeline.log")
        assert submitted["step"] == PipelineStep.download
        assert submitted["settings"].max_download_height == 720
        assert submitted["settings"].download_backend == "native"

        jobs = client.get("/api/jobs").json()
        assert [job["id"] for job in jobs] == [created["id"]]


def test_step_endpoint_updates_options_and_runtime_settings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data"))
    manager = app.state.manager
    manifest = manager.store.create(
        "https://youtu.be/example",
        PipelineOptions(),
    )
    manifest.completed_steps = ["download", "extract"]
    manifest.status = JobStatus.extracted
    manager.store.save(manifest)
    submitted = {}

    def fake_submit_step(job_id, step, *, force=False, settings=None):
        submitted.update(
            {
                "job_id": job_id,
                "step": step,
                "force": force,
                "settings": settings,
            }
        )
        return manager.store.get(job_id)

    monkeypatch.setattr(manager, "submit_step", fake_submit_step)
    with TestClient(app) as client:
        response = client.post(
            f"/api/jobs/{manifest.id}/steps/transcribe",
            json={
                "force": True,
                "options": {"source_language": "en"},
                "settings": {
                    "asr_backend": "mlx_whisper",
                    "asr_model": "small.en",
                },
            },
        )
        assert response.status_code == 202
        restored = manager.store.get(manifest.id)
        assert restored.options.source_language == "en"
        assert submitted["step"] == PipelineStep.transcribe
        assert submitted["force"] is True
        assert submitted["settings"].asr_backend == "mlx_whisper"
        assert submitted["settings"].asr_model == "small.en"


def test_automated_endpoint_runs_all_steps_with_one_settings_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data"))
    manager = app.state.manager
    submitted = {}
    monkeypatch.setattr(
        app.state.secret_store,
        "read",
        lambda reference: (
            "resolved-secret"
            if reference == "c" * 32
            else ""
        ),
    )

    def fake_submit_existing(manifest, *, settings=None, metadata=None):
        submitted.update(
            {
                "source": manifest.source,
                "options": manifest.options,
                "settings": settings,
                "metadata": metadata,
            }
        )
        manifest.metadata.update(metadata or {})
        manager.store.save(manifest)
        return manifest

    monkeypatch.setattr(manager, "submit_existing", fake_submit_existing)
    with TestClient(app) as client:
        response = client.post(
            "/api/jobs/automated",
            json={
                "url": "https://www.youtube.com/watch?v=example",
                "options": {
                    "source_language": "en",
                    "target_language": "简体中文",
                    "duck_original_audio": True,
                },
                "settings": {
                    "asr_backend": "mlx_whisper",
                    "asr_model": "small.en",
                    "translator_provider": "codex_cli",
                    "tts_provider": "edge",
                    "tts_http_api_key": "must-not-be-saved",
                    "max_tempo_factor": 1.8,
                },
                "translator_api_key_ref": "c" * 32,
            },
        )

    assert response.status_code == 202
    assert submitted["options"].source_language == "en"
    assert submitted["settings"].asr_backend == "mlx_whisper"
    assert submitted["settings"].translator_provider == "codex_cli"
    assert submitted["settings"].translator_api_key == "resolved-secret"
    assert submitted["settings"].max_tempo_factor == 1.8
    restored = manager.store.get(response.json()["id"])
    assert restored.metadata["run_mode"] == "automated"
    assert restored.metadata["runtime_settings"]["asr_model"] == "small.en"
    assert "tts_http_api_key" not in restored.metadata["runtime_settings"]


def test_api_key_template_uses_keychain_reference(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data"))
    store = app.state.secret_store
    saved = {}

    def fake_save(value, *, reference=None):
        saved["value"] = value
        saved["reference"] = reference
        return "b" * 32

    monkeypatch.setattr(store, "save", fake_save)
    with TestClient(app) as client:
        response = client.post(
            "/api/secrets/translator-api-key",
            json={"value": "cloud-secret"},
        )

    assert response.status_code == 200
    assert response.json()["ref"] == "b" * 32
    assert saved["value"] == "cloud-secret"


def test_book_import_and_list_api(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data"))
    with TestClient(app) as client:
        response = client.post(
            "/api/books/import",
            files={
                "file": (
                    "Example Book.epub",
                    b"epub-placeholder",
                    "application/epub+zip",
                )
            },
            data={"output_mode": "bilingual"},
        )
        books = client.get("/api/books").json()

    assert response.status_code == 201
    assert response.json()["title"] == "Example Book"
    assert response.json()["output_mode"] == "bilingual"
    assert books[0]["id"] == response.json()["id"]
    assert "source_path" not in books[0]


def test_book_import_rejects_paper_layout_for_epub(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data"))
    with TestClient(app) as client:
        response = client.post(
            "/api/books/import",
            files={
                "file": (
                    "Example Book.epub",
                    b"epub-placeholder",
                    "application/epub+zip",
                )
            },
            data={"output_mode": "paper_reference"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "论文/专业 PDF 排版模式仅支持 PDF。"


def test_book_import_accepts_professional_pdf_mode(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data"))
    with TestClient(app) as client:
        response = client.post(
            "/api/books/import",
            files={
                "file": (
                    "paper.pdf",
                    b"%PDF-1.4 placeholder",
                    "application/pdf",
                )
            },
            data={"output_mode": "pdf2zh_bing_mono"},
        )

    assert response.status_code == 201
    assert response.json()["output_mode"] == "pdf2zh_bing_mono"


def test_segments_preview_is_limited(tmp_path: Path) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data"))
    manager = app.state.manager
    manifest = manager.store.create(
        "https://youtu.be/example",
        PipelineOptions(),
    )
    manager.store.write_segments(
        manifest,
        [
            {
                "index": index,
                "start": float(index),
                "end": float(index + 1),
                "source_text": f"line {index}",
            }
            for index in range(3)
        ],
    )

    with TestClient(app) as client:
        response = client.get(
            f"/api/jobs/{manifest.id}/segments?limit=2"
        )
        assert response.status_code == 200
        assert response.json()["total"] == 3
        assert len(response.json()["items"]) == 2
