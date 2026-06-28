"""FastAPI application."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from .errors import InvalidSourceError, VideoTranslatorError
from .manager import JobManager
from .models import JobCreateRequest, JobStatus
from .pipeline.downloader import validate_remote_url
from .settings import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or get_settings()
    manager = JobManager(runtime_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        manager.shutdown()

    app = FastAPI(
        title="Video Translator",
        version="0.1.0",
        description="视频转写、翻译、中文配音与封装",
        lifespan=lifespan,
    )
    app.state.manager = manager

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
        return {"status": "ok"}

    @app.post("/api/jobs", status_code=status.HTTP_202_ACCEPTED)
    def create_job(request: JobCreateRequest) -> dict:
        try:
            validated = validate_remote_url(request.url, runtime_settings)
        except InvalidSourceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        manifest = manager.submit(validated, request.options)
        return manifest.public_dict()

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict:
        try:
            manifest = manager.store.get(job_id)
        except (FileNotFoundError, VideoTranslatorError) as exc:
            raise HTTPException(status_code=404, detail="任务不存在") from exc
        return manifest.public_dict()

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
        return FileResponse(
            output,
            media_type="video/mp4",
            filename=output.name,
        )

    return app


app = create_app()

