"""Scoped observability and explicit reconciliation; no implicit replay."""
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool


class Reconciliation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    external_result_checked: StrictBool
    confirmed_not_executed: StrictBool = False
    result: dict | list | str | int | float | bool | None = None


def register(app, store, require_run):
    @app.get('/api/runs/{run_id}/context-manifests/{request_id}')
    def manifest(run_id: str, request_id: str):
        require_run(run_id)
        from .context_manifest import get_manifest, validate_manifest
        result = get_manifest(store, request_id)
        if not result or result.get('run_id') != run_id:
            raise HTTPException(404, 'Context manifest not found for this research')
        if not validate_manifest(store, result):
            raise HTTPException(409, {'code': 'context_manifest_integrity', 'recoverable': True,
                'message': 'The manifest no longer matches its frozen model input; inspect the retained request snapshot.'})
        return result

    @app.get('/api/runs/{run_id}/tool-invocations/{invocation_id}')
    def invocation(run_id: str, invocation_id: str):
        require_run(run_id)
        result = store.get('tool_invocations', invocation_id)
        if not result or result.get('run_id') != run_id:
            raise HTTPException(404, 'Tool invocation not found for this research')
        return result

    @app.post('/api/runs/{run_id}/tool-invocations/{invocation_id}/reconcile')
    def reconcile(run_id: str, invocation_id: str, body: Reconciliation):
        require_run(run_id)
        record = store.get('tool_invocations', invocation_id)
        if not record or record.get('run_id') != run_id:
            raise HTTPException(404, 'Tool invocation not found for this research')
        if not body.external_result_checked:
            raise HTTPException(422, 'Inspect the external result before reconciliation')
        if store.get('runs', run_id, {}).get('status') == 'running':
            raise HTTPException(409, 'Pause the research worker before reconciliation')
        from .tool_invocations import reconcile as resolve
        try:
            return resolve(store, invocation_id, result=body.result,
                           confirmed_not_executed=body.confirmed_not_executed)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc
