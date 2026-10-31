from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import math
import os
import re
import tempfile
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from fastapi import FastAPI, HTTPException, UploadFile, File, Request, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict, StrictBool, field_validator, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import CONTEXT_TOKEN_MAX, DATA_DIR, ROOT, RETIRED_IMPLEMENTATION_REVIEW_FIELDS, get_settings, public_settings, save_secrets
from .engine import Engine, ROLES, STAGES, digest
from .idea_materials import render_idea_materials
from .codex_delivery import codex_status, validate_project_dir, local_codex_view
from .exporting import build_export
from .historical_closure import HistoricalClosureError, build_historical_closure, render_historical_closure
from .intelligence import Intelligence, ProviderError, PolicyReviewRequired, is_moderation_error, _http_error_details, _redact_request_body
from .lab import hardware_profile
from .python_environments import validate_python, discover_python_environments, apply_execution_preferences
from .observability import research_map, agent_history_page, handoff_views
from .storage import Store, RunDeletedError, now, uid, redact


class ArchitectureDiagramRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    document_id: str = Field(min_length=1)
    expected_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    retry: StrictBool = False


class RapidContinuationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    snapshot_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    authorize_policy_change: StrictBool
    instruction: str = Field(min_length=4, max_length=2000)


class PerformancePolicyRequest(RapidContinuationRequest):
    metric: str = Field(pattern=r'^val_[A-Za-z0-9_]+$')
    direction: str = Field(pattern='^(minimize|maximize)$')


class RunCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(default='', max_length=160)
    objective: str = Field(min_length=8, max_length=16000)
    mode: str = Field(default='live', pattern='^(live|demo)$')
    publication_target: str = Field(default='', max_length=300)
    reference_ids: list[str] = Field(default_factory=list, max_length=10)
    budget_usd: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    max_calls: int | None = Field(default=150, ge=3, le=1000, strict=True)
    max_rounds: int = Field(default=3, ge=1, le=8)
    discussion_mode: str = Field(default='until_stalled', pattern='^(until_stalled|bounded)$')
    stagnation_rounds: int = Field(default=3, ge=2, le=10)
    max_trials: int = Field(default=12, ge=6, le=100)
    max_research_cycles: int | None = Field(default=None, ge=1)
    architecture_max_revisions: int | None = Field(default=None, ge=1, le=20, strict=True, deprecated=True)
    architecture_stagnation_rounds: int = Field(default=3, ge=2, le=10, strict=True)
    min_relative_improvement: float = Field(default=0.01, ge=0, le=1, allow_inf_nan=False)
    context_tokens: int = Field(default=CONTEXT_TOKEN_MAX, ge=4000, le=CONTEXT_TOKEN_MAX)
    memory_semantic_enabled: StrictBool = False
    memory_lexical_candidates: int = Field(default=64, ge=1, le=10000, strict=True)
    memory_semantic_candidates: int = Field(default=64, ge=1, le=10000, strict=True)
    memory_top_k: int = Field(default=6, ge=1, le=1000, strict=True)
    memory_rrf_k: int = Field(default=60, ge=1, le=10000, strict=True)
    memory_semantic_min_score: float = Field(default=0.2, ge=-1, le=1, allow_inf_nan=False)
    evidence_top_k: int = Field(default=8, ge=2, le=1000, strict=True)
    evidence_max_results: int = Field(default=12, ge=2, le=1000, strict=True)
    evidence_lexical_candidates: int = Field(default=64, ge=1, le=10000, strict=True)
    evidence_semantic_candidates: int = Field(default=64, ge=1, le=10000, strict=True)
    evidence_rrf_k: int = Field(default=60, ge=1, le=10000, strict=True)
    evidence_semantic_enabled: StrictBool = True
    execution_archive_inline_bytes: int = Field(default=65536, ge=1024, le=1048576, strict=True)
    execution_archive_retention_days: int | None = Field(default=None, ge=1, le=36500, strict=True)
    skip_unavailable_papers: StrictBool = False
    ccf_ab_only: StrictBool = False
    experiment_timeout: int = Field(default=120, ge=10, le=7200)
    allow_local_execution: bool = False
    execution_mode: str = Field(default='local', pattern='^(local|docker)$')
    python_executable: str = Field(default='', max_length=2048)
    gpu_index: int | None = Field(default=None, ge=0, le=128, strict=True)
    vram_limit_gb: float | None = Field(default=None, ge=0, le=100000, allow_inf_nan=False, strict=True)
    cpu_limit_cores: int | None = Field(default=None, ge=1, le=4096, strict=True)
    ram_limit_gb: float | None = Field(default=None, gt=0, le=100000, allow_inf_nan=False, strict=True)
    disk_limit_gb: float | None = Field(default=None, gt=0, le=1000000, allow_inf_nan=False, strict=True)
    implementation_backend: str = Field(default='local_codex', pattern='^local_codex$')
    codex_project_dir: str = Field(default=str(ROOT), max_length=2048)
    codex_model: str = Field(default='', max_length=200, pattern=r'^[A-Za-z0-9._:/-]*$')
    codex_reasoning_effort: str = Field(default='', pattern='^(|none|minimal|low|medium|high|xhigh|max|ultra)$')
    codex_timeout_seconds: int = Field(default=1800, ge=60, le=7200)
    codex_max_revisions: int | None = Field(default=3, ge=1, le=6)
    implementation_review_strategy: str = Field(default='acceptance_contract', pattern='^(whole_alignment|acceptance_contract)$')
    implementation_indexed_review_protocol: int = Field(default=1, ge=1, le=1, strict=True)
    implementation_review_max_revisions: int | None = Field(default=2, ge=0, le=20, strict=True)
    implementation_review_max_seconds: int | None = Field(default=14400, ge=1, le=604800, strict=True)
    implementation_review_time_basis: str = Field(default='policy_active_time', pattern='^(per_revision_review|policy_wall_time|policy_active_time)$')
    implementation_review_max_calls: int | None = Field(default=240, ge=1, le=10000, strict=True)

    @model_validator(mode='after')
    def finite_acceptance_budget(self):
        if self.implementation_review_strategy == 'acceptance_contract' and (
                self.implementation_review_max_revisions is None
                or self.implementation_review_time_basis not in {'policy_wall_time', 'policy_active_time'}):
            raise ValueError('验收合同协议需要有限修订轮次及项目级累计时间。')
        return self

    @field_validator('codex_project_dir')
    @classmethod
    def project_directory(cls, value):
        return validate_project_dir(value)

    @field_validator('python_executable')
    @classmethod
    def python_environment(cls, value):
        return validate_python(value)


class MemoryCreate(BaseModel):
    text: str = Field(min_length=1, max_length=100000)
    run_id: str | None = None
    role: str = 'shared'
    metadata: dict = Field(default_factory=dict)


class UrlImport(BaseModel):
    url: str = Field(max_length=2048)


class RequestRetryConfirmation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    snapshot_id: str = Field(pattern=r'^[0-9a-f]{64}$')
    acknowledge_duplicate_charge: StrictBool


class LossReviewRecoveryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    snapshot_id: str = Field(pattern=r'^[0-9a-f]{64}$')


_REVIEW_FIELDS = ('id', 'request_id', 'run_id', 'role', 'provider', 'model', 'operation_id',
                  'controller_operation_id', 'http_status', 'source', 'review_status', 'request_audit_id',
                  'request_body_available', 'request_body_truncated', 'historical_capture_missing',
                  'provenance', 'source_completed_at', 'created_at', 'derived_from_legacy_receipt')
_AUDIT_FIELDS = ('id', 'request_id', 'run_id', 'role', 'provider', 'model', 'operation_id',
                 'controller_operation_id', 'created_at', 'protocol', 'endpoint', 'http_method', 'capture_stage',
                 'body', 'truncated', 'body_preview', 'body_sha256', 'redacted_body_sha256', 'hash_format',
                 'redacted_body_bytes', 'original_body_bytes', 'redacted', 'note')
_RESPONSE_ID_LOCATIONS = {*(f'header:{name}' for name in (
    'x-request-id', 'request-id', 'x-sub2api-request-id', 'x-gateway-request-id', 'x-trace-id')),
    *(f'{prefix}:{name}' for prefix in ('body', 'error') for name in ('request_id', 'requestId'))}


def _diagnostic_redaction(value, secrets):
    """Defense in depth for downloads; never include stored HTTP headers."""
    clean = _redact_request_body(value, secrets)

    def without_headers(item):
        if isinstance(item, dict):
            return {key: without_headers(child) for key, child in item.items()
                    if str(key).lower().replace('-', '_') not in {
                        'headers', 'request_headers', 'response_headers', 'authorization', 'proxy_authorization',
                        'api_key', 'apikey', 'x_api_key', 'cookie', 'set_cookie'}}
        if isinstance(item, list):
            return [without_headers(child) for child in item]
        return item

    return without_headers(clean)


def moderation_reviews_view(store, run_id, billing, *, secrets=()):
    """Read-only persisted reviews plus explicitly identified legacy receipts."""
    records = {row['request_id']: copy.deepcopy(row) for row in store.list('moderation_reviews', run_id)
               if isinstance(row.get('request_id'), str) and row['request_id']}
    for receipt in billing.get('records', []):
        request_id = receipt.get('id')
        if (not isinstance(request_id, str) or not request_id or request_id in records
                or receipt.get('run_id', run_id) != run_id or receipt.get('status') != 'http_error'
                or not is_moderation_error(receipt.get('error_details'))):
            continue
        audit_id = receipt.get('request_audit_id')
        audit = store.get('request_audits', audit_id) if isinstance(audit_id, str) else None
        available = bool(audit and audit.get('run_id') == run_id and audit.get('request_id') == request_id)
        records[request_id] = {key: receipt.get(key) for key in (
            'role', 'provider', 'model', 'operation_id', 'controller_operation_id', 'http_status', 'error_details',
            'response_request_ids')}
        records[request_id].update(id=request_id, request_id=request_id, run_id=run_id,
            source='unknown', review_status='pending', request_audit_id=audit_id if available else None,
            request_body_available=available, request_body_truncated=audit.get('truncated') if available else None,
            historical_capture_missing=not available, provenance='canonical_billing_receipt_read_only_view',
            source_completed_at=receipt.get('completed_at'), created_at=receipt.get('created_at'),
            derived_from_legacy_receipt=True)
    result = []
    for request_id in sorted(records):
        row = records[request_id]
        item = {key: copy.deepcopy(row.get(key)) for key in _REVIEW_FIELDS if key in row}
        item['error_details'] = _http_error_details({'error': row.get('error_details')}, list(secrets))
        response_ids = row.get('response_request_ids') or {}
        item['response_request_ids'] = {}
        for location, values in response_ids.items() if isinstance(response_ids, dict) else ():
            if location not in _RESPONSE_ID_LOCATIONS:
                continue
            candidates = values if isinstance(values, list) else [values]
            identifiers = []
            for opaque in candidates:
                if (len(identifiers) < 8 and isinstance(opaque, str)
                        and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}', opaque)
                        and _redact_request_body(opaque, list(secrets)) == opaque and opaque not in identifiers):
                    identifiers.append(opaque)
            if identifiers:
                item['response_request_ids'][location] = identifiers if isinstance(values, list) else identifiers[0]
        result.append(_diagnostic_redaction(item, list(secrets)))
    return result


