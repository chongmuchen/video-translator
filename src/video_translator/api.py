"""FastAPI application."""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
from pathlib import Path
import shutil
import uuid

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from .errors import InvalidSourceError, VideoTranslatorError
from .books.manager import BookManager
from .books.models import BookStep, BookStepRequest
from .books.ocr import check_ocr_environment
from .books.pdf import PAPER_OUTPUT_MODES
from .books.pipeline import BookTranslationPipeline
from .books.professional_pdf import PROFESSIONAL_PDF_OUTPUT_MODES
from .manager import JobManager
from .models import (
    AutomatedJobCreateRequest,
    JobCreateRequest,
    JobManifest,
    JobStatus,
    SecretSaveRequest,
    StagedJobCreateRequest,
    StepRunRequest,
)
from .paper_podcast.manager import PaperPodcastManager
from .paper_podcast.models import (
    PaperPodcastRequest,
    PaperPodcastStep,
)
from .paper_podcast.pipeline import PaperPodcastPipeline
from .pipeline.downloader import validate_remote_url
from .pipeline.stepwise import PipelineStep, STEP_ORDER
from .settings import Settings, get_settings
from .secrets import KeychainSecretStore


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or get_settings()
    manager = JobManager(runtime_settings)
    book_manager = BookManager(runtime_settings)
    paper_podcast_manager = PaperPodcastManager(runtime_settings)
    secret_store = KeychainSecretStore()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        manager.shutdown()
        book_manager.shutdown()
        paper_podcast_manager.shutdown()

    app = FastAPI(
        title="Video Translator",
        version="0.1.0",
        description="视频转写、翻译、中文配音与封装",
        lifespan=lifespan,
    )
    app.state.manager = manager
    app.state.book_manager = book_manager
    app.state.paper_podcast_manager = paper_podcast_manager
    app.state.secret_store = secret_store

    def job_payload(manifest: JobManifest) -> dict:
        payload = manifest.public_dict()
        job_dir = manager.store.job_dir(manifest.id)
        payload["directory_name"] = job_dir.name
        payload["directory_path"] = str(job_dir)
        payload["manifest_path"] = str(job_dir / "manifest.json")
        payload["log_path"] = str(job_dir / "pipeline.log")
        payload["running"] = manager.is_running(manifest.id)
        payload["next_step"] = next(
            (
                step.value
                for step in STEP_ORDER
                if step.value not in manifest.completed_steps
            ),
            None,
        )
        artifact_paths = {}
        for key, value in {
            "source": manifest.source_path,
            "audio": manifest.audio_path,
            "segments": manifest.segments_path,
            "subtitle": manifest.subtitle_path,
            "dub_audio": manifest.dub_audio_path,
            "output": manifest.output_path,
        }.items():
            if value and Path(value).is_file():
                artifact_paths[key] = str(Path(value))
        payload["artifact_paths"] = artifact_paths
        if payload.get("download_ready"):
            payload["download_url"] = f"/api/jobs/{manifest.id}/download"
        payload["artifacts"] = {
            "source": bool(
                manifest.source_path and Path(manifest.source_path).is_file()
            ),
            "audio": bool(
                manifest.audio_path and Path(manifest.audio_path).is_file()
            ),
            "segments": bool(
                manifest.segments_path
                and Path(manifest.segments_path).is_file()
            ),
            "subtitle": bool(
                manifest.subtitle_path
                and Path(manifest.subtitle_path).is_file()
            ),
            "dub_audio": bool(
                manifest.dub_audio_path
                and Path(manifest.dub_audio_path).is_file()
            ),
            "output": bool(
                manifest.output_path and Path(manifest.output_path).is_file()
            ),
        }
        payload["step_progress"] = job_step_progress(manifest)
        return payload

    def segment_counts(manifest: JobManifest) -> dict[str, int] | None:
        if not manifest.segments_path:
            return None
        path = Path(manifest.segments_path)
        if not path.is_file():
            return None
        try:
            segments = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        total = len(segments)
        translated = sum(
            1
            for item in segments
            if str(item.get("translated_text") or "").strip()
        )
        synthesized = sum(
            1
            for item in segments
            if str(item.get("tts_file") or "").strip()
        )
        unclear = sum(1 for item in segments if item.get("asr_unclear"))
        return {
            "total": total,
            "translated": translated,
            "translation_pending": max(0, total - translated),
            "synthesized": synthesized,
            "synthesis_pending": max(0, total - synthesized),
            "unclear": unclear,
        }

    def job_step_progress(manifest: JobManifest) -> dict:
        counts = segment_counts(manifest)
        return {
            "download": {
                "done": bool(
                    manifest.source_path and Path(manifest.source_path).is_file()
                ),
            },
            "extract": {
                "done": bool(
                    manifest.audio_path and Path(manifest.audio_path).is_file()
                ),
            },
            "transcribe": {
                "done": bool(
                    manifest.segments_path
                    and Path(manifest.segments_path).is_file()
                ),
                **({"segments": counts["total"]} if counts else {}),
            },
            "translate": {
                "done": "translate" in manifest.completed_steps,
                **(
                    {
                        "translated": counts["translated"],
                        "pending": counts["translation_pending"],
                        "total": counts["total"],
                        "unclear": counts["unclear"],
                    }
                    if counts
                    else {}
                ),
            },
            "synthesize": {
                "done": "synthesize" in manifest.completed_steps,
                **(
                    {
                        "synthesized": counts["synthesized"],
                        "pending": counts["synthesis_pending"],
                        "total": counts["total"],
                    }
                    if counts
                    else {}
                ),
            },
            "align": {
                "done": bool(
                    manifest.dub_audio_path
                    and Path(manifest.dub_audio_path).is_file()
                ),
            },
            "mux": {
                "done": bool(
                    manifest.output_path and Path(manifest.output_path).is_file()
                ),
            },
        }

    def settings_for(request_settings) -> Settings:
        updates = request_settings.model_dump(exclude_none=True)
        if updates.get("download_proxy") == "direct":
            updates["download_proxy"] = ""
        return runtime_settings.model_copy(update=updates)

    def settings_with_saved_key(
        request_settings,
        reference: str | None,
    ) -> Settings:
        selected = settings_for(request_settings)
        if reference:
            selected = selected.model_copy(
                update={
                    "translator_api_key": secret_store.read(reference)
                }
            )
        return selected

    def apply_option_updates(
        manifest: JobManifest,
        request: StepRunRequest,
    ) -> None:
        updates = request.options.model_dump(exclude_none=True)
        if not updates:
            return
        manifest.options = manifest.options.model_copy(update=updates)
        manager.store.save(manifest)

    @app.exception_handler(VideoTranslatorError)
    async def handle_application_error(
        _: Request,
        exc: VideoTranslatorError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": str(exc)},
        )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(Path(__file__).parent / "web" / "index.html")

    @app.get("/api/health")
    def health() -> dict:
        ocr = check_ocr_environment(
            runtime_settings,
            required_languages=("eng", "chi_sim"),
        )
        return {
            "status": "ok",
            "storage": "local",
            "data_directory": str(runtime_settings.data_dir),
            "ocr": {
                "available": ocr.available,
                "ocrmypdf": ocr.ocrmypdf,
                "tesseract": ocr.tesseract,
                "ghostscript": ocr.ghostscript,
                "languages": ocr.languages,
                "missing_languages": ocr.missing_languages,
            },
        }

    @app.post("/api/secrets/translator-api-key")
    def save_translator_api_key(request: SecretSaveRequest) -> dict:
        reference = secret_store.save(
            request.value,
            reference=request.reference,
        )
        return {
            "ref": reference,
            "storage": "macOS Keychain",
        }

    @app.delete("/api/secrets/translator-api-key/{reference}")
    def delete_translator_api_key(reference: str) -> dict:
        secret_store.delete(reference)
        return {"deleted": True}

    @app.get("/api/jobs")
    def list_jobs(limit: int = 100) -> list[dict]:
        safe_limit = max(1, min(limit, 500))
        return [
            job_payload(manifest)
            for manifest in manager.store.list(limit=safe_limit)
        ]

    def book_payload(manifest) -> dict:
        payload = manifest.public_dict()
        book_dir = book_manager.store.job_dir(manifest.id)
        payload["directory_name"] = book_dir.name
        payload["directory_path"] = str(book_dir)
        payload["manifest_path"] = str(book_dir / "book-manifest.json")
        artifact_paths = {}
        for key, value in {
            "source": manifest.source_path,
            "blocks": manifest.blocks_path,
            "output": manifest.output_path,
            "ocr_pdf": manifest.metadata.get("ocr_source_path"),
            "ocr_text": manifest.metadata.get("ocr_sidecar_path"),
            "ocr_log": manifest.metadata.get("ocr_log_path"),
        }.items():
            if value and Path(value).is_file():
                artifact_paths[key] = str(Path(value))
        payload["artifact_paths"] = artifact_paths
        rendered_outputs = {}
        raw_rendered_outputs = manifest.metadata.get("rendered_outputs", {})
        if isinstance(raw_rendered_outputs, dict):
            for mode, value in raw_rendered_outputs.items():
                if value and Path(value).is_file():
                    rendered_outputs[str(mode)] = str(Path(value))
        payload["rendered_outputs"] = rendered_outputs
        professional_logs = {}
        raw_professional_logs = manifest.metadata.get(
            "professional_pdf_logs",
            {},
        )
        if isinstance(raw_professional_logs, dict):
            for mode, value in raw_professional_logs.items():
                if value and Path(value).is_file():
                    professional_logs[str(mode)] = str(Path(value))
        payload["professional_pdf_logs"] = professional_logs
        payload["running"] = book_manager.is_running(manifest.id)
        payload["download_ready"] = bool(
            manifest.output_path
            and Path(manifest.output_path).is_file()
        )
        payload["next_step"] = (
            None
            if payload["download_ready"]
            else next(
                (
                    step.value
                    for step in BookStep
                    if step.value not in manifest.completed_steps
                ),
                None,
            )
        )
        if payload["download_ready"]:
            payload["download_url"] = f"/api/books/{manifest.id}/download"
        payload["step_progress"] = book_step_progress(manifest)
        payload["layout_warning_count"] = len(
            manifest.metadata.get("layout_warnings", [])
        )
        return payload

    def book_step_progress(manifest) -> dict:
        total = translated = 0
        if manifest.blocks_path and Path(manifest.blocks_path).is_file():
            try:
                blocks = json.loads(
                    Path(manifest.blocks_path).read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                blocks = []
            total = len(blocks)
            translated = sum(
                1
                for item in blocks
                if str(item.get("translated_text") or "").strip()
            )
        return {
            "extract": {
                "done": "extract" in manifest.completed_steps,
                "blocks": total,
                "ocr_used": bool(manifest.metadata.get("ocr_used")),
                "ocr_mode": manifest.metadata.get("ocr_mode"),
                "ocr_languages": manifest.metadata.get("ocr_languages"),
            },
            "translate": {
                "done": "translate" in manifest.completed_steps,
                "translated": translated,
                "pending": max(0, total - translated),
                "total": total,
            },
            "render": {
                "done": bool(
                    manifest.output_path and Path(manifest.output_path).is_file()
                ),
                "layout_warnings": len(
                    manifest.metadata.get("layout_warnings", [])
                ),
            },
        }

    def paper_podcast_payload(manifest) -> dict:
        payload = manifest.public_dict()
        podcast_dir = paper_podcast_manager.store.job_dir(manifest.id)
        payload["directory_name"] = podcast_dir.name
        payload["directory_path"] = str(podcast_dir)
        payload["manifest_path"] = str(
            podcast_dir / "podcast-manifest.json"
        )
        artifact_paths = {}
        for key, value in {
            "source": manifest.source_path,
            "text": manifest.text_path,
            "notes": manifest.notes_path,
            "script_json": manifest.script_json_path,
            "script_markdown": manifest.script_markdown_path,
            "audio": manifest.audio_path,
        }.items():
            if value and Path(value).is_file():
                artifact_paths[key] = str(Path(value))
        payload["artifact_paths"] = artifact_paths
        payload["running"] = paper_podcast_manager.is_running(manifest.id)
        payload["download_ready"] = bool(
            manifest.audio_path and Path(manifest.audio_path).is_file()
        )
        payload["next_step"] = (
            None
            if payload["download_ready"]
            else next(
                (
                    step.value
                    for step in PaperPodcastStep
                    if step.value not in manifest.completed_steps
                ),
                None,
            )
        )
        if payload["download_ready"]:
            payload["download_url"] = (
                f"/api/paper-podcasts/{manifest.id}/download"
            )
        payload["step_progress"] = {
            "extract": {
                "done": "extract" in manifest.completed_steps,
                "blocks": manifest.metadata.get("block_count", 0),
                "pages": manifest.metadata.get("page_count"),
            },
            "script": {
                "done": "script" in manifest.completed_steps,
                "lines": manifest.metadata.get("script_line_count", 0),
                "note_chunks": manifest.metadata.get("note_chunk_count", 0),
            },
            "synthesize": {
                "done": payload["download_ready"],
                "clips": manifest.metadata.get("audio_clip_count", 0),
            },
        }
        return payload

    @app.get("/api/books")
    def list_books() -> list[dict]:
        return [
            book_payload(manifest)
            for manifest in book_manager.store.list()
        ]

    @app.get("/api/paper-podcasts")
    def list_paper_podcasts() -> list[dict]:
        return [
            paper_podcast_payload(manifest)
            for manifest in paper_podcast_manager.store.list()
        ]

    @app.post(
        "/api/paper-podcasts/import",
        status_code=status.HTTP_201_CREATED,
    )
    def import_paper_podcast(
        file: UploadFile = File(...),
        style: str = Form("deep_dive"),
        duration_minutes: int = Form(8),
    ) -> dict:
        suffix = Path(file.filename or "").suffix.lower()
        if suffix != ".pdf":
            raise HTTPException(
                status_code=400,
                detail="论文播客第一版只支持 PDF。",
            )
        if style not in {"deep_dive", "narration"}:
            raise HTTPException(status_code=400, detail="播客风格无效。")
        safe_duration = max(2, min(int(duration_minutes), 60))
        upload_dir = runtime_settings.runtime_dir / "paper-podcast-uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        temporary = upload_dir / f"{uuid.uuid4().hex}.pdf"
        try:
            with temporary.open("wb") as target:
                shutil.copyfileobj(file.file, target)
            if temporary.stat().st_size > 500 * 1024 * 1024:
                raise HTTPException(
                    status_code=413,
                    detail="论文 PDF 不能超过 500 MB。",
                )
            manifest = PaperPodcastPipeline(
                runtime_settings,
                paper_podcast_manager.store,
            ).import_paper(
                temporary,
                title=Path(file.filename or temporary.name).stem,
                style=style,
                duration_minutes=safe_duration,
            )
            return paper_podcast_payload(manifest)
        finally:
            temporary.unlink(missing_ok=True)

    @app.post(
        "/api/paper-podcasts/{podcast_id}/steps/{step}",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def run_paper_podcast_step(
        podcast_id: str,
        step: PaperPodcastStep,
        request: PaperPodcastRequest,
    ) -> dict:
        try:
            selected_settings = settings_with_saved_key(
                request.settings,
                request.translator_api_key_ref,
            )
            manifest = paper_podcast_manager.submit_step(
                podcast_id,
                step,
                request,
                settings=selected_settings,
            )
            return paper_podcast_payload(manifest)
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail="论文播客任务不存在",
            ) from exc
        except (VideoTranslatorError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/api/paper-podcasts/{podcast_id}/run",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def run_paper_podcast_all(
        podcast_id: str,
        request: PaperPodcastRequest,
    ) -> dict:
        try:
            selected_settings = settings_with_saved_key(
                request.settings,
                request.translator_api_key_ref,
            )
            manifest = paper_podcast_manager.submit_all(
                podcast_id,
                request,
                settings=selected_settings,
            )
            return paper_podcast_payload(manifest)
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail="论文播客任务不存在",
            ) from exc
        except (VideoTranslatorError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/paper-podcasts/{podcast_id}")
    def get_paper_podcast(podcast_id: str) -> dict:
        try:
            return paper_podcast_payload(
                paper_podcast_manager.store.get(podcast_id)
            )
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail="论文播客任务不存在",
            ) from exc

    @app.get("/api/paper-podcasts/{podcast_id}/download")
    def download_paper_podcast(
        podcast_id: str,
        artifact: str = "audio",
    ) -> FileResponse:
        try:
            manifest = paper_podcast_manager.store.get(podcast_id)
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail="论文播客任务不存在",
            ) from exc
        selected = {
            "audio": manifest.audio_path,
            "script": manifest.script_markdown_path,
            "script_json": manifest.script_json_path,
            "text": manifest.text_path,
            "notes": manifest.notes_path,
        }.get(artifact)
        if not selected:
            raise HTTPException(
                status_code=409,
                detail="这个论文播客产物尚未生成",
            )
        path = Path(selected).resolve()
        job_root = paper_podcast_manager.store.job_dir(podcast_id).resolve()
        outputs_root = paper_podcast_manager.store.outputs_dir.resolve()
        if (
            job_root not in path.parents
            and outputs_root not in path.parents
        ) or not path.is_file():
            raise HTTPException(status_code=404, detail="论文播客产物不存在")
        media_type = "audio/mpeg" if path.suffix.lower() == ".mp3" else None
        return FileResponse(path, media_type=media_type, filename=path.name)

    @app.post(
        "/api/books/import",
        status_code=status.HTTP_201_CREATED,
    )
    def import_book(
        file: UploadFile = File(...),
        output_mode: str = Form("translated_only"),
    ) -> dict:
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in {".pdf", ".epub"}:
            raise HTTPException(
                status_code=400,
                detail="只支持 PDF 或 EPUB。",
            )
        output_modes = {
            "translated_only",
            "bilingual",
            *PAPER_OUTPUT_MODES,
            *PROFESSIONAL_PDF_OUTPUT_MODES,
        }
        if output_mode not in output_modes:
            raise HTTPException(status_code=400, detail="输出模式无效。")
        if suffix != ".pdf" and (
            output_mode in PAPER_OUTPUT_MODES
            or output_mode in PROFESSIONAL_PDF_OUTPUT_MODES
        ):
            raise HTTPException(
                status_code=400,
                detail="论文/专业 PDF 排版模式仅支持 PDF。",
            )
        upload_dir = runtime_settings.runtime_dir / "book-uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        temporary = upload_dir / f"{uuid.uuid4().hex}{suffix}"
        try:
            with temporary.open("wb") as target:
                shutil.copyfileobj(file.file, target)
            if temporary.stat().st_size > 500 * 1024 * 1024:
                raise HTTPException(
                    status_code=413,
                    detail="书籍文件不能超过 500 MB。",
                )
            manifest = BookTranslationPipeline(
                runtime_settings,
                book_manager.store,
            ).import_book(
                temporary,
                output_mode=output_mode,
                title=Path(file.filename or temporary.name).stem,
            )
            return book_payload(manifest)
        finally:
            temporary.unlink(missing_ok=True)

    @app.post(
        "/api/books/{book_id}/steps/{step}",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def run_book_step(
        book_id: str,
        step: BookStep,
        request: BookStepRequest,
    ) -> dict:
        try:
            selected_settings = settings_with_saved_key(
                request.settings,
                request.translator_api_key_ref,
            )
            manifest = book_manager.submit_step(
                book_id,
                step,
                request,
                settings=selected_settings,
            )
            return book_payload(manifest)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="书籍任务不存在") from exc
        except (VideoTranslatorError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/api/books/{book_id}/run",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def run_book_all(
        book_id: str,
        request: BookStepRequest,
    ) -> dict:
        try:
            selected_settings = settings_with_saved_key(
                request.settings,
                request.translator_api_key_ref,
            )
            manifest = book_manager.submit_all(
                book_id,
                request,
                settings=selected_settings,
            )
            return book_payload(manifest)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="书籍任务不存在") from exc
        except (VideoTranslatorError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/books/{book_id}")
    def get_book(book_id: str) -> dict:
        try:
            return book_payload(book_manager.store.get(book_id))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="书籍任务不存在") from exc

    @app.get("/api/books/{book_id}/download")
    def download_book(book_id: str, mode: str | None = None) -> FileResponse:
        try:
            manifest = book_manager.store.get(book_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="书籍任务不存在") from exc
        requested = None
        if mode:
            raw_outputs = manifest.metadata.get("rendered_outputs", {})
            requested = (
                raw_outputs.get(mode)
                if isinstance(raw_outputs, dict)
                else None
            )
            if not requested:
                raise HTTPException(
                    status_code=404,
                    detail="这个排版版本尚未生成",
                )
        output_path = requested or manifest.output_path
        if not output_path:
            raise HTTPException(status_code=409, detail="书籍尚未排版完成")
        path = Path(output_path).resolve()
        if (
            book_manager.store.outputs_dir.resolve() not in path.parents
            or not path.is_file()
        ):
            raise HTTPException(status_code=404, detail="书籍输出不存在")
        return FileResponse(path, filename=path.name)

    @app.post("/api/jobs", status_code=status.HTTP_202_ACCEPTED)
    def create_job(request: JobCreateRequest) -> dict:
        try:
            validated = validate_remote_url(request.url, runtime_settings)
        except InvalidSourceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        manifest = manager.submit(validated, request.options)
        return job_payload(manifest)

    @app.post(
        "/api/jobs/staged",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_staged_job(request: StagedJobCreateRequest) -> dict:
        try:
            validated = validate_remote_url(request.url, runtime_settings)
            manifest = manager.create(validated, request.options)
            manager.submit_step(
                manifest.id,
                PipelineStep.download,
                settings=settings_for(request.settings),
            )
        except (InvalidSourceError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return job_payload(manifest)

    @app.post(
        "/api/jobs/automated",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_automated_job(
        request: AutomatedJobCreateRequest,
    ) -> dict:
        try:
            validated = validate_remote_url(request.url, runtime_settings)
            automated_settings = settings_for(request.settings)
            if request.translator_api_key_ref:
                automated_settings = automated_settings.model_copy(
                    update={
                        "translator_api_key": secret_store.read(
                            request.translator_api_key_ref
                        )
                    }
                )
            settings_snapshot = request.settings.model_dump(
                exclude_none=True
            )
            for secret in (
                "translator_api_key",
                "tts_http_api_key",
            ):
                settings_snapshot.pop(secret, None)
            manifest = manager.create(
                validated,
                request.options,
            )
            manifest.metadata.update(
                {
                    "run_mode": "automated",
                    "runtime_settings": settings_snapshot,
                }
            )
            manager.store.save(manifest)
            manager.submit_existing(
                manifest,
                settings=automated_settings,
            )
        except (InvalidSourceError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return job_payload(manifest)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        try:
            manifest = manager.store.get(job_id)
        except (FileNotFoundError, VideoTranslatorError) as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        return job_payload(manifest)

    @app.post(
        "/api/jobs/{job_id}/steps/{step}",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def run_job_step(
        job_id: str,
        step: PipelineStep,
        request: StepRunRequest,
    ) -> dict:
        try:
            manifest = manager.store.get(job_id)
            if manager.is_running(job_id):
                raise HTTPException(
                    status_code=409,
                    detail="任务正在执行，请等待当前步骤完成。",
                )
            selected_index = STEP_ORDER.index(step)
            if selected_index:
                previous = STEP_ORDER[selected_index - 1].value
                if previous not in manifest.completed_steps:
                    raise HTTPException(
                        status_code=409,
                        detail=f"执行 {step.value} 前必须先完成 {previous}。",
                    )
            apply_option_updates(manifest, request)
            manager.submit_step(
                job_id,
                step,
                force=request.force,
                settings=settings_for(request.settings),
            )
        except HTTPException:
            raise
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail="任务不存在",
            ) from exc
        except (VideoTranslatorError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return job_payload(manifest)

    @app.get("/api/jobs/{job_id}/log")
    def get_job_log(job_id: str) -> PlainTextResponse:
        try:
            log_path = manager.store.job_dir(job_id) / "pipeline.log"
        except VideoTranslatorError as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        if not log_path.is_file():
            return PlainTextResponse("")
        content = log_path.read_text(encoding="utf-8", errors="replace")
        return PlainTextResponse(content[-100_000:])

    @app.get("/api/jobs/{job_id}/segments")
    def get_job_segments(job_id: str, limit: int = 50) -> dict:
        try:
            manifest = manager.store.get(job_id)
        except (FileNotFoundError, VideoTranslatorError) as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        if not manifest.segments_path:
            return {"total": 0, "items": []}
        path = Path(manifest.segments_path).resolve()
        job_root = manager.store.job_dir(job_id).resolve()
        if job_root not in path.parents or not path.is_file():
            raise HTTPException(status_code=404, detail="识别结果不存在")
        try:
            items = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=500,
                detail="识别结果无法读取",
            ) from exc
        safe_limit = max(1, min(limit, 500))
        return {"total": len(items), "items": items[:safe_limit]}

    @app.get("/api/jobs/{job_id}/download")
    def download_job(job_id: str) -> FileResponse:
        try:
            manifest = manager.store.get(job_id)
        except (FileNotFoundError, VideoTranslatorError) as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        if manifest.status != JobStatus.completed or not manifest.output_path:
            raise HTTPException(status_code=409, detail="任务尚未完成")
        output = Path(manifest.output_path).resolve()
        outputs_root = runtime_settings.outputs_dir.resolve()
        if outputs_root not in output.parents or not output.is_file():
            raise HTTPException(status_code=404, detail="输出文件不存在")
        media_type = (
            "audio/mp4"
            if output.suffix.lower() in {".m4a", ".mp4a"}
            else "video/mp4"
        )
        return FileResponse(
            output,
            media_type=media_type,
            filename=output.name,
        )

    return app


app = create_app()
