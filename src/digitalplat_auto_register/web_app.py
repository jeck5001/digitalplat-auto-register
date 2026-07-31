"""Web management console for one-at-a-time DigitalPlat registrations."""

import argparse
import asyncio
import json
import os
import re
import tempfile
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, HTTPException, Response, status
from fastapi.responses import HTMLResponse

from .core.registrar import register_with_defaults
from .core.result import StepResult

DEFAULT_REFERRAL_CODE = "4qn8iw8r1o"
DEFAULT_JOBS_PATH = "/app/data/jobs.json"
MAX_JOB_HISTORY = 30
RUNNING_INTERRUPTED_MESSAGE = "Registration stopped because the service restarted."
_SENSITIVE_TEXT = re.compile(
    r"(?i)(bearer\s+|(?:token|password|passcode|verification[ _-]?code|"
    r"proxy(?:[ _-]?(?:password|credential))?|authorization)\s*[:=]\s*)"
    r"[^\s,;]+"
)
_AUTHORIZATION_BEARER = re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+[^\s,;]+")
_OPAQUE_TOKEN = re.compile(r"\b(?:td_|eyJ)[A-Za-z0-9._-]{12,}\b")
_URL_CREDENTIAL = re.compile(r"(?i)(https?://[^\s:/]+:)[^@\s/]+@")


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_text(value: Any, limit: int = 1000) -> Optional[str]:
    """Keep operational messages useful without exposing credential-like values."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = _AUTHORIZATION_BEARER.sub("Authorization: Bearer [redacted]", text)
    text = _SENSITIVE_TEXT.sub(r"\1[redacted]", text)
    text = _URL_CREDENTIAL.sub(r"\1[redacted]@", text)
    return _OPAQUE_TOKEN.sub("[redacted]", text)[:limit]


def _safe_duration(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value), 3)
    return None


def _safe_step(step: Any) -> Dict[str, Any]:
    source: Mapping[str, Any]
    if isinstance(step, Mapping):
        source = step
    else:
        source = {
            "name": getattr(step, "name", "registration_step"),
            "success": getattr(step, "success", False),
            "duration": getattr(step, "duration", None),
            "message": getattr(step, "message", None),
        }
    return {
        "name": _safe_text(source.get("name"), limit=128) or "registration_step",
        "success": bool(source.get("success")),
        "duration": _safe_duration(source.get("duration")),
        "message": _safe_text(source.get("message")),
    }


def _safe_result(result: Any) -> Dict[str, Any]:
    status = getattr(result, "registration_status", None)
    return {
        "success": bool(getattr(result, "success", False)),
        "username": _safe_text(getattr(result, "username", None), limit=256),
        "email": _safe_text(getattr(result, "email", None), limit=320),
        "status": _safe_text(getattr(status, "value", status), limit=64),
        "duration": _safe_duration(getattr(result, "total_duration", None)),
        "error_stage": _safe_text(getattr(result, "error_stage", None), limit=128),
    }


@dataclass
class RegistrationJob:
    """The deliberately limited, safe-to-persist state for one request."""

    id: str
    referral_code: str = DEFAULT_REFERRAL_CODE
    status: str = "running"
    created_at: str = field(default_factory=_timestamp)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    steps: List[Dict[str, Any]] = field(default_factory=list)
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    task: Optional[asyncio.Task] = field(default=None, repr=False)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "referral_code": DEFAULT_REFERRAL_CODE,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "steps": [_safe_step(step) for step in self.steps],
            "result": RegistrationManager._safe_result_snapshot(self.result),
            "error": _safe_text(self.error),
        }


class RegistrationManager:
    """Serialize registrations and atomically retain safe job snapshots."""

    def __init__(self, data_path: Optional[Path] = None) -> None:
        self._data_path = Path(data_path or os.getenv("JOBS_PATH", DEFAULT_JOBS_PATH))
        self._jobs: "OrderedDict[str, RegistrationJob]" = OrderedDict()
        self._active_job_id: Optional[str] = None
        self._lock = asyncio.Lock()
        self._loaded = False

    async def load(self) -> None:
        """Load prior jobs and close any job interrupted by a process restart."""
        async with self._lock:
            if self._loaded:
                return
            self._loaded = True
            changed = False
            if self._data_path.exists():
                try:
                    payload = json.loads(self._data_path.read_text(encoding="utf-8"))
                    raw_jobs = (
                        payload.get("jobs", []) if isinstance(payload, dict) else []
                    )
                except (OSError, ValueError):
                    raw_jobs = []
                for raw in raw_jobs:
                    job = self._job_from_snapshot(raw)
                    if job is None:
                        continue
                    if job.status == "running":
                        job.status = "failed"
                        job.finished_at = _timestamp()
                        job.error = RUNNING_INTERRUPTED_MESSAGE
                        changed = True
                    self._jobs[job.id] = job
            self._trim_history()
            if changed:
                self._persist_locked()

    async def start(self) -> RegistrationJob:
        async with self._lock:
            if self._active_job_id:
                raise RuntimeError("A registration is already running")
            job = RegistrationJob(id=uuid4().hex[:12])
            self._jobs[job.id] = job
            self._active_job_id = job.id
            self._trim_history()
            self._persist_locked()
            job.task = asyncio.create_task(self._run(job))
            return job

    def get(self, job_id: str) -> Optional[RegistrationJob]:
        return self._jobs.get(job_id)

    def overview(self) -> Dict[str, Any]:
        jobs = [job.snapshot() for job in reversed(self._jobs.values())]
        return {
            "active_job_id": self._active_job_id,
            "jobs": jobs,
            "total_jobs": len(jobs),
            "successful_jobs": sum(job["status"] == "succeeded" for job in jobs),
        }

    async def _run(self, job: RegistrationJob) -> None:
        job.started_at = _timestamp()
        await self._persist()

        def on_step_complete(step: StepResult) -> None:
            job.steps.append(_safe_step(step))
            asyncio.create_task(self._persist())

        try:
            result = await register_with_defaults(
                referral_code=DEFAULT_REFERRAL_CODE,
                on_step_complete=on_step_complete,
            )
            job.result = _safe_result(result)
            job.error = _safe_text(getattr(result, "error", None))
            job.status = "succeeded" if result.success else "failed"
        except Exception as error:
            job.error = _safe_text(error)
            job.status = "failed"
        finally:
            job.finished_at = _timestamp()
            self._active_job_id = None
            await self._persist()

    async def _persist(self) -> None:
        async with self._lock:
            self._persist_locked()

    def _persist_locked(self) -> None:
        self._data_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "jobs": [job.snapshot() for job in self._jobs.values()],
        }
        fd, temporary_path = tempfile.mkstemp(
            prefix=".jobs-", suffix=".json", dir=str(self._data_path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self._data_path)
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)

    def _job_from_snapshot(self, raw: Any) -> Optional[RegistrationJob]:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("id"), str):
            return None
        status = raw.get("status")
        if status not in {"running", "succeeded", "failed"}:
            return None
        steps = raw.get("steps") if isinstance(raw.get("steps"), list) else []
        result = raw.get("result") if isinstance(raw.get("result"), Mapping) else None
        return RegistrationJob(
            id=raw["id"][:128],
            status=status,
            created_at=_safe_text(raw.get("created_at"), limit=64) or _timestamp(),
            started_at=_safe_text(raw.get("started_at"), limit=64),
            finished_at=_safe_text(raw.get("finished_at"), limit=64),
            steps=[_safe_step(step) for step in steps],
            result=self._safe_result_snapshot(result),
            error=_safe_text(raw.get("error")),
        )

    @staticmethod
    def _safe_result_snapshot(
        result: Optional[Mapping[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if result is None:
            return None
        return {
            "success": bool(result.get("success")),
            "username": _safe_text(result.get("username"), limit=256),
            "email": _safe_text(result.get("email"), limit=320),
            "status": _safe_text(result.get("status"), limit=64),
            "duration": _safe_duration(result.get("duration")),
            "error_stage": _safe_text(result.get("error_stage"), limit=128),
        }

    def _trim_history(self) -> None:
        while len(self._jobs) > MAX_JOB_HISTORY:
            oldest_id, oldest = next(iter(self._jobs.items()))
            if oldest.id == self._active_job_id:
                return
            self._jobs.pop(oldest_id)


def create_app(manager: Optional[RegistrationManager] = None) -> FastAPI:
    registration_manager = manager or RegistrationManager()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await registration_manager.load()
        app.state.registration_manager = registration_manager
        yield

    app = FastAPI(
        title="DigitalPlat Register Console",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> str:
        return DASHBOARD_HTML

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        return {
            "ok": True,
            "active_job_id": registration_manager.overview()["active_job_id"],
        }

    @app.get("/api/overview")
    async def overview() -> Dict[str, Any]:
        return registration_manager.overview()

    @app.get("/api/jobs/{job_id}")
    async def job_detail(job_id: str) -> Dict[str, Any]:
        job = registration_manager.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Registration job not found")
        return job.snapshot()

    @app.post("/api/jobs", status_code=status.HTTP_202_ACCEPTED)
    async def start_registration(response: Response) -> Dict[str, Any]:
        try:
            job = await registration_manager.start()
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        response.headers["Location"] = f"/api/jobs/{job.id}"
        return job.snapshot()

    return app


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the DigitalPlat web console")
    parser.add_argument("--host", default=os.getenv("WEB_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("WEB_PORT", "8400")))
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DigitalPlat 控制台</title>
  <style>
    :root { --ink:#17201e; --muted:#69726e; --line:#d7ddd8; --paper:#f6f7f3; --panel:#ffffff; --green:#13795b; --red:#b63b32; --amber:#a56918; --teal:#016d79; }
    * { box-sizing:border-box; }
    body { margin:0; color:var(--ink); background:var(--paper); font-family:"Avenir Next","PingFang SC","Noto Sans CJK SC",sans-serif; }
    main { max-width:1120px; margin:0 auto; padding:32px 24px 48px; }
    header { display:flex; align-items:flex-end; justify-content:space-between; gap:24px; border-bottom:2px solid var(--ink); padding-bottom:18px; }
    h1 { margin:0; font-size:30px; font-weight:650; letter-spacing:0; }
    .subtitle { color:var(--muted); font-size:14px; }
    .metrics { display:grid; grid-template-columns:repeat(3,1fr); gap:1px; background:var(--line); border:1px solid var(--line); margin:24px 0; }
    .metric { background:var(--panel); padding:18px; min-height:94px; }
    .metric-label { color:var(--muted); font-size:12px; }
    .metric-value { font-size:30px; margin-top:8px; font-variant-numeric:tabular-nums; }
    .workspace { display:grid; grid-template-columns:minmax(260px,340px) 1fr; gap:24px; align-items:start; }
    .tool, .history { background:var(--panel); border:1px solid var(--line); }
    .tool { padding:22px; }
    h2 { font-size:17px; margin:0 0 18px; }
    label { display:block; color:var(--muted); font-size:13px; margin-bottom:8px; }
    input { width:100%; border:1px solid #abb5af; background:#fff; border-radius:4px; padding:11px 12px; color:var(--ink); font:inherit; }
    button { width:100%; border:0; border-radius:4px; padding:12px 14px; background:var(--green); color:#fff; font:inherit; font-weight:650; cursor:pointer; margin-top:14px; }
    button:disabled { background:#a7b0ab; cursor:not-allowed; }
    .hint { font-size:12px; color:var(--muted); line-height:1.6; margin:14px 0 0; }
    .history-head { display:flex; align-items:center; justify-content:space-between; padding:18px 20px; border-bottom:1px solid var(--line); }
    .history-head h2 { margin:0; }
    .state { font-size:12px; color:var(--muted); }
    .empty { padding:34px 20px; color:var(--muted); text-align:center; }
    .job { padding:16px 20px; border-bottom:1px solid var(--line); }
    .job:last-child { border-bottom:0; }
    .job-top { display:flex; align-items:center; justify-content:space-between; gap:12px; }
    .job-id { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:13px; }
    .badge { padding:4px 8px; border-radius:999px; font-size:12px; font-weight:650; white-space:nowrap; }
    .running { background:#e0f1f1; color:var(--teal); } .succeeded { background:#def3e8; color:var(--green); } .failed { background:#f8e2df; color:var(--red); }
    .job-meta { color:var(--muted); font-size:12px; margin-top:8px; }
    .steps { display:flex; flex-wrap:wrap; gap:6px; margin-top:12px; }
    .step { border:1px solid var(--line); padding:4px 7px; color:var(--muted); font-size:12px; }
    .step.ok { border-color:#8dc8ad; color:var(--green); } .step.no { border-color:#df9991; color:var(--red); }
    .failure { color:var(--red); font-size:13px; margin-top:10px; white-space:pre-wrap; }
    @media (max-width:760px) { main { padding:22px 14px 34px; } header { align-items:flex-start; flex-direction:column; gap:8px; } .metrics { grid-template-columns:1fr; } .workspace { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <main>
    <header><div><h1>DigitalPlat 控制台</h1><div class="subtitle">注册任务与运行状态</div></div><div id="updated" class="subtitle">正在连接</div></header>
    <section class="metrics" aria-label="任务概览"><div class="metric"><div class="metric-label">运行中</div><div id="active-count" class="metric-value">0</div></div><div class="metric"><div class="metric-label">本次运行任务</div><div id="total-count" class="metric-value">0</div></div><div class="metric"><div class="metric-label">成功</div><div id="success-count" class="metric-value">0</div></div></section>
    <section class="workspace"><section class="tool"><h2>新建注册</h2><div class="hint">邀请码：4qn8iw8r1o</div><button id="start-button" type="button">开始注册</button><p id="start-message" class="hint">每次任务自动生成注册资料，同一时间只运行一个任务。</p></section><section class="history"><div class="history-head"><h2>任务记录</h2><span id="active-state" class="state">无活动任务</span></div><div id="jobs"><div class="empty">尚未创建任务</div></div></section></section>
  </main>
  <script>
    const startButton = document.getElementById('start-button');
    const message = document.getElementById('start-message');
    const jobsElement = document.getElementById('jobs');
    const labels = { running:'运行中', succeeded:'成功', failed:'失败' };
    const text = value => value || '-';
    function renderJobs(jobs) {
      jobsElement.replaceChildren();
      if (!jobs.length) { jobsElement.innerHTML = '<div class="empty">尚未创建任务</div>'; return; }
      jobs.forEach(job => {
        const item = document.createElement('article'); item.className = 'job';
        const top = document.createElement('div'); top.className = 'job-top';
        const id = document.createElement('span'); id.className = 'job-id'; id.textContent = job.id;
        const badge = document.createElement('span'); badge.className = `badge ${job.status}`; badge.textContent = labels[job.status] || job.status;
        top.append(id, badge); item.append(top);
        const meta = document.createElement('div'); meta.className = 'job-meta';
        const outcome = job.result ? `${text(job.result.username)}  ${text(job.result.email)}  |  ${text(job.result.duration)} 秒` : '等待流程返回';
        meta.textContent = `${job.created_at}  |  ${outcome}`; item.append(meta);
        if (job.steps.length) { const steps = document.createElement('div'); steps.className = 'steps'; job.steps.forEach(step => { const chip = document.createElement('span'); chip.className = `step ${step.success ? 'ok' : 'no'}`; chip.textContent = step.name; steps.append(chip); }); item.append(steps); }
        if (job.error) { const error = document.createElement('div'); error.className = 'failure'; error.textContent = job.error; item.append(error); }
        jobsElement.append(item);
      });
    }
    async function refresh() {
      try { const response = await fetch('/api/overview'); const data = await response.json(); const active = Boolean(data.active_job_id); document.getElementById('active-count').textContent = active ? '1' : '0'; document.getElementById('total-count').textContent = data.total_jobs; document.getElementById('success-count').textContent = data.successful_jobs; document.getElementById('active-state').textContent = active ? '任务正在执行' : '无活动任务'; startButton.disabled = active; document.getElementById('updated').textContent = `已更新 ${new Date().toLocaleTimeString()}`; renderJobs(data.jobs); } catch (error) { document.getElementById('updated').textContent = '服务暂不可用'; }
    }
    startButton.addEventListener('click', async () => { startButton.disabled = true; message.textContent = '正在创建任务...'; try { const response = await fetch('/api/jobs', { method:'POST' }); const data = await response.json(); if (!response.ok) throw new Error(data.detail || '无法创建任务'); message.textContent = `任务 ${data.id} 已开始`; } catch (error) { message.textContent = error.message; startButton.disabled = false; } await refresh(); });
    refresh(); setInterval(refresh, 2000);
  </script>
</body>
</html>"""