def _moderation_source_links(store, run_id, review):
    """Link exact operation inputs only; proximity or a paper title is not proof."""
    operation = review.get('operation_id')
    keys = store.record_keys('transcripts', run_id,
        predicate="json_extract(data,'$.operation_id')=? AND json_extract(data,'$.role')=? AND json_extract(data,'$.kind')='input'",
        parameters=(operation, review.get('role'))) if operation else []
    inputs = [row for key in keys if (row := store.get('transcripts', key))
              and row.get('operation_id') == operation and row.get('role') == review.get('role')
              and row.get('kind') == 'input' and row.get('run_id') == run_id]
    papers, references, artifacts = set(), set(), set()

    def collect(value, field=None):
        if isinstance(value, dict):
            if field == 'papers' and isinstance(value.get('id'), str):
                papers.add(value['id'])
            if field == 'references' and isinstance(value.get('id'), str):
                references.add(value['id'])
            if value.get('verified') is True and isinstance(value.get('id'), str):
                papers.add(value['id'])
            for key, destination in (('reference_id', references), ('artifact_id', artifacts)):
                if isinstance(value.get(key), str):
                    destination.add(value[key])
            for key, destination in (('paper_ids', papers), ('source_paper_ids', papers),
                                     ('reference_ids', references), ('source_artifact_ids', artifacts)):
                if isinstance(value.get(key), list):
                    destination.update(item for item in value[key] if isinstance(item, str))
            for key, child in value.items():
                collect(child, key)
        elif isinstance(value, list):
            for child in value:
                collect(child, field)

    for row in inputs:
        collect(row.get('context', {}))
    return {'input_transcript_ids': sorted(row['id'] for row in inputs), 'paper_ids': sorted(papers),
            'reference_ids': sorted(references), 'artifact_ids': sorted(artifacts),
            'scope': '仅关联同一操作的已保存输入。输入可能经过压缩，不等于实际发送正文；关联不证明任何论文触发拒绝。'}


async def _implementation_control_thread(function, *args, **kwargs):
    """Retain the lifecycle lock until an uncancellable control worker stops."""
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()
        raise


def create_app(data_dir=DATA_DIR, intelligence_factory=Intelligence):
    from .task_runtime import TaskRuntime
    store = Store(Path(data_dir) / 'research.sqlite3')
    engine = Engine(store, Path(data_dir), intelligence_factory=intelligence_factory)
    task_runtime = TaskRuntime()
    from .run_control_requests import RunControls, register as register_run_controls
    run_controls = RunControls(engine)

    @asynccontextmanager
    async def research_lifespan():
        engine.recover()
        run_controls.recover()
        # One local backfill also retries interrupted indexing; never calls a model/network.
        def backfill():
            with store._read_db() as db:
                pending_runs = [row[0] for row in db.execute("""SELECT DISTINCT json_extract(data,'$.run_id')
                    FROM records WHERE collection='literature_notes'
                    AND json_extract(data,'$.id') NOT IN
                    (SELECT json_extract(data,'$.origin_note_id') FROM records WHERE collection='paper_rag_jobs'
                     AND json_extract(data,'$.status')='completed' AND json_extract(data,'$.origin_note_id') IS NOT NULL)""")]
            for run_id in pending_runs:
                if store.get('runs', run_id):
                    try:
                        engine.paper_rag.publish(engine, run_id)
                    except RunDeletedError:
                        continue
        await asyncio.to_thread(backfill)
        async def maintain_rag():
            while True:
                await asyncio.sleep(60)
                await asyncio.to_thread(backfill)
        maintenance = asyncio.create_task(maintain_rag())
        async def maintain_codex_chats():
            from .codex_cleanup import cleanup_completed
            while True:
                await cleanup_completed(store, limit=6)
                await asyncio.sleep(60)
        chat_cleanup = asyncio.create_task(maintain_codex_chats())
        async def maintain_architecture_diagrams():
            from .architecture_diagrams import start_pending
            while True:
                start_pending(engine)
                await asyncio.sleep(5)
        diagram_maintenance = asyncio.create_task(maintain_architecture_diagrams())
        try:
            yield
        finally:
            maintenance.cancel()
            chat_cleanup.cancel()
            diagram_maintenance.cancel()
            await asyncio.gather(maintenance, chat_cleanup, diagram_maintenance, return_exceptions=True)
            await run_controls.close()
            await engine.shutdown()

    @asynccontextmanager
    async def lifespan(app):
        await task_runtime.start()
        research = research_lifespan()
        try:
            await task_runtime.call(research.__aenter__)
            try:
                yield
            finally:
                await task_runtime.call(research.__aexit__, None, None, None)
        finally:
            await task_runtime.close()
            from .assignment_reading_cache import CACHE
            await asyncio.to_thread(CACHE.close)

    app = FastAPI(title='AutoResearch Lab', version='0.1.0', lifespan=lifespan)
    app.state.store, app.state.engine = store, engine
    app.state.task_runtime = task_runtime
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver'])
    allowed_origins = ['http://localhost:5173', 'http://127.0.0.1:5173', 'http://localhost:8000', 'http://127.0.0.1:8000']
    app.add_middleware(CORSMiddleware, allow_origins=allowed_origins, allow_methods=['GET', 'POST', 'PUT', 'DELETE'], allow_headers=['Content-Type'])

    @app.exception_handler(RunDeletedError)
    async def deleted_task_error(request: Request, exc: RunDeletedError):
        return JSONResponse({'detail': str(exc)}, status_code=409)

    @app.middleware('http')
    async def same_origin(request: Request, call_next):
        started = time.perf_counter()
        origin = request.headers.get('origin')
        # A loopback instance may use an alternate port while retaining the
        # same-origin boundary; TrustedHostMiddleware still validates its host.
        own_origin = str(request.base_url).rstrip('/')
        if origin and origin not in allowed_origins and origin != own_origin:
            return JSONResponse({'detail': 'This local application rejects requests from other web origins'}, status_code=403)
        response = await call_next(request)
        endpoint = request.scope.get('endpoint')
        lane = getattr(endpoint, 'execution_lane', 'data')
        response.headers['X-AutoResearch-Lane'] = lane
        response.headers['Server-Timing'] = f'app;dur={(time.perf_counter() - started) * 1000:.2f}'
        return response

    def require_run(run_id):
        run = store.get('runs', run_id)
        if not run:
            raise HTTPException(404, 'Research task not found')
        return run

    def intelligence():
        return intelligence_factory(store, get_settings(store))

    register_run_controls(app, run_controls, task_runtime, require_run)

    def diagnostic_secrets():
        providers = get_settings(store).get('providers', [])
        return [secret for provider in providers for secret in (
            os.environ.get(provider.get('api_key_env', ''), ''), provider.get('api_key', ''))
            if isinstance(secret, str) and secret]

    from .memory_views import register_memory_routes
    from .reliability_routes import register as register_reliability_routes
    register_reliability_routes(app, store, require_run)
    register_memory_routes(app, store, require_run,
                           lambda value: _diagnostic_redaction(value, diagnostic_secrets()))
    from .code_editors import register as register_code_editors
    register_code_editors(app, store, require_run)

    @app.get('/api/health')
    async def health():
        from .innovation_policy import VERSION as novelty_scope_policy
        return {'status': 'ok', 'version': '0.1.0', 'time': now(), 'context_compiler': 3, 'layered_literature_memory': 1,
                'request_execution': {'data_loop': 'http', 'control_loop': 'research-control',
                    'control_loop_running': task_runtime.loop is not None,
                    'reading_projection': 'isolated_process', 'research_workers': 1},
                'novelty_evidence_recovery': 2, 'novelty_scope_policy': novelty_scope_policy,
                'context_navigation': 1,
                'context_capacity_policy': 2,
                'literature_media': {'version': 1, 'local_semantic_retrieval': True, 'local_ocr': True,
                    'image_model_inputs': True, 'exact_context_text_table': True},
                'capabilities': {'codex_reasoning': 1, 'idea_provenance': 1, 'literature_notes': 1, 'idea_materials': 1, 'local_codex': 1, 'codex_literature': 1, 'pdf_tools': 1, 'pdf_reading_recovery': 2, 'survey_json_recovery': 1, 'pdf_citation_handles': 2, 'normal_stream_retry': 1, 'agent_self_repair': 1, 'innovation_review': 1, 'resource_contract': 1, 'stage_details': 1, 'paper_rag': 1, 'paper_identity': 1, 'review_reference_repair': 1, 'review_fix_repair': 1, 'review_split': 1}}

    @app.get('/api/tools')
    def tools_catalog(run_id: str | None = None):
        from .tool_views import tool_catalog
        if run_id is not None:
            require_run(run_id)
        return _diagnostic_redaction(tool_catalog(store, run_id), diagnostic_secrets())

    @app.get('/api/runs/{run_id}/tool-calls')
    def run_tool_calls(run_id: str, limit: int = Query(default=30, ge=1, le=100),
                       offset: int = Query(default=0, ge=0),
                       role: str | None = Query(default=None, pattern='^(director|engineer|scout)$'),
                       tool: str | None = Query(default=None, min_length=1, max_length=128),
                       status: str | None = Query(default=None, pattern='^(started|completed|failed)$')):
        from .tool_views import tool_calls_view
        run = require_run(run_id)
        secrets = diagnostic_secrets()
        result = tool_calls_view(store, run, clean=lambda value: _diagnostic_redaction(value, secrets),
                                 limit=limit, offset=offset, role=role, tool=tool, status=status)
        require_run(run_id)
        return _diagnostic_redaction(result, secrets)

    @app.get('/api/runs/{run_id}/datasets')
    def run_datasets(run_id: str, asset_id: str | None = None, expected_sha256: str | None = None,
                     offset: int = Query(default=0, ge=0), limit: int = Query(default=10, ge=1, le=20)):
        require_run(run_id)
        try:
            result = engine.datasets.inspect(run_id, asset_id=asset_id, expected_sha256=expected_sha256,
                                             offset=offset, limit=limit)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        require_run(run_id)
        return _diagnostic_redaction(result, diagnostic_secrets())

    @app.get('/api/runs/{run_id}/experiment-protocols')
    def run_experiment_protocols(run_id: str, protocol_id: str | None = None, note_id: str | None = None, expected_sha256: str | None = None,
                                offset: int = Query(default=0, ge=0), limit: int = Query(default=10, ge=1, le=20)):
        from .experiment_protocols import inspect
        require_run(run_id)
        try:
            result = inspect(engine, run_id, protocol_id=protocol_id, note_id=note_id, expected_sha256=expected_sha256, offset=offset, limit=limit)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        require_run(run_id)
        return _diagnostic_redaction(result, diagnostic_secrets())

    @app.get('/api/runs/{run_id}/benchmarks')
    def run_benchmarks(run_id: str):
        from . import benchmark_matrix
        require_run(run_id)
        plan = engine.experiment_plan(run_id)
        if not benchmark_matrix.enabled(plan):
            return {'benchmark_matrix': False, 'task_plans': []}
        design = engine.outputs(run_id)['design']
        result = {'benchmark_matrix': True, 'plan': plan,
            'selection': engine.store.get('selections', run_id),
            'validation': engine.cycle_validation_summary(run_id, design),
            'evaluation': engine.evaluation_summary(run_id)}
        require_run(run_id)
        return _diagnostic_redaction(result, diagnostic_secrets())

    @app.get('/api/runs/{run_id}/tool-calls/{call_key}')
    def run_tool_call_detail(run_id: str, call_key: str):
        from .tool_views import tool_call_detail_view
        run = require_run(run_id)
        secrets = diagnostic_secrets()
        result = tool_call_detail_view(store, run, call_key,
                                      clean=lambda value: _diagnostic_redaction(value, secrets))
        require_run(run_id)
        if result is None:
            raise HTTPException(404, '本次研究不存在该工具调用记录，或事件关联不唯一')
        return JSONResponse(_diagnostic_redaction(result, secrets), headers={'Cache-Control': 'no-store'})

    @app.get('/api/local-codex/status')
    async def local_codex_status():
        return await codex_status(get_settings(store).get('defaults', {}).get('codex_project_dir'))

    @app.get('/api/local-codex/models')
    async def local_codex_models():
        from .local_codex import LocalCodexClient
        try:
            blocked_env_names = [p.get('api_key_env', '') for p in get_settings(store).get('providers', [])]
            models = await LocalCodexClient(blocked_env_names=blocked_env_names).models()
            return {'models': models, 'checked_at': now()}
        except Exception:
            return {'models': [], 'checked_at': now(), 'error': '无法读取本机 Codex 模型列表，请检查本机登录和程序状态；未发送生成请求。'}

    @app.get('/api/runs/{run_id}/local-codex')
    def run_local_codex(run_id: str):
        require_run(run_id)
        return _diagnostic_redaction(local_codex_view(engine, run_id), diagnostic_secrets())

    @app.get('/api/runs/{run_id}/reasoning-requests')
    def reasoning_requests(run_id: str, limit: int = Query(default=30, ge=1, le=100), offset: int = Query(default=0, ge=0)):
        require_run(run_id)
        rows = store.reasoning_request_summaries(run_id, limit=limit, offset=offset)
        result = _diagnostic_redaction(rows, diagnostic_secrets())
        require_run(run_id)
        return result

    @app.get('/api/runs/{run_id}/reasoning-requests/{request_id}')
    def reasoning_request(run_id: str, request_id: str):
        require_run(run_id)
        row = store.get('codex_reasoning', request_id)
        if not row or row.get('run_id') != run_id:
            raise HTTPException(404, '本次研究不存在该推理请求')
        receipt = row.get('checkpoint', {})
        payload = {'id': row['id'], 'run_id': run_id, 'role': row['role'],
            'created_at': row['created_at'], 'selection': row['selection'],
            'instruction': row['instruction'], 'schema': row.get('schema'),
            'transport_schema': row.get('transport_schema'),
            'receipt': {k: receipt.get(k) for k in ('status', 'thread_id', 'turn_id',
                'turn_dispatched_at', 'auth_mode', 'reasoning_only', 'error_code',
                'native_tool_violation', 'usage', 'instruction_sha256')},
            'result': row.get('result'),
            'scope': '发送前冻结的本地任务正文、schema 和请求参数；回执单独显示。内容已脱敏，不含登录凭据或模型内部思考。'}
        return JSONResponse(_diagnostic_redaction(payload, diagnostic_secrets()), headers={'Cache-Control': 'no-store'})

    @app.get('/api/settings')
    def settings():
        return public_settings(get_settings(store))

    @app.put('/api/settings')
    def update_settings(payload: dict):
        if any(r['status'] == 'running' for r in store.list('runs')):
            raise HTTPException(409, 'Pause running tasks before changing provider settings')
        current = get_settings(store)
        pending_secrets = {}
        if not isinstance(payload.get('providers', []), list):
            raise HTTPException(422, 'Providers must be a list')
        for candidate in payload.get('providers', []):
            if not isinstance(candidate, dict):
                raise HTTPException(422, 'Each provider must be an object')
            found = next((p for p in current['providers'] if p['id'] == candidate.get('id')), None)
            if found is None:
                raise HTTPException(422, 'Unknown provider id')
            if found.get('protocol') in {'local_codex'}:
                from .subscription_settings import validate
                try:
                    found.update(validate(found, candidate))
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc
                continue
            provider = {**found, **{k: v for k, v in candidate.items() if k in {'name', 'protocol', 'base_url', 'model', 'context_window', 'max_output_tokens', 'timeout_seconds', 'input_price', 'output_price', 'cached_input_price', 'cache_write_price', 'price_verified', 'prompt_cache_enabled', 'responses_streaming'}}}
            if any(not isinstance(provider.get(k), str) or not provider[k].strip() for k in ['name', 'protocol', 'base_url', 'model']):
                raise HTTPException(422, 'Provider name, model, protocol and URL must be nonempty text')
            provider.setdefault('responses_streaming', False)
            if any(not isinstance(provider.get(k), bool) for k in ['price_verified', 'prompt_cache_enabled', 'responses_streaming']):
                raise HTTPException(422, 'Price verification, prompt cache and response streaming flags must be booleans')
            parsed = urlparse(provider['base_url'])
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                raise HTTPException(422, 'Provider base URL must be HTTPS without embedded credentials')
            if provider['protocol'] not in {'responses', 'anthropic', 'chat'}:
                raise HTTPException(422, 'Unknown wire protocol')
            read_timeout = provider.get('timeout_seconds', 120)
            if (type(read_timeout) not in (int, float) or not math.isfinite(read_timeout)
                    or not 5 <= read_timeout <= 180):
                raise HTTPException(422, 'Provider read timeout must be a number from 5 to 180 seconds')
            provider['timeout_seconds'] = float(read_timeout)
            try:
                for key in ['input_price', 'output_price', 'cached_input_price', 'cache_write_price']:
                    provider[key] = float(provider[key])
                    if not 0 <= provider[key] <= 10000:
                        raise ValueError('rate')
                provider['max_output_tokens'] = int(provider['max_output_tokens'])
                provider['context_window'] = int(provider['context_window'])
                if not 64 <= provider['max_output_tokens'] <= 32000 or provider['context_window'] < 4000:
                    raise ValueError('token limits')
            except (TypeError, ValueError):
                raise HTTPException(422, 'Invalid price or token limit')
            if candidate.get('api_key'):
                pending_secrets[found['api_key_env']] = candidate['api_key']
            found.update(provider)
        if 'roles' in payload:
            roles = payload['roles']
            if not isinstance(roles, dict) or set(roles) != set(ROLES) or any(not isinstance(v, str) or v not in {p['id'] for p in current['providers']} for v in roles.values()):
                raise HTTPException(422, 'All three research roles need a known provider')
            current['roles'] = roles
        if 'defaults' in payload:
            if not isinstance(payload['defaults'], dict):
                raise HTTPException(422, 'Defaults must be an object')
            allowed_defaults = set(RunCreate.model_fields) - {'title', 'objective', 'mode', 'publication_target', 'reference_ids'}
            allowed_defaults.add('unknown_cost_reserve_usd')
            if set(payload['defaults']) - allowed_defaults:
                raise HTTPException(422, 'Unknown default execution setting')
            merged = {**current['defaults'], **payload['defaults']}
            try:
                normalized = RunCreate(objective='Validate default limits', **{k: v for k, v in merged.items() if k in RunCreate.model_fields}).model_dump()
                merged.update({k: normalized[k] for k in merged if k in normalized})
                reserve = float(merged.get('unknown_cost_reserve_usd', .25))
                if not math.isfinite(reserve) or not 0 <= reserve <= 1000:
                    raise ValueError('Invalid unknown cost reserve')
                merged['unknown_cost_reserve_usd'] = reserve
            except Exception:
                raise HTTPException(422, 'Invalid default run limits')
            current['defaults'] = merged
        try:
            json.dumps(current, allow_nan=False)
            if pending_secrets:
                save_secrets(pending_secrets)
        except (ValueError, TypeError):
            raise HTTPException(422, 'Invalid settings or credential value; nothing was saved')
        store.put('settings', 'main', current)
        return public_settings(current)

    @app.post('/api/providers/{provider_id}/test')
    async def test_provider(provider_id: str):
        provider = next((p for p in get_settings(store)['providers'] if p['id'] == provider_id), None)
        if not provider:
            raise HTTPException(404, 'Unknown provider')
        if provider['protocol'] == 'local_codex':
            from .codex_reasoning import connection
            try:
                status = await connection(get_settings(store))
                selected = next((m for m in status['models'] if m['model'] == provider['model']), {})
                ok = status['auth_mode'] == 'chatgpt' and all(e in selected.get('supported_reasoning_efforts', [])
                    for e in [provider['reasoning_effort'], provider['deep_reasoning_effort']])
                return {'ok': ok, 'model': provider['model'], 'latency_ms': 0,
                        'message': '仅检查登录和模型列表，未生成。', **status}
            except Exception:
                return {'ok': False, 'model': provider['model'], 'latency_ms': 0,
                        'message': '无法确认本机 Codex 的 ChatGPT 登录或模型配置；未回退到 API。'}
        key = os.environ.get(provider['api_key_env'])
        if not key:
            return {'ok': False, 'model': provider['model'], 'latency_ms': 0, 'message': 'API key not configured'}
        started = time.monotonic()
        headers = {'x-api-key': key, 'anthropic-version': '2023-06-01'} if provider['protocol'] == 'anthropic' else {'Authorization': f'Bearer {key}'}
        try:
            async with httpx.AsyncClient(timeout=25) as client:
                response = await client.get(provider['base_url'].rstrip('/') + '/models', headers=headers)
            data = response.json()
            listed = any(m.get('id') == provider['model'] for m in data.get('data', []))
            return {'ok': response.is_success and listed, 'model': provider['model'], 'latency_ms': round((time.monotonic() - started) * 1000), 'message': f'HTTP {response.status_code}; ' + ('selected model listed. No generation request was sent by this check.' if listed else 'selected model absent or request rejected. Model listing does not guarantee generation availability.')}
        except Exception as exc:
            return {'ok': False, 'model': provider['model'], 'latency_ms': round((time.monotonic() - started) * 1000), 'message': redact(str(exc))[:500]}

    @app.get('/api/hardware')
    async def hardware(python_executable: str = Query(default='', max_length=2048)):
        try:
            executable = validate_python(python_executable)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        measured = await asyncio.to_thread(hardware_profile)
        return await asyncio.to_thread(apply_execution_preferences, {'python_executable': executable}, measured)

    @app.get('/api/python-environments')
    async def python_environments():
        return await asyncio.to_thread(discover_python_environments)

    @app.get('/api/overview')
    def overview():
        # FastAPI runs synchronous routes off the event loop. SQLite and billing
        # reads must not hold up health checks or the browser while they wait.
        current_settings = get_settings(store)
        from .billing_projection import summary as billing_summary
        from .page_queries import run_list, run_counts
        bill = billing_summary(store)
        providers = [{key: value.get(key) for key in ('id', 'name', 'model', 'configured')}
                     for value in public_settings(current_settings)['providers']]
        return {'runs': run_list(store, limit=4), 'run_counts': run_counts(store), 'hardware': hardware_profile(),
                'providers': providers, 'roles': current_settings['roles'],
                'totals': bill, 'memory_count': store.count('memory')}

    @app.get('/api/runs')
    def runs():
        from .page_queries import run_list
        return run_list(store)

    @app.post('/api/runs', status_code=201)
    def create_run(data: RunCreate):
        for ref in data.reference_ids:
            if not store.get('references', ref):
                raise HTTPException(422, 'Unknown reference document')
        values = {**get_settings(store)['defaults'], **data.model_dump(exclude_unset=True)}
        try:
            validated = RunCreate(**{k: v for k, v in values.items() if k in RunCreate.model_fields})
        except Exception:
            raise HTTPException(422, 'Saved execution defaults are invalid; update settings before creating a task')
        return engine.create({**validated.model_dump(), 'literature_protocol': 2, 'pdf_tools_protocol': 1,
                              'codex_literature_protocol': 1})

    @app.get('/api/runs/{run_id}')
    def run_detail(run_id: str):
        # The old URL is also a summary. Details are explicitly requested via
        # /views/{section}, /stages/{stage}, and exact document/record routes.
        return run_summary(run_id)

    @app.get('/api/runs/{run_id}/summary')
    def run_summary(run_id: str):
        from . import run_views
        require_run(run_id)
        value = run_views.summary(engine, run_id)
        value['moderation_reviews'] = moderation_reviews_view(store, run_id,
            run_views.legacy_moderation_receipts(store, run_id), secrets=diagnostic_secrets())
        return value

    @app.get('/api/runs/{run_id}/views/{section}')
    def run_view(run_id: str, section: str):
        from . import run_views
        require_run(run_id)
        if section not in run_views.SECTIONS:
            raise HTTPException(404, 'Unknown research view')
        return run_views.section(engine, run_id, section, intelligence=intelligence,
            local_codex=lambda: _diagnostic_redaction(local_codex_view(engine, run_id), diagnostic_secrets()))

    @app.get('/api/runs/{run_id}/implementation-progress')
    def implementation_progress_detail(run_id: str):
        from .implementation_progress import detail
        require_run(run_id)
        with store.read_snapshot() as snapshot:
            value = detail(snapshot, snapshot.get('runs', run_id))
        return _diagnostic_redaction(value, diagnostic_secrets())

    @app.get('/api/runs/{run_id}/experiment-workflow')
    def experiment_workflow(run_id: str, offset: int = Query(default=0, ge=0),
                            limit: int = Query(default=8, ge=1, le=25),
                            expected_sha256: str | None = Query(default=None, pattern='^[a-f0-9]{64}$')):
        from .experiment_tools import status
        require_run(run_id)
        try:
            return _diagnostic_redaction(status(engine, run_id, {'offset': offset, 'limit': limit,
                'expected_sha256': expected_sha256}), diagnostic_secrets())
        except (ValueError, KeyError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/runs/{run_id}/experiment-decisions')
    def experiment_decisions(run_id: str, limit: int = Query(default=8, ge=1, le=20),
                             before_time: str | None = None, before_id: str | None = None):
        from .experiment_decisions import history
        require_run(run_id)
        if bool(before_time) != bool(before_id):
            raise HTTPException(422, 'A complete decision cursor is required')
        return _diagnostic_redaction(history(store, run_id, limit=limit,
            before=[before_time, before_id] if before_time else None), diagnostic_secrets())

    @app.get('/api/runs/{run_id}/training-console')
    def training_console(run_id: str, trial_id: str | None = Query(default=None, max_length=200)):
        from .experiment_views import console
        require_run(run_id)
        try:
            return _diagnostic_redaction(console(engine, run_id, trial_id), diagnostic_secrets())
        except KeyError as exc:
            raise HTTPException(404, '当前任务没有此实验记录') from exc

    @app.get('/api/runs/{run_id}/training-loss-review')
    def training_loss_review_preview(run_id: str):
        from .training_review_recovery import snapshot, view
        from .engine import ResearchBlocked
        require_run(run_id)
        try:
            return _diagnostic_redaction({**snapshot(engine, run_id), **view(engine, run_id)}, diagnostic_secrets())
        except (ValueError, ResearchBlocked) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/runs/{run_id}/performance-policy')
    def performance_policy_preview(run_id: str, metric: str, direction: str):
        from .performance_policy import preview
        from .engine import ResearchBlocked
        require_run(run_id)
        try:
            return preview(engine, run_id, metric, direction)
        except (ValueError, ResearchBlocked) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/performance-policy')
    @task_runtime.control
    async def performance_policy_adopt(run_id: str, payload: PerformancePolicyRequest):
        from .performance_policy import adopt
        from .engine import ResearchBlocked
        require_run(run_id)
        if not payload.authorize_policy_change:
            raise HTTPException(422, '必须明确授权本任务性能判据修订。')
        try:
            return await adopt(engine, run_id, payload.snapshot_id, payload.metric, payload.direction, payload.instruction)
        except (ValueError, ResearchBlocked) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/runs/{run_id}/rapid-continuation')
    def rapid_continuation_preview(run_id: str):
        from .rapid_continuation import snapshot, view
        from .engine import ResearchBlocked
        require_run(run_id)
        try:
            frozen = snapshot(engine, run_id)
            return _diagnostic_redaction({'snapshot_id': frozen['snapshot_id'],
                'policy_amendment': frozen['policy_amendment'], 'record': view(store, run_id)}, diagnostic_secrets())
        except (ValueError, ResearchBlocked) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/rapid-continuation')
    @task_runtime.control
    async def rapid_continuation_start(run_id: str, payload: RapidContinuationRequest):
        from .rapid_continuation import start
        from .engine import ResearchBlocked
        require_run(run_id)
        if not payload.authorize_policy_change:
            raise HTTPException(422, '必须明确授权本轮停止规则修订。')
        try:
            return await start(engine, run_id, payload.snapshot_id, payload.instruction)
        except (ValueError, ResearchBlocked, ProviderError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/training-loss-review')
    @task_runtime.control
    async def training_loss_review_start(run_id: str, payload: LossReviewRecoveryRequest):
        from .training_review_recovery import start
        from .engine import ResearchBlocked
        require_run(run_id)
        try:
            return await start(engine, run_id, payload.snapshot_id)
        except (ValueError, ResearchBlocked, ProviderError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/runs/{run_id}/stages/{stage}')
    def run_stage(run_id: str, stage: str):
        from . import run_views
        require_run(run_id)
        if stage not in STAGES:
            raise HTTPException(404, 'Unknown research stage')
        return run_views.stage(engine, run_id, stage, intelligence=intelligence,
            local_codex=lambda: _diagnostic_redaction(local_codex_view(engine, run_id), diagnostic_secrets()))

    @app.get('/api/runs/{run_id}/architecture-adjustments/{revision_id}')
    def architecture_adjustment(run_id: str, revision_id: str):
        from .architecture_adjustment_views import detail
        require_run(run_id)
        try:
            return detail(store, run_id, revision_id)
        except KeyError as exc:
            raise HTTPException(404, 'Unknown architecture adjustment') from exc

    @app.get('/api/runs/{run_id}/architecture-diagrams')
    def architecture_diagrams(run_id: str, document_id: str, expected_sha256: str, historical: bool = False):
        from .architecture_diagrams import document_review, view
        require_run(run_id)
        try:
            review = document_review(engine, run_id, document_id, expected_sha256, historical=historical)
            return view(engine, run_id, review['document'], historical=historical)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/architecture-diagrams')
    @task_runtime.control
    async def generate_architecture_diagrams(run_id: str, request: ArchitectureDiagramRequest):
        from .architecture_diagrams import queue, start_pending
        require_run(run_id)
        try:
            result = await asyncio.to_thread(queue, engine, run_id, request.document_id,
                request.expected_sha256, retry=request.retry)
        except (ValueError, KeyError, OSError) as exc:
            raise HTTPException(409, redact(str(exc))) from exc
        start_pending(engine)
        return result

    @app.get('/api/runs/{run_id}/architecture-diagrams/render/{kind}')
    async def render_architecture_diagram(run_id: str, kind: str, document_id: str, expected_sha256: str, historical: bool = False):
        from .architecture_diagram_presentation import presentation
        require_run(run_id)
        if kind not in {'network', 'flowchart'}:
            raise HTTPException(404, '未知架构图类型。')
        try:
            return await asyncio.to_thread(presentation, engine, run_id, document_id, expected_sha256, kind, historical=historical)
        except (ValueError, KeyError, OSError) as exc:
            raise HTTPException(409, redact(str(exc))) from exc

    @app.get('/api/runs/{run_id}/consensus-status')
    def run_consensus_status(run_id: str):
        from .consensus_status import view as consensus_status_view
        from .observability import current_debate, LABELS
        run = require_run(run_id)
        if store.get('run_deletions', run_id):
            raise HTTPException(404, '研究已删除或正在删除')
        pending = current_debate(engine, run_id)
        activity = consensus_status_view(engine, run_id, pending)
        latest = require_run(run_id)
        if store.get('run_deletions', run_id):
            raise HTTPException(404, '研究已删除或正在删除')
        fields = ('status', 'stage', 'state', 'updated_at')
        if any(latest.get(field) != run.get(field) for field in fields):
            raise HTTPException(409, '研究状态已变化，正在读取新的共识状态')
        return {'run_id': run_id, 'derived': True, 'checked_at': now(),
                'run_status': run['status'], 'stage': run['stage'],
                'stage_label': LABELS.get(run['stage'], run['stage']), 'activity': activity}

    @app.get('/api/runs/{run_id}/documents')
    def document_indexes(run_id: str):
        require_run(run_id)
        from .document_index import list_documents
        return list_documents(engine, run_id)

    @app.get('/api/runs/{run_id}/implementation-source-supplement')
    def implementation_source_supplement_status(run_id: str):
        require_run(run_id)
        from .implementation_source_supplement import COLLECTION, POINTER
        pointer = store.get('stage_contexts', run_id + POINTER, {})
        record = store.get(COLLECTION, pointer.get('record_id', ''), {})
        return {key: record[key] for key in ('id', 'sha256', 'status', 'active_role', 'votes', 'issues',
            'created_at', 'completed_at', 'adopted', 'scientific_approval', 'execution_authorized') if key in record}

    @app.post('/api/runs/{run_id}/implementation-source-supplement')
    @task_runtime.control
    async def implementation_source_supplement(run_id: str, payload: dict = Body(...)):
        require_run(run_id)
        from .implementation_source_supplement import prepare
        if set(payload) != {'source_handoff_id', 'document_refs', 'supplement_id'}:
            raise HTTPException(422, 'Expected source_handoff_id, document_refs and supplement_id')
        try:
            async with engine._lifecycle_lock:
                return await _implementation_control_thread(prepare, engine, run_id, **payload)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/implementation-source-supplement/repair')
    @task_runtime.control
    async def implementation_source_supplement_repair(run_id: str, payload: dict = Body(...)):
        require_run(run_id)
        from .implementation_supplement_repair import prepare
        if set(payload) != {'record_id', 'expected_sha256', 'replacements'}:
            raise HTTPException(422, 'Expected original review identity and exact unit replacements')
        try:
            async with engine._lifecycle_lock:
                return await _implementation_control_thread(prepare, engine, run_id, **payload)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/implementation-verification')
    @task_runtime.control
    async def implementation_verification_prepare(run_id: str, payload: dict):
        require_run(run_id)
        from .code_verification import prepare
        required = {'workflow_id', 'snapshot_sha256', 'document_sha256', 'entrypoint',
                    'requirement_ids', 'issue_routes'}
        if not required <= payload.keys() or payload.keys() - required - {'timeout_seconds'}:
            raise HTTPException(422, 'Expected frozen delivery, approved requirements and exact test routing')
        try:
            async with engine._lifecycle_lock:
                return await _implementation_control_thread(prepare, engine, run_id, **payload)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/runs/{run_id}/implementation-verification')
    def implementation_verification_status(run_id: str):
        require_run(run_id)
        from .code_verification import policy, RECEIPTS
        return {'policy': policy(engine, run_id),
                'receipts': engine.store.list(RECEIPTS, run_id),
                'definition_requests': engine.store.list('implementation_definition_requests', run_id)}

    @app.get('/api/runs/{run_id}/implementation-code-recovery-preview')
    @task_runtime.control
    async def implementation_code_recovery_preview(run_id: str, workflow_id: str):
        require_run(run_id)
        from .implementation_code_recovery import preview
        try:
            async with engine._lifecycle_lock:
                return await _implementation_control_thread(preview, engine, run_id, workflow_id)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/implementation-code-recovery')
    @task_runtime.control
    async def implementation_code_recovery(run_id: str, payload: dict):
        require_run(run_id)
        if set(payload) != {'expect_sha', 'workflow_id'}:
            raise HTTPException(422, 'Recovery requires workflow identity and preview SHA')
        from .implementation_code_recovery import restore
        try:
            async with engine._lifecycle_lock:
                return await _implementation_control_thread(restore, engine, run_id, **payload)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/runs/{run_id}/dataset-transition')
    @task_runtime.control
    async def dataset_transition_preview(run_id: str, targets: str):
        require_run(run_id)
        from .dataset_transition import preview
        try:
            async with engine._lifecycle_lock:
                return await _implementation_control_thread(preview, engine, run_id, json.loads(targets))
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/dataset-transition')
    @task_runtime.control
    async def dataset_transition_prepare(run_id: str, payload: dict):
        require_run(run_id)
        if set(payload) != {'snapshot_id', 'targets', 'instruction'}:
            raise HTTPException(422, 'Expected snapshot_id, targets and operator instruction')
        from .dataset_transition import prepare
        try:
            async with engine._lifecycle_lock:
                record = await _implementation_control_thread(prepare, engine, run_id, **payload)
            return {k: record[k] for k in ('id', 'cycle', 'sha256', 'scientific_approval', 'execution_authorized')}
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/dataset-transition/adapt')
    @task_runtime.control
    async def dataset_transition_adapt(run_id: str, payload: dict):
        require_run(run_id)
        if set(payload) != {'transition_id', 'instruction'}:
            raise HTTPException(422, 'Expected transition_id and operator instruction')
        from .dataset_transition import adapt
        try:
            async with engine._lifecycle_lock:
                record = await _implementation_control_thread(adapt, engine, run_id, **payload)
            return {k: record[k] for k in ('id', 'sha256', 'graph_thread_id', 'execution_authorized')}
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/dataset-transition/incremental')
    @task_runtime.control
    async def dataset_transition_incremental(run_id: str, payload: dict):
        require_run(run_id)
        if (set(payload) != {'instruction'} or not isinstance(payload['instruction'], str)
                or not 1 <= len(payload['instruction'].strip()) <= 4000):
            raise HTTPException(422, 'Expected a bounded operator instruction')
        from .dataset_configuration_queue import prepare, view
        try:
            async with engine._lifecycle_lock:
                await _implementation_control_thread(prepare, engine, run_id, payload['instruction'])
            return view(engine.store, run_id)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/experiment-attempt-policy')
    @task_runtime.control
    async def experiment_attempt_policy(run_id: str, payload: dict):
        require_run(run_id)
        if set(payload) != {'instruction'}:
            raise HTTPException(422, 'Expected explicit instruction to remove experiment attempt limits')
        from .experiment_attempt_policy import prepare
        try:
            async with engine._lifecycle_lock:
                return await _implementation_control_thread(prepare, engine, run_id, **payload)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/dataset-transition/budgets')
    @task_runtime.control
    async def dataset_budgets(run_id: str, payload: dict):
        require_run(run_id)
        if set(payload) != {'instruction'}:
            raise HTTPException(422, 'Expected operator instruction for task-based dataset budgets')
        from .dataset_budgets import prepare as configure_budgets
        try:
            async with engine._lifecycle_lock:
                return await _implementation_control_thread(configure_budgets, engine, run_id, **payload)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/dataset-transition/recover-configuration')
    @task_runtime.control
    async def dataset_configuration_recover(run_id: str, payload: dict):
        require_run(run_id)
        if set(payload) != {'setup_id', 'instruction'}:
            raise HTTPException(422, 'Expected setup_id and operator instruction')
        from .dataset_experiments import recover_configuration
        try:
            async with engine._lifecycle_lock:
                saved = await _implementation_control_thread(recover_configuration, engine, run_id, **payload)
            return {k: saved[k] for k in ('id', 'status', 'previous_setup', 'configuration_recovery_protocol')}
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/dataset-transition/tuning')
    @task_runtime.control
    async def dataset_transition_tuning(run_id: str, payload: dict):
        require_run(run_id)
        if set(payload) != {'transition_id', 'instruction'}:
            raise HTTPException(422, 'Expected transition_id and operator instruction')
        from .dataset_experiments import prepare_tuning
        try:
            async with engine._lifecycle_lock:
                record = await _implementation_control_thread(prepare_tuning, engine, run_id, **payload)
            return {k: record[k] for k in ('id', 'sha256', 'body')}
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/runs/{run_id}/implementation-restart-preview')
    @task_runtime.control
    async def implementation_restart_preview(run_id: str):
        require_run(run_id)
        from .implementation_restart import preview
        try:
            async with engine._lifecycle_lock:
                return await _implementation_control_thread(preview, engine, run_id)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/implementation-restart')
    @task_runtime.control
    async def implementation_restart(run_id: str, payload: dict):
        require_run(run_id)
        if set(payload) != {'expect_sha', 'idempotency_key'}:
            raise HTTPException(422, 'Restart requires the bound preview SHA and idempotency key')
        from .implementation_restart import restart
        try:
            async with engine._lifecycle_lock:
                receipt = await _implementation_control_thread(restart, engine, run_id, **payload)
            return {key: receipt[key] for key in ('id', 'run_id', 'cycle', 'generation',
                'graph_thread_id', 'archive_path', 'archive_sha256', 'archived_workflow_ids',
                'paid_evidence_retained', 'target_config', 'removed_config_fields',
                'archived_implementation_handoff_ids', 'archived_implementation_artifact_ids',
                'scientific_approval', 'execution_authorized')
                if key in receipt}
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/architecture-revision-handoff')
    def architecture_revision_handoff(run_id: str, authorization: dict | None = Body(default=None)):
        require_run(run_id)
        from .architecture_revisions import prepare
        try:
            return prepare(engine, run_id, operator_authorization=authorization)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/architecture-definition-handoff')
    def architecture_definition_handoff(run_id: str):
        require_run(run_id)
        from .architecture_definitions import prepare
        try:
            return prepare(engine, run_id)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/architecture-definition-completion-handoff')
    def architecture_definition_completion_handoff(run_id: str):
        require_run(run_id)
        from .architecture_definition_completion import prepare
        try:
            return prepare(engine, run_id)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/architecture-definition-review-recovery')
    def architecture_definition_review_recovery(run_id: str):
        require_run(run_id)
        from .architecture_final_delivery import recover
        try:
            return recover(engine, run_id)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/architecture-integration-handoff')
    def architecture_integration_handoff(run_id: str):
        require_run(run_id)
        from .architecture_integration import prepare
        try:
            return prepare(engine, run_id)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/architecture-delivery-repair-handoff')
    def architecture_delivery_repair_handoff(run_id: str):
        require_run(run_id)
        from .architecture_integration_recovery import prepare_delivery_repair
        try:
            return prepare_delivery_repair(engine, run_id)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/architecture-read-window-handoff')
    def architecture_read_window_handoff(run_id: str):
        require_run(run_id)
        from .architecture_revisions import prepare_read_window_handoff
        try:
            return prepare_read_window_handoff(engine, run_id)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/architecture-workflow-handoff')
    def architecture_workflow_handoff(run_id: str):
        require_run(run_id)
        from .architecture_workflow import prepare
        try:
            return prepare(engine, run_id)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/document-working-notes-policy')
    def document_working_notes_policy(run_id: str):
        require_run(run_id)
        from .document_working_notes import prepare_future_policy
        try:
            return prepare_future_policy(engine, run_id)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/architecture-working-notes-handoff')
    def architecture_working_notes_handoff(run_id: str):
        require_run(run_id)
        from .architecture_note_recovery import prepare
        try:
            return prepare(engine, run_id)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/runs/{run_id}/documents/{document_id}')
    def document_section(run_id: str, document_id: str, expected_sha256: str,
                         section_id: str, offset: int = 0, limit: int = 4000):
        require_run(run_id)
        from .document_index import read
        try:
            return read(engine, run_id, {'document_id': document_id, 'expected_sha256': expected_sha256,
                'section_id': section_id, 'offset': offset, 'limit': limit})
        except (ValueError, OSError, KeyError, TypeError):
            raise HTTPException(409, '文档版本或章节读取校验失败，请重新读取目录')

    @app.get('/api/runs/{run_id}/documents/{document_id}/delivery')
    def document_delivery(run_id: str, document_id: str, expected_sha256: str):
        require_run(run_id)
        from .implementation_document_delivery import location
        try:
            return location(engine, run_id, {'document_id': document_id, 'sha256': expected_sha256})
        except (ValueError, OSError, KeyError, TypeError):
            raise HTTPException(409, '文档位置或版本校验失败，请重新读取目录')

    @app.get('/api/runs/{run_id}/documents/{document_id}/file')
    def document_file(run_id: str, document_id: str, expected_sha256: str):
        require_run(run_id)
        from .document_index import load
        try:
            manifest, text = load(engine, run_id, document_id, expected_sha256)
            artifact = store.get('artifacts', manifest['artifact_id'])
            filename = Path(artifact['path']).name
        except (ValueError, OSError, KeyError, TypeError):
            raise HTTPException(409, '文档版本或完整性校验失败，无法下载')
        media_type = 'text/plain; charset=utf-8' if manifest.get('document_kind') == 'paper_rag_source' else 'text/markdown; charset=utf-8'
        # The index binds canonical UTF-8 text. Windows originals may contain
        # CRLF; export the validated version without rewriting original bytes.
        return Response(text.encode('utf-8'), media_type=media_type,
            headers={'Cache-Control': 'no-store',
                     'Content-Disposition': "attachment; filename*=UTF-8''" + quote(filename)})

    @app.get('/api/runs/{run_id}/moderation/{request_id}/audit')
    def moderation_audit(run_id: str, request_id: str):
        require_run(run_id)
        secrets = diagnostic_secrets()
        reviews = moderation_reviews_view(store, run_id, intelligence().billing(run_id), secrets=secrets)
        review = next((row for row in reviews if row['request_id'] == request_id), None)
        if review is None:
            raise HTTPException(404, 'Moderation review not found for this research task')
        audit_id = review.get('request_audit_id')
        audit = store.get('request_audits', audit_id) if isinstance(audit_id, str) else None
        if audit:
            if audit.get('run_id') != run_id:
                raise HTTPException(404, 'Request audit not found for this research task')
            if audit.get('request_id') != request_id or any(
                    review.get(key) is not None and audit.get(key) != review[key]
                    for key in ('role', 'provider', 'model', 'operation_id')):
                raise HTTPException(409, 'Stored request audit does not match the moderation review')
            audit = {key: copy.deepcopy(audit.get(key)) for key in _AUDIT_FIELDS if key in audit}
        links = _moderation_source_links(store, run_id, review)
        missing = []
        if audit is None:
            missing.extend(['request_audit', 'request_body'])
        elif audit.get('body') is None:
            missing.append('complete_request_body')
        if not review.get('response_request_ids'):
            missing.append('response_request_ids')
        if not links['input_transcript_ids']:
            missing.append('matching_input_transcript')
        payload = _diagnostic_redaction({'run_id': run_id, 'request_id': request_id, 'review': review,
            'audit': audit, 'source_links': links, 'missing': missing,
            'scope': '本地请求 ID 与服务端响应中的请求标识分开记录。拒绝来源仍未知；缺失历史请求正文不能由后来的上下文重建。'
                     '审计捕获发生在 HTTP 发送前，不独立证明发送成功；下载不包含鉴权 headers，保存的哈希对应原捕获。'}, secrets)
        filename = 'moderation-' + re.sub(r'[^A-Za-z0-9_-]', '_', request_id)[:80] + '.json'
        return JSONResponse(payload, headers={'Content-Disposition': f'attachment; filename="{filename}"'})

    @app.delete('/api/runs/{run_id}')
    @task_runtime.control
    async def delete_run(run_id: str):
        try:
            return await engine.delete(run_id)
        except KeyError as exc:
            raise HTTPException(404, 'Research task not found') from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/runs/{run_id}/agents/{role}/history')
    def role_history(run_id: str, role: str, limit: int = Query(default=100, ge=1, le=200),
                     cursor: str | None = Query(default=None, max_length=4096)):
        require_run(run_id)
        if role not in ROLES:
            raise HTTPException(404, 'Unknown agent role')
        try:
            return agent_history_page(store, run_id, role, limit=limit, cursor=cursor)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.put('/api/runs/{run_id}/config')
    def run_config(run_id: str, payload: dict):
        run = require_run(run_id)
        if run['status'] == 'running':
            raise HTTPException(409, 'Pause before changing task limits')
        if RETIRED_IMPLEMENTATION_REVIEW_FIELDS.intersection(payload):
            raise HTTPException(422, '实现文档分章节与批量派发配置已退役；仅支持三方整篇审查。')
        if run.get('state', {}).get('implementation_restart'):
            from .implementation_restart import TARGET_CONFIG
            clock_fields = {'implementation_review_max_seconds', 'implementation_review_max_calls',
                            'implementation_review_time_basis'}
            if any(key in payload and (type(payload[key]) is not type(value) or payload[key] != value)
                   and not (key in clock_fields and type(payload[key]) is type(run['config'].get(key))
                            and payload[key] == run['config'].get(key))
                   for key, value in TARGET_CONFIG.items()):
                raise HTTPException(409, '本次实现重开已绑定新的全文审核配置；不能重新启用旧阶段限制。')
        codex_fields = {'implementation_backend', 'codex_project_dir', 'codex_model', 'codex_reasoning_effort'}
        if 'ccf_ab_only' in payload and payload['ccf_ab_only'] != run['config'].get('ccf_ab_only', False):
            if engine.outputs(run_id).get('literature'):
                raise HTTPException(409, '文献分析已完成，请新建研究使用不同文献范围，避免沿用不符合新范围的共识。')
        review_fields = {'implementation_review_max_revisions',
                         'implementation_review_max_seconds', 'implementation_review_max_calls',
                         'implementation_review_time_basis', 'implementation_review_strategy',
                         'implementation_indexed_review_protocol'}
        if any(k not in {'budget_usd', 'max_calls', 'max_rounds', 'discussion_mode', 'stagnation_rounds', 'max_trials', 'max_research_cycles', 'architecture_max_revisions', 'architecture_stagnation_rounds', 'min_relative_improvement', 'experiment_timeout', 'context_tokens', 'skip_unavailable_papers', 'ccf_ab_only', 'allow_local_execution', 'execution_mode', 'python_executable', 'gpu_index', 'vram_limit_gb', 'cpu_limit_cores', 'ram_limit_gb', 'disk_limit_gb', 'codex_timeout_seconds', 'codex_max_revisions', *codex_fields, *review_fields} for k in payload):
            raise HTTPException(422, 'Only execution limits can be changed after task creation')
        generation = run.get('state', {}).get('implementation_generation')
        workflow_where = "json_extract(data,'$.implementation_generation')=?" if generation else '1'
        frozen = bool(store.record_keys('stage_contexts', predicate='key=?',
            parameters=[f'{run_id}:implementation'], limit=1)) or bool(store.record_keys(
                'codex_workflows', run_id, predicate=workflow_where,
                parameters=(generation,) if generation else (), limit=1))
        if frozen and any(key in payload and payload[key] != run['config'].get(key) for key in review_fields):
            raise HTTPException(409, '验收协议与预算已冻结；不能在审核中更改标准或重置预算。')
        if (frozen and 'implementation_indexed_review_protocol' in payload
                and payload['implementation_indexed_review_protocol'] != run['config'].get('implementation_indexed_review_protocol')):
            raise HTTPException(409, '旧实现输入已冻结；须通过实现重开入口启用索引式全文审核。')
        compute_defaults = {'python_executable': '', 'gpu_index': None, 'vram_limit_gb': None, 'execution_mode': 'local',
                            'cpu_limit_cores': None, 'ram_limit_gb': None, 'disk_limit_gb': None}
        if (frozen or store.count('experiments', run_id)) and any(
                k in payload and payload[k] != run['config'].get(k, default) for k, default in compute_defaults.items()):
            raise HTTPException(409, '代码实现或实验已启动，运行环境与显存配置已锁定；请新建研究使用不同配置，以保持实验可比较。')
        defaults = {'implementation_backend': 'local_codex', 'codex_project_dir': str(ROOT), 'codex_model': '', 'codex_reasoning_effort': ''}
        if frozen and any(k in payload and payload[k] != run['config'].get(k, defaults[k]) for k in codex_fields):
            raise HTTPException(409, '代码实现交接已冻结，不能更改实现方式、项目目录、模型或推理等级；请新建研究以使用新配置。')
        updated = {**run['config'], **payload}
        try:
            validation = {k: v for k, v in updated.items() if k in RunCreate.model_fields and k != 'objective'}
            historical_strategy = ('implementation_review_strategy' not in payload
                and updated.get('implementation_review_strategy') in {'chapter_issue', 'consolidated', 'segmented'})
            if historical_strategy:
                validation['implementation_review_strategy'] = 'whole_alignment'
            validated = RunCreate(objective=run['objective'], **validation).model_dump()
            updated.update({k: validated[k] for k in updated if k in validated
                and not (historical_strategy and k == 'implementation_review_strategy')})
        except Exception:
            raise HTTPException(422, 'Invalid task limits')
        if 'architecture_max_revisions' in payload or 'architecture_stagnation_rounds' in payload:
            updated.update(architecture_max_revisions=None, architecture_progress_protocol=1)
        if 'max_research_cycles' in payload:
            from .research_iteration import set_limit
            try:
                set_limit(store, run_id, validated['max_research_cycles'],
                          '用户通过执行配置调整科研迭代次数上限')
            except ValueError as exc:
                raise HTTPException(409,str(exc)) from exc
            if 'max_research_cycles' in run['config']:
                updated['max_research_cycles']=run['config']['max_research_cycles']
            else:
                updated.pop('max_research_cycles',None)
        result=engine.update_run(run_id, config=updated)
        from .research_iteration import view as iteration_view
        return {**result,'research_iteration':iteration_view(store,run_id)}

    @app.get('/api/runs/{run_id}/implementation-review-clock')
    def review_clock_preview(run_id: str, max_seconds: int | None = Query(default=None, ge=1, le=604800),
                             max_calls: int | None = Query(default=None, ge=1, le=10000), unlimited: bool = False):
        require_run(run_id)
        from .implementation_review_clock import preview
        try:
            return preview(store, run_id, max_seconds, max_calls, unlimited=unlimited)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/implementation-review-clock')
    @task_runtime.control
    async def review_clock_adjust(run_id: str, payload: dict):
        require_run(run_id)
        if ('preview_sha256' not in payload or set(payload) - {'preview_sha256', 'max_seconds', 'max_calls', 'unlimited'}
                or 'unlimited' in payload and type(payload['unlimited']) is not bool
                or not isinstance(payload['preview_sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', payload['preview_sha256'])
                or 'max_seconds' in payload and (type(payload['max_seconds']) is not int
                    or not 1 <= payload['max_seconds'] <= 604800)
                or 'max_calls' in payload and (type(payload['max_calls']) is not int
                    or not 1 <= payload['max_calls'] <= 10000)):
            raise HTTPException(422, '需要当前计时调整预览的完整 SHA256。')
        from .implementation_review_clock import adopt
        try:
            async with engine._lifecycle_lock:
                worker = engine.tasks.get(run_id)
                if worker is not None and not worker.done():
                    raise ValueError('研究工作线程尚未退出，请等待停止完成后再调整计时。')
                return adopt(store, run_id, payload['preview_sha256'], payload.get('max_seconds'), payload.get('max_calls'), unlimited=payload.get('unlimited', False))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/runs/{run_id}/request-retry')
    def request_retry_preview(run_id: str):
        require_run(run_id)
        return engine.request_retry_preview(run_id)

    @app.post('/api/runs/{run_id}/request-retry')
    @task_runtime.control
    async def request_retry_confirm(run_id: str, payload: RequestRetryConfirmation):
        require_run(run_id)
        try:
            return await engine.authorize_request_retry(run_id, payload.snapshot_id, payload.acknowledge_duplicate_charge)
        except (ValueError, ProviderError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/runs/{run_id}/{action}')
    @task_runtime.control
    async def action(run_id: str, action: str):
        require_run(run_id)
        try:
            if action in {'start', 'resume'}:
                return await engine.start(run_id)
            if action in {'pause', 'cancel'}:
                return await engine.stop(run_id, cancel=action == 'cancel')
            raise HTTPException(404, 'Unknown action')
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        except PolicyReviewRequired as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/runs/{run_id}/events')
    def events(run_id: str, after: int = 0):
        require_run(run_id)
        return store.events(run_id, after=after)

    @app.get('/api/runs/{run_id}/pdf/file')
    def local_pdf_file(run_id: str, source_id: str, download: bool = False):
        require_run(run_id)
        from .pdf_tools import LocalPDF
        try:
            _, original = LocalPDF(engine).original(run_id, source_id)
        except (ValueError, OSError) as exc:
            raise HTTPException(404, '该任务的本地 PDF 不可用或完整性校验未通过') from exc
        return FileResponse(original['path'], media_type='application/pdf', filename=Path(original['path']).name,
                            content_disposition_type='attachment' if download else 'inline', headers={'Cache-Control': 'no-store'})

    @app.get('/api/runs/{run_id}/stream')
    async def stream(run_id: str, request: Request, after: int = 0):
        await asyncio.to_thread(require_run, run_id)
        async def generate():
            cursor = after
            while not await request.is_disconnected():
                for event in await asyncio.to_thread(store.events, run_id, after=cursor):
                    cursor = event['id']
                    yield f'id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n'
                yield ': heartbeat\n\n'
                await asyncio.sleep(2)
        return StreamingResponse(generate(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    @app.get('/api/runs/{run_id}/report')
    def report(run_id: str):
        run = require_run(run_id)
        directory = engine.data_dir / 'runs' / run_id
        final_path = directory / 'report.md'
        final_committed = run['status'] == 'completed' or any(
            step['stage'] == 'report' and step['status'] == 'completed'
            for step in store.list('steps', run_id))
        result = {'markdown': '', 'kind': 'none', 'status': run['status'],
                  'run_id': run_id, 'run_updated_at': run['updated_at'], 'filename': None, 'title': '报告尚未生成'}
        if final_path.is_file() and final_committed:
            return {**result, 'markdown': final_path.read_text(encoding='utf-8'),
                    'kind': 'final', 'filename': 'report.md', 'title': '最终研究报告'}
        if run['status'] in {'blocked', 'failed'}:
            filenames = ('failed-report.md', 'blocked-report.md') if run['status'] == 'failed' else ('blocked-report.md',)
            for filename in filenames:
                path = directory / filename
                if path.is_file():
                    return {**result, 'markdown': path.read_text(encoding='utf-8'), 'kind': 'stage',
                            'filename': filename, 'title': '未完成：失败阶段报告' if run['status'] == 'failed' else '未完成：阻塞阶段报告'}
        return result

    @app.get('/api/runs/{run_id}/historical-report-preview')
    def historical_report_preview(run_id: str, source_cycle: int | None = Query(default=None, gt=0)):
        run = require_run(run_id)
        if run['status'] not in {'blocked', 'failed', 'paused'}:
            raise HTTPException(409, 'Historical evidence preview requires a blocked, failed or paused task')
        try:
            snapshot = build_historical_closure(store, run_id, data_dir, source_cycle)
            markdown = render_historical_closure(snapshot, [])
        except HistoricalClosureError as exc:
            raise HTTPException(409, str(exc)) from exc
        if store.get('runs', run_id) != run:
            raise HTTPException(409, 'Task changed while preparing the historical evidence preview; retry after it stops changing')
        return {'kind': 'historical_preview', 'status': run['status'], 'run_id': run_id,
                'source_cycle': snapshot['source_cycle'], 'approved': False,
                'title': '已有验证结果预览（未经三方审议）', 'markdown': markdown, 'snapshot': snapshot}

    @app.get('/api/runs/{run_id}/export')
    def export(run_id: str):
        run = require_run(run_id)
        keys = [os.environ.get(provider['api_key_env'], '') for provider in get_settings(store)['providers']]
        try:
            content = build_export(store, data_dir, run, secrets=keys)
        except ValueError as exc:
            raise HTTPException(413, str(exc))
        return StreamingResponse(iter([content]), media_type='application/zip', headers={'Content-Disposition': f'attachment; filename="research-cycle-{engine.cycle(run_id)}.zip"'})

    @app.get('/api/runs/{run_id}/idea-materials')
    def idea_materials(run_id: str, proposal_hash: str = Query(min_length=64, max_length=64)):
        run = require_run(run_id)
        if store.get('run_deletions', run_id):
            raise HTTPException(404, '研究已删除或正在删除，材料不可下载')
        if not re.fullmatch(r'[a-f0-9]{64}', proposal_hash):
            raise HTTPException(422, '提案版本标识格式不正确')
        records = store.list('proposal_reviews', run_id)
        consensus = store.get('consensus', run_id)
        if consensus:
            records.extend([consensus, *consensus.get('history', [])])
        # The pending proposal can be exported before all three votes are committed.
        from .observability import current_debate
        pending = current_debate(engine, run_id)
        if pending:
            records.append(pending)
        record = next((record for record in reversed(records) if isinstance(record, dict)
                       and record.get('proposal_hash') == proposal_hash
                       and isinstance(record.get('proposal'), dict)
                       and digest(record['proposal']) == proposal_hash), None)
        if record is None:
            raise HTTPException(404, '当前研究中未找到该版本的提案，不能用其他轮次替代')
        papers = [*engine.outputs(run_id).get('literature', {}).get('papers', []),
                  *(item['paper'] for item in store.list('run_evidence', run_id) if isinstance(item.get('paper'), dict))]
        markdown = render_idea_materials(record['proposal'], papers, assessment=record.get('evidence_assessment'),
                                        cycle=record.get('cycle'), round_number=record.get('round'))
        markdown = f'研究标识：`{run_id}`\n\n提案版本：`{proposal_hash}`\n\n' + markdown
        if store.get('runs', run_id) != run or store.get('run_deletions', run_id):
            raise HTTPException(409, '研究状态已变化，请刷新后重新下载')
        return _diagnostic_redaction({'run_id': run_id, 'proposal_hash': proposal_hash,
            'filename': f'idea-cycle-{record.get("cycle") or engine.cycle(run_id)}-round-{record.get("round") or "pending"}.md', 'markdown': markdown,
            'generation': 'deterministic_local_no_ai', 'approved': False}, diagnostic_secrets())

    @app.get('/api/memory/index')
    def memory_index(run_id: str | None = None, limit: int = Query(default=20, ge=1, le=100),
                     offset: int = Query(default=0, ge=0, le=100000)):
        if run_id:
            require_run(run_id)
        return intelligence().memory_index(run_id, limit=limit, offset=offset)

    @app.get('/api/literature/rag')
    def paper_rag_catalog(q: str = Query('', max_length=500), venue: str = Query('', max_length=500),
                          domain: str = Query('', max_length=200), topic: str = Query('', max_length=200),
                          year_from: int | None = Query(None, ge=1800, le=2200),
                          year_to: int | None = Query(None, ge=1800, le=2200),
                          ccf_rank: str = Query('', pattern=r'^(|A|B|C|__unknown__)$'),
                          sort: str = Query('relevance', pattern=r'^(relevance|publication_recent|analysis_recent|title)$'),
                          offset: int = Query(0, ge=0), limit: int = Query(24, ge=1, le=100)):
        try:
            result = engine.paper_rag.catalog(q, venue=venue, domain=domain, topic=topic, year_from=year_from,
                year_to=year_to, ccf_rank=ccf_rank, sort=sort, offset=offset, limit=limit)
            workspace_library = engine.data_dir == (ROOT / 'data').resolve()
            result['library'] = {'kind': 'workspace' if workspace_library else 'custom',
                'label': '工作空间 data 文献库' if workspace_library else engine.data_dir.name}
            return result
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get('/api/literature/rag/import-schema')
    def paper_rag_import_schema():
        from .rag_import import schema
        return schema()

    @app.post('/api/literature/rag/import')
    async def paper_rag_import(file: UploadFile = File(...), dry_run: bool = Query(True)):
        from .rag_import import import_package, MAX_PACKAGE
        from starlette.concurrency import run_in_threadpool
        try:
            body = await file.read(MAX_PACKAGE + 1)
        finally:
            await file.close()
        if len(body) > MAX_PACKAGE:
            raise HTTPException(413, 'ZIP exceeds 64 MiB')
        try:
            return await run_in_threadpool(import_package, engine.paper_rag, body, dry_run=dry_run)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get('/api/literature/rag/imports/{package_id}')
    def paper_rag_import_status(package_id: str):
        result = store.get('paper_rag_imports', package_id)
        if result is None:
            raise HTTPException(404, 'Import report not found')
        return result

    @app.get('/api/literature/rag/{version_id}')
    def paper_rag_read(version_id: str, section: str = 'analysis', offset: int = 0,
                       limit: int = 6000, expected_sha256: str | None = None):
        try:
            return engine.paper_rag.read(version_id, section=section, offset=offset, limit=limit,
                                         expected_sha256=expected_sha256)
        except (ValueError, KeyError, OSError, TypeError):
            raise HTTPException(404, '共享论文版本不可用或读取参数不正确')

    @app.get('/api/literature/rag/{version_id}/summary')
    def paper_rag_summary(version_id: str):
        from .literature_notes import NOTE_FIELDS
        try:
            saved = engine.paper_rag.load(version_id)
            from .paper_rag_curation import analysis_bundle
            document = saved['document']
            return {**analysis_bundle(engine.paper_rag, saved),
                'rag_entry_id': engine.paper_rag.canonical_id(saved['entry_id']), 'rag_version_id': version_id,
                'source': {**{key: document.get(key) for key in ('format', 'url', 'text_sha256', 'content_sha256',
                    'pages_total', 'pages_extracted', 'text_truncated', 'reported_coverage', 'missing_parts', 'extraction_method')},
                    'text_chars': len(document['text'])},
                'original_pdf_available': bool(saved.get('original') and document.get('format') == 'pdf'),
                'analysis': saved['note'] if saved.get('provenance') == 'external_agent_analysis' else {k: saved['note'][k] for k in NOTE_FIELDS},
                'origin_run_id': saved['origin_run_id'], 'origin_note_id': saved['origin_note_id'],
                'analysis_created_at': saved['analysis_created_at'], 'provenance': saved.get('provenance', 'shared_paper_rag'),
                'scientific_claims_verified': False, 'reading_scope': saved['note'].get('reading_scope', 'inspected_passages')}
        except (ValueError, KeyError, OSError, TypeError):
            raise HTTPException(404, '共享论文版本不可用')

    @app.get('/api/literature/rag/{version_id}/pdf')
    def paper_rag_pdf(version_id: str, download: bool = False):
        try:
            path = engine.paper_rag.original_pdf(version_id)
        except (ValueError, KeyError, OSError, TypeError):
            raise HTTPException(404, '共享 PDF 不可用或完整性校验未通过')
        return FileResponse(path, media_type='application/pdf', filename=path.name,
            content_disposition_type='attachment' if download else 'inline', headers={'Cache-Control': 'no-store'})

    @app.get('/api/memory')
    def memory(response: Response, q: str = Query(default='', max_length=500), run_id: str | None = None,
               limit: int = Query(default=30, ge=1, le=100), offset: int = Query(default=0, ge=0, le=10000)):
        run_id = run_id or None
        if q.strip():
            matches = intelligence().memory_search(q, run_id=run_id, limit=offset + limit + 1)
            rows = matches[offset:offset + limit]
            for row in rows:
                row.pop('features', None)
                row['text_chars'] = len(row.get('text') or '')
                row['text'] = (row.get('text') or '')[:2000]
                row['truncated'] = len(row['text']) < row['text_chars']
            response.headers['X-Has-More'] = str(len(matches) > offset + limit).lower()
            return redact(rows)
        from .page_queries import memory_page
        rows, total = memory_page(store, run_id, limit=limit, offset=offset)
        response.headers['X-Total-Count'] = str(total)
        response.headers['X-Has-More'] = str(offset + len(rows) < total).lower()
        return rows

    @app.get('/api/memory/{memory_id}')
    def memory_detail(memory_id: str, run_id: str | None = None):
        from .page_queries import memory_page
        rows, _ = memory_page(store, run_id or None, limit=1, key=memory_id)
        if not rows:
            raise HTTPException(404, 'Memory record not found')
        return rows[0]

    @app.post('/api/memory')
    def add_memory(body: MemoryCreate):
        if body.run_id:
            require_run(body.run_id)
        return intelligence().memory_add(body.run_id or 'global', body.role, body.text, body.metadata)

    @app.get('/api/literature')
    async def literature(q: str):
        if len(q.strip()) < 2:
            return []
        try:
            return await engine.literature.search(q[:500], limit=10,
                ccf_ab_only=get_settings(store)['defaults'].get('ccf_ab_only', False))
        except Exception as exc:
            raise HTTPException(502, redact(str(exc))[:600])

    @app.post('/api/literature/verify')
    async def verify(paper: dict):
        return await engine.literature.verify(paper)

    def save_reference(document):
        from .reference_papers import reference_status
        document['id'] = document.get('id') or uid()
        existing = store.get('references', document['id'], {})
        document['created_at'] = document.get('created_at') or existing.get('created_at') or now()
        document.update(reference_status(engine.paper_rag, document))
        store.put('references', document['id'], document)
        index = intelligence().memory_index_document('global', 'scout', document, source_id='reference:' + document['id'])
        return {k: v for k, v in {**document, 'text_length': len(document.get('text', '')),
            'memory_document_id': index['id'], 'index_status': index['index_status'], 'chunk_count': index['chunk_count']}.items()
            if k not in {'text', 'pages', 'original_path'}}

    reference_upload_locks = {}

    def retain_reference_upload(content, filename, suffix, content_hash):
        from .reference_papers import REFERENCE_MAX_BYTES
        from .deletion import _plain_path
        existing = store.get('references', content_hash[:24])
        if existing and existing.get('content_sha256') != content_hash:
            raise ValueError('Reference document hash collision')
        if existing and existing.get('content_sha256') == content_hash and existing.get('original_path'):
            original = Path(existing['original_path']).absolute()
            if original.is_relative_to(Path(data_dir).resolve() / 'references'):
                try:
                    _plain_path(original, Path(data_dir).resolve())
                    if original.is_file() and original.stat().st_size <= REFERENCE_MAX_BYTES:
                        body_hash = hashlib.sha256(original.read_bytes()).hexdigest()
                        if body_hash == content_hash:
                            return {**save_reference(existing), 'duplicate_upload': True}
                except (OSError, ValueError):
                    pass
        path = Path(data_dir) / 'references' / (content_hash + suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        _plain_path(path, Path(data_dir).resolve())
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != content_hash:
                raise ValueError('Stored reference original integrity check failed')
        else:
            descriptor, temporary_name = tempfile.mkstemp(prefix='.reference-', suffix='.tmp', dir=path.parent)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, 'wb') as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        # Extraction must not save a provisional reference row. One finalized
        # snapshot commits original location, hashes, pages and reuse metadata.
        document = engine.literature._document(content, filename,
            'application/pdf' if suffix == '.pdf' else 'text/plain', allow_empty_pdf=True)
        document.update(source='user_file', provenance='user_supplied_untrusted', url=None,
            file_name=Path(filename).name, original_path=str(path.resolve()))
        if existing:
            document['created_at'] = existing.get('created_at') or now()
        return {**save_reference(document), 'duplicate_upload': bool(existing)}

    async def ingest_reference_upload(file):
        from .reference_papers import REFERENCE_MAX_BYTES
        filename = file.filename or ''
        suffix = Path(filename).suffix.lower()
        if suffix not in {'.pdf', '.txt', '.md'}:
            raise HTTPException(422, 'Supported reference types: PDF, TXT, Markdown')
        content = await file.read(REFERENCE_MAX_BYTES + 1)
        if len(content) > REFERENCE_MAX_BYTES:
            raise HTTPException(413, 'Reference limit is 12 MiB per article')
        content_hash = hashlib.sha256(content).hexdigest()
        # Concurrent identical uploads share extraction and a finalized row.
        lock = reference_upload_locks.setdefault(content_hash, asyncio.Lock())
        try:
            async with lock:
                return await asyncio.to_thread(retain_reference_upload, content, filename, suffix, content_hash)
        except Exception as exc:
            raise HTTPException(422, redact(str(exc))[:600])

    @app.post('/api/references')
    async def upload_reference(file: UploadFile = File(...)):
        return await ingest_reference_upload(file)

    @app.post('/api/references/batch')
    async def upload_references(files: list[UploadFile] = File(...)):
        from .reference_papers import REFERENCE_MAX_FILES
        if not files or len(files) > REFERENCE_MAX_FILES:
            raise HTTPException(422, 'Upload between 1 and 10 reference articles')
        # Each returned reference is independently reusable across task creation;
        # a later failed item does not invalidate an already retained original.
        references = []
        for file in files:
            references.append(await ingest_reference_upload(file))
        return {'references': references}

    @app.post('/api/references/url')
    async def import_reference(body: UrlImport):
        try:
            return await asyncio.to_thread(save_reference, await engine.literature.ingest_url(body.url))
        except Exception as exc:
            raise HTTPException(422, redact(str(exc))[:600])

    @app.get('/api/model-pricing')
    def model_pricing():
        from .model_pricing import load
        current = load(store)
        models = {p['model'] for p in get_settings(store)['providers'] if p.get('protocol') == 'local_codex'}
        with store._read_db() as db:
            for row in db.execute("SELECT DISTINCT json_extract(data,'$.reported_model') AS reported, "
                    "json_extract(data,'$.requested_model') AS requested FROM records WHERE collection='codex_usage'"):
                models.update(v for v in row if isinstance(v, str) and v)
        return {**current, 'observed_models': sorted(models)}

    @app.put('/api/model-pricing')
    def update_model_pricing(payload: dict):
        from .model_pricing import save, PricingConflict
        try:
            return save(store, payload)
        except PricingConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get('/api/billing')
    def billing(run_id: str | None = None, offset: int = Query(default=0, ge=0, le=100000),
                limit: int = Query(default=30, ge=1, le=100),
                native_offset: int = Query(default=0, ge=0, le=100000),
                native_limit: int = Query(default=0, ge=0, le=100)):
        from .billing_projection import page
        return _diagnostic_redaction(page(store, run_id or None, offset=offset, limit=limit,
            native_offset=native_offset, native_limit=native_limit), diagnostic_secrets())

    dist = ROOT / 'frontend' / 'dist'
    if dist.exists():
        app.mount('/', StaticFiles(directory=dist, html=True), name='frontend')
    return app


app = create_app()
