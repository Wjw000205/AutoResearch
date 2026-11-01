"""Small, durable JSONL stdio bridge to the user's installed Codex app-server.

The caller owns the durable operation record and must atomically persist every
``on_progress`` snapshot. Markers are saved *before* thread/turn dispatch. A
dispatched checkpoint is never permission to submit another turn: recovery only
reads the original thread. A reasoning initialization cancelled or rejected before
its durable account-confirmation marker can finish its first dispatch once under
the same operation.
New, explicitly approved repairs use a new operation_id and may pass the prior
thread_id. No credentials, raw RPC payloads, stderr, tool output,
or reasoning items are logged or returned by this module.

Protocol checked against local Codex 0.158.0-alpha.2.1 JSON schema and
https://learn.chatgpt.com/docs/app-server. Model/effort overrides are sent only
when explicitly selected by the caller and supported by model/list.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Callable

from .codex_executable import resolve_codex


TERMINAL = {'completed', 'failed', 'interrupted', 'blocked', 'unknown'}
PUBLIC_PHASES = {None, 'commentary', 'final_answer'}
MAX_LINE = 8 * 1024 * 1024
MAX_PUBLIC_TEXT = 200_000
RECEIPT_FIELDS = {'run_id', 'operation_id', 'workspace', 'project_root', 'instruction_sha256',
    'requested_model', 'requested_effort', 'version', 'status', 'thread_id', 'turn_id', 'public_messages',
    'usage', 'usage_scope', 'usage_observed_at', 'price_verified', 'cost_usd', 'cost_status', 'created_at', 'updated_at',
    'error_code', 'turn_dispatched_at', 'project_id', 'model', 'model_provider', 'reported_model',
    'reported_effort', 'model_reporting_scope', 'recovered_read_only', 'interrupt_requested',
    'public_output_truncated', 'activities', 'event_counts', 'last_event_at', 'server_errors',
    'first_failure', 'recovery_history', 'reasoning_only', 'output_schema_sha256', 'auth_mode',
    'output_schema_policy','effective_output_schema_sha256',
    'native_tool_violation', 'image_inputs_sha256', 'initialization_recovery', 'rpc_errors',
    'account_read_retries', 'initialization_diagnostic', 'initialization_connection_retries',
    'non_retryable_server_error', 'cleanup_error_code'}
RPC_METHODS = {'initialize', 'account/read', 'model/list', 'config/read', 'thread/read',
    'thread/turns/list', 'thread/list', 'thread/delete', 'thread/start', 'thread/resume', 'turn/start'}
RPC_ERROR_CATEGORIES = {'parse_error', 'invalid_request', 'method_not_found', 'invalid_params',
    'internal_error', 'server_error', 'other', 'malformed_error'}
# Per-thread overrides; never change the user's global Codex configuration.
REASONING_CONFIG = {
    **{f'features.{name}': False for name in (
        'shell_tool', 'unified_exec', 'apply_patch_freeform', 'apps', 'plugins',
        'multi_agent', 'multi_agent_v2', 'browser_use', 'browser_use_external',
        'computer_use', 'image_generation', 'view_image', 'code_mode_host',
        'goals', 'sleep_tool', 'memories', 'hooks', 'skill_search',
        'skill_mcp_dependency_install', 'request_permissions_tool',
        'default_mode_request_user_input', 'send_message_to_user_async',
        'workspace_dependencies', 'tool_suggest')},
    'features.code_mode.enabled': False, 'features.skip_host_skill_discovery': True,
    'agents.enabled': False, 'apps._default.enabled': False,
    'web_search': 'disabled', 'project_doc_max_bytes': 0,
}
REASONING_ITEM_TYPES = {'userMessage', 'agentMessage', 'reasoning', 'contextCompaction'}
ACTIVITY_TYPES = {'commandExecution', 'fileChange', 'contextCompaction'}
ACTIVITY_STATUSES = {'inProgress', 'completed', 'failed', 'declined'}
ERROR_CATEGORIES = {'contextWindowExceeded', 'sessionBudgetExceeded', 'usageLimitExceeded',
    'rateLimitExceeded', 'serverOverloaded', 'cyberPolicy', 'misalignmentPolicyViolation',
    'internalServerError', 'unauthorized', 'badRequest', 'threadRollbackFailed', 'sandboxError', 'other'}
STRUCTURED_ERRORS = {'httpConnectionFailed', 'responseStreamConnectionFailed',
    'responseStreamDisconnected', 'responseTooManyFailedAttempts', 'activeTurnNotSteerable'}


class LocalCodexError(RuntimeError):
    """Safe, stable error code; remote error text is intentionally not copied."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class LocalCodexBlocked(LocalCodexError):
    pass


class LocalCodexRPCError(LocalCodexError):
    """A fixed local classification, never the remote message or data."""

    def __init__(self, transient_reason=None):
        super().__init__('codex_rpc_error')
        self.transient_reason = transient_reason


ACCOUNT_RETRY_LIMIT = 5
ACCOUNT_RETRY_RECEIPT_LIMITS = {2, ACCOUNT_RETRY_LIMIT}
ACCOUNT_RETRY_REASONS = {'account_read_timeout', 'internal_error_unclassified'}
ACCOUNT_FAILURE_CATEGORIES = ACCOUNT_RETRY_REASONS | {'authentication_required', 'request_rejected'}
INITIALIZATION_CONNECTION_RETRY_LIMIT = 3
INITIALIZATION_CONNECTION_REASONS = {
    'initialize_timeout', 'initialize_internal_error_unclassified',
    *ACCOUNT_RETRY_REASONS, 'initialization_transport_closed',
    'initialization_transport_eof', 'initialization_read_timeout'}


def _account_failure_category(method, code, message):
    if method != 'account/read' or code != -32603 or not isinstance(message, str) or not message.strip():
        return None
    # This classification uses the newly received message without retaining it.
    # Login and request rejection take precedence over all retryable reads.
    if re.search(r'\b(?:401|403|unauthorized|forbidden|invalid[_ -](?:grant|token|credentials|'
                 r'client)|access[_ -]denied|(?:login|authentication|interaction|consent)[_ -]required|'
                 r'refresh[_ -]token[_ -]reused|token[_ -](?:expired|invalid|revoked)|expired|revoked|'
                 r'log in|sign in|not (?:logged|signed) in|'
                 r'authentication failed|permission denied)\b',
                 message, re.IGNORECASE):
        return 'authentication_required'
    if re.search(r'\binvalid[_ -](?:params|parameters|request)\b', message, re.IGNORECASE):
        return 'request_rejected'
    if re.search(r'\b(?:timed out|timeout|deadline exceeded)\b', message, re.IGNORECASE):
        return 'account_read_timeout'
    # An internal read error is not proof of a transient cause. A bounded repeat
    # of this read is nevertheless safe before the durable auth/dispatch markers.
    return 'internal_error_unclassified'


def _valid_account_retries(value):
    return (isinstance(value, list) and len(value) <= ACCOUNT_RETRY_LIMIT and all(
        isinstance(item, dict) and set(item) == {'reason', 'attempted_at'}
        and isinstance(item['reason'], str) and item['reason'] in ACCOUNT_RETRY_REASONS
        and isinstance(item['attempted_at'], str) and bool(item['attempted_at']) for item in value))


def _valid_initialization_connection_retries(value):
    return (isinstance(value, list) and len(value) <= INITIALIZATION_CONNECTION_RETRY_LIMIT and all(
        isinstance(item, dict) and set(item) == {'reason', 'attempted_at'}
        and isinstance(item['reason'], str) and item['reason'] in INITIALIZATION_CONNECTION_REASONS
        and isinstance(item['attempted_at'], str) and bool(item['attempted_at']) for item in value))


def _initialize_failure_category(code, message):
    category = _account_failure_category('account/read', code, message)
    return {'account_read_timeout': 'initialize_timeout',
        'internal_error_unclassified': 'initialize_internal_error_unclassified'}.get(category, category)


def _valid_initialization_diagnostic(value):
    if not (isinstance(value, dict) and set(value) == {
        'stage', 'rpc_method', 'category', 'retry_count', 'retry_limit', 'next_action', 'observed_at'}
        and all(isinstance(value[name], str) for name in (
            'stage', 'rpc_method', 'category', 'next_action', 'observed_at'))
        and value['stage'] in {'initialize', 'account_read', 'authenticated', 'reconnect'}
        and value['rpc_method'] in ({'initialize', 'account/read'} if value['stage'] == 'reconnect'
            else {'initialize'} if value['stage'] == 'initialize' else {'account/read'})
        and type(value['retry_limit']) is int
        and type(value['retry_count']) is int and 0 <= value['retry_count'] <= value['retry_limit']
        and bool(value['observed_at'])):
        return False
    if value['stage'] == 'reconnect':
        return (value['retry_limit'] == INITIALIZATION_CONNECTION_RETRY_LIMIT
            and value['category'] in INITIALIZATION_CONNECTION_REASONS
            and value['next_action'] in {'retry_initialization_connection', 'retry_exhausted'})
    categories = ACCOUNT_FAILURE_CATEGORIES | {'checking', 'ready', 'rpc_error'}
    if value['stage'] == 'initialize':
        categories |= {'initialize_timeout', 'initialize_internal_error_unclassified'}
    return (value['retry_limit'] in ACCOUNT_RETRY_RECEIPT_LIMITS
        and value['category'] in categories
        and value['next_action'] in {'retry_account_read', 'check_login', 'retry_exhausted',
            'inspect_initialization', 'read_original_turn', 'continue'})


async def _account_retry_wait(seconds):
    """Cooperative backoff; callers persist the consumed allowance before waiting."""
    await asyncio.sleep(seconds)


async def _initialization_connection_retry_wait(seconds):
    """Cancellable backoff after the reconnect allowance is durably consumed."""
    await asyncio.sleep(seconds)


def child_environment(blocked_env_names=(), *, environ=None):
    """Keep Codex's native login/config; remove known project provider secrets.

    Pass every configured AutoResearch ``api_key_env`` in blocked_env_names.
    Native Codex authentication files are neither read nor copied.
    """
    blocked = {str(name).upper() for name in blocked_env_names}
    return {key: value for key, value in (os.environ if environ is None else environ).items()
            if key.upper() not in blocked and not
            (key.upper().startswith(('RUIKON_', 'MIAOAI_', 'AUTORESEARCH_'))
             and any(part in key.upper() for part in ('KEY', 'TOKEN', 'SECRET')))}


def find_codex():
    found = resolve_codex()
    if not found['available']:
        raise LocalCodexBlocked(found['error_code'])
    return found['executable']


async def spawn_codex(*arguments, executable=None, process_factory=None, **options):
    """Retry discovery once only if process creation never happened.

    No initialize/thread/turn RPC is retried here. Permissions and other startup
    failures are not mistaken for a desktop-update path race.
    """
    factory = process_factory or asyncio.create_subprocess_exec
    excluded = []
    for attempt in range(2):
        found = ({'available': True, 'executable': executable, 'source': 'client_override'}
                 if executable else resolve_codex(exclude=excluded))
        if not found['available']:
            raise LocalCodexBlocked(found['error_code'])
        try:
            process = await factory(found['executable'], *arguments, **options)
            return process, found
        except FileNotFoundError:
            if executable or found['source'] == 'explicit_configuration' or attempt:
                raise LocalCodexBlocked('codex_process_start_failed') from None
            excluded.append(found['executable'])
        except OSError:
            raise LocalCodexBlocked('codex_process_start_failed') from None


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _directory(path):
    raw = Path(path).absolute()
    for entry in (raw, *raw.parents):
        if entry.is_symlink() or getattr(entry, 'is_junction', lambda: False)():
            raise LocalCodexBlocked('codex_workspace_link_not_allowed')
    resolved = raw.resolve()
    if not resolved.is_dir():
        raise ValueError('codex_workspace_missing')
    return resolved


async def _invoke(function, *args):
    if function is not None:
        if inspect.iscoroutinefunction(function):
            return await function(*args)
        # Native notifications can arrive in bursts. SQLite checkpoints and
        # active-state reads must yield to pause/cancel and other connections.
        worker = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            value = await asyncio.shield(worker)
        except asyncio.CancelledError:
            # A cancelled await cannot stop a persistence thread. Drain it
            # before releasing the request lock or writing terminal state.
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
        if inspect.isawaitable(value):
            return await value
        return value


async def _await_cleanup(task):
    """Finish owned transport cleanup before propagating cancellation."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    if cancelled:
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


async def close_owned_process(process, *, stderr_draining=False):
    """Reap this child only, discarding pipe data without retaining its contents."""
    async def discard(stream):
        while await stream.read(8192):
            pass

    async def finish():
        streams = [getattr(process, 'stdout', None)]
        if not stderr_draining:
            streams.append(getattr(process, 'stderr', None))
        drains = [asyncio.create_task(discard(stream)) for stream in streams if stream is not None]
        try:
            if process.stdin:
                try:
                    process.stdin.close()
                except (BrokenPipeError, ConnectionResetError, ProcessLookupError):
                    pass
            for action in (None, process.terminate, process.kill):
                if action is not None:
                    try:
                        action()
                    except ProcessLookupError:
                        pass  # Exit can race with either signal; still reap it.
                try:
                    await asyncio.wait_for(process.wait(), 3)
                    return
                except asyncio.TimeoutError:
                    continue
            raise LocalCodexError('codex_process_shutdown_timeout')
        finally:
            for task in drains:
                task.cancel()
            await asyncio.gather(*drains, return_exceptions=True)

    await _await_cleanup(asyncio.create_task(finish()))


async def close_after_operation(client):
    """Use in a finally block: cleanup must not replace an active failure."""
    primary_error = sys.exception()
    try:
        await client._close()
    except (LocalCodexError, OSError):
        if primary_error is None:
            raise
        primary_error.add_note('codex_process_shutdown_failed')


class LocalCodexClient:
    """One process per operation/readback, never attached to the current chat.

    ``run`` returns a receipt for protocol/remote failure, with status blocked or
    unknown. Invalid caller arguments and checkpoint mismatches raise before any
    subprocess. Persistence/check_active exceptions propagate; no retry occurs.
    asyncio cancellation persists unknown and interrupts the owned turn when its
    ID is known. Callers must not turn unknown into a new operation automatically.
    """

    def __init__(self, executable=None, *, timeout=900, request_timeout=30,
                 blocked_env_names=(), process_factory=None):
        if not 1 <= timeout <= 86400 or not 0.05 <= request_timeout <= 300:
            raise ValueError('invalid_codex_timeout')
        self.executable = executable
        self.timeout = timeout
        self.request_timeout = request_timeout
        self.blocked_env_names = tuple(blocked_env_names)
        self.process_factory = process_factory or asyncio.create_subprocess_exec
        self._process = None
        self._stderr_task = None
        self._counter = 0
        self._state = None
        self._progress = None
        self._check = None
        self._interrupt_requested = False
        self._turn_dispatched_here = False
        self._busy = False
        self._close_task = None
        self._secrets = ()

    def _public_text(self, value, *, response=False):
        text = str(value) if response else str(value)[:MAX_PUBLIC_TEXT]
        for secret in self._secrets:
            text = text.replace(secret, '[REDACTED]')
        return re.sub(r'\bsk-[A-Za-z0-9_-]{12,}', '[REDACTED]', text)

    async def _save(self, **updates):
        self._state.update(updates, updated_at=_now())
        if (self._state.get('status') in {'unknown', 'blocked', 'failed'}
                and self._state.get('error_code') and not self._state.get('first_failure')):
            self._state['first_failure'] = {'status': self._state['status'],
                'error_code': self._state['error_code'], 'observed_at': _now(),
                'thread_id': self._state.get('thread_id'), 'turn_id': self._state.get('turn_id')}
        await _invoke(self._progress, copy.deepcopy(self._state))

    async def _write(self, value):
        if self._process is None or self._process.returncode is not None:
            raise LocalCodexError('codex_transport_closed')
        try:
            self._process.stdin.write((json.dumps(value, ensure_ascii=False) + '\n').encode('utf-8'))
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            raise LocalCodexError('codex_transport_closed') from None

    async def _deny(self, message):
        method = message.get('method', '')
        if method in {'item/commandExecution/requestApproval', 'item/fileChange/requestApproval'}:
            reply = {'result': {'decision': 'cancel'}}
        elif method == 'item/permissions/requestApproval':
            reply = {'result': {'permissions': {}, 'scope': 'turn'}}
        else:
            reply = {'error': {'code': -32601, 'message': 'Client does not authorize server requests'}}
        await self._write({'id': message['id'], **reply})
        # Do not persist the requested command, permission object or credentials.
        raise LocalCodexBlocked('codex_permission_or_user_input_required')

    async def _receive(self, deadline):
        while True:
            await _invoke(self._check)
            if self._interrupt_requested:
                self._interrupt_requested = False
                await self._interrupt_owned()
                await self._save(interrupt_requested=True)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise LocalCodexError('codex_read_timeout')
            try:
                line = await asyncio.wait_for(self._process.stdout.readline(), min(0.5, remaining))
            except asyncio.TimeoutError:
                continue
            except (ValueError, asyncio.LimitOverrunError):
                raise LocalCodexError('codex_protocol_message_too_large') from None
            if not line:
                raise LocalCodexError('codex_transport_eof')
            if len(line) > MAX_LINE:
                raise LocalCodexError('codex_protocol_message_too_large')
            try:
                message = json.loads(line)
            except (ValueError, UnicodeError):
                raise LocalCodexError('codex_invalid_json') from None
            if not isinstance(message, dict):
                raise LocalCodexError('codex_invalid_message')
            if 'id' in message and 'method' in message:
                await self._deny(message)
            return message

    async def _rpc(self, method, params):
        self._counter += 1
        request_id = self._counter
        await self._write({'id': request_id, 'method': method, 'params': params})
        deadline = asyncio.get_running_loop().time() + self.request_timeout
        while True:
            message = await self._receive(deadline)
            if message.get('id') == request_id:
                if 'error' in message:
                    error = message['error']
                    # Remote messages/data may contain credentials or prompts. Retain
                    # only our own method/code/category and a fixed timeout tag.
                    code = error.get('code') if isinstance(error, dict) else None
                    code = code if type(code) is int and -(2**31) <= code < 2**31 else None
                    category = ({-32700: 'parse_error', -32600: 'invalid_request',
                        -32601: 'method_not_found', -32602: 'invalid_params',
                        -32603: 'internal_error'}.get(code,
                        'server_error' if code is not None and -32099 <= code <= -32000 else 'other')
                        if isinstance(error, dict) else 'malformed_error')
                    remote_message = error.get('message', '') if isinstance(error, dict) else ''
                    failure_category = (_initialize_failure_category(code, remote_message) if method == 'initialize'
                        else _account_failure_category(method, code, remote_message))
                    transient_reason = (failure_category if failure_category in INITIALIZATION_CONNECTION_REASONS else None)
                    if self._state is not None:
                        failures = self._state.setdefault('rpc_errors', [])
                        diagnostic = {'method': method if method in RPC_METHODS else 'unknown',
                            'code': code, 'category': category, 'observed_at': _now()}
                        if transient_reason:
                            diagnostic['transient_reason'] = transient_reason
                        if failure_category:
                            diagnostic['failure_category'] = failure_category
                        failures.append(diagnostic)
                        del failures[:-12]
                        updates = {}
                        if method in {'initialize', 'account/read'}:
                            updates['initialization_diagnostic'] = self._initialization_diagnostic(
                                'initialize' if method == 'initialize' else 'account_read',
                                failure_category or 'rpc_error',
                                'check_login' if failure_category == 'authentication_required'
                                    else 'inspect_initialization')
                        await self._save(**updates)
                    if 'already has an active writer' in str(remote_message):
                        raise LocalCodexBlocked('codex_thread_writer_busy')
                    if method == 'thread/read' and 'thread not loaded:' in str(remote_message):
                        raise LocalCodexBlocked('codex_thread_not_found')
                    raise LocalCodexRPCError(transient_reason)
                if not isinstance(message.get('result'), dict):
                    raise LocalCodexError('codex_invalid_response')
                return message['result']
            if 'id' not in message:
                await self._event(message)

    async def _drain_stderr(self):
        # Prevent a full stderr pipe from deadlocking the server; never retain it.
        while await self._process.stderr.read(8192):
            pass

    async def _open(self, workspace):
        if self._state is not None:
            await self._save(initialization_diagnostic=self._initialization_diagnostic(
                'initialize', 'checking', 'continue'))
        environment = child_environment(self.blocked_env_names)
        self._secrets = tuple(value for key, value in os.environ.items()
                              if key not in environment and len(value) >= 8)
        options = {'stdin': asyncio.subprocess.PIPE, 'stdout': asyncio.subprocess.PIPE,
                   'stderr': asyncio.subprocess.PIPE, 'cwd': str(workspace), 'env': environment,
                   'limit': MAX_LINE}
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NO_WINDOW
        self._process, _ = await spawn_codex('app-server', '--stdio', executable=self.executable,
                                           process_factory=self.process_factory, **options)
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self._rpc('initialize', {'clientInfo': {'name': 'autoresearch_local',
            'title': 'AutoResearch 本机代码实现', 'version': '1.0'},
            'capabilities': {'experimentalApi': False}})
        await self._write({'method': 'initialized'})
        if self._state is not None:
            await self._save(initialization_diagnostic=self._initialization_diagnostic(
                'initialize', 'ready', 'continue'))

    def _initialization_diagnostic(self, stage, category, next_action, *, retry_count=None):
        reconnect = stage == 'reconnect'
        retries = self._state.get('initialization_connection_retries' if reconnect else 'account_read_retries', [])
        valid = _valid_initialization_connection_retries if reconnect else _valid_account_retries
        count = len(retries) if valid(retries) else 0
        method = 'initialize' if stage == 'initialize' else 'account/read'
        if reconnect:
            method = ('account/read' if category in ACCOUNT_RETRY_REASONS else 'initialize'
                if category in {'initialize_timeout', 'initialize_internal_error_unclassified'} else
                self._state.get('initialization_diagnostic', {}).get('rpc_method', 'initialize'))
        return {'stage': stage, 'rpc_method': method,
            'category': category, 'retry_count': count if retry_count is None else retry_count,
            'retry_limit': INITIALIZATION_CONNECTION_RETRY_LIMIT if reconnect else ACCOUNT_RETRY_LIMIT,
            'next_action': next_action, 'observed_at': _now()}

    async def _close(self):
        async def finish():
            process, stderr = self._process, self._stderr_task
            try:
                if process is not None:
                    await close_owned_process(process, stderr_draining=stderr is not None)
                self._process = None
            finally:
                if stderr:
                    stderr.cancel()
                    await asyncio.gather(stderr, return_exceptions=True)
                self._stderr_task = None

        if self._close_task is None:
            self._close_task = asyncio.create_task(finish())
        task = self._close_task
        try:
            await _await_cleanup(task)
        finally:
            if task.done():
                self._close_task = None

    def _item(self, item):
        if not isinstance(item, dict) or item.get('type') != 'agentMessage':
            return
        if item.get('phase') not in PUBLIC_PHASES or not isinstance(item.get('text'), str):
            return
        identifier = item.get('id')
        if not isinstance(identifier, str):
            return
        messages = self._state['public_messages']
        previous = next((value for value in messages if value['id'] == identifier), None)
        if self._state.get('reasoning_only'):
            value={'id':identifier,'phase':item.get('phase'),'text':self._public_text(item['text'],response=True)}
            if previous:previous.update(value)
            else:messages.append(value)
            return
        available = max(0, MAX_PUBLIC_TEXT - sum(len(value['text']) for value in messages if value is not previous))
        text = self._public_text(item['text'])
        if len(item['text']) > available:
            self._state['public_output_truncated'] = True
        value = {'id': identifier, 'phase': item.get('phase'), 'text': text[:available]}
        if previous:
            previous.update(value)
        elif len(messages) < 200:
            messages.append(value)
        else:
            self._state['public_output_truncated'] = True

    def _activity(self, item, *, source='stream', lifecycle=None):
        if (self._state.get('reasoning_only') and isinstance(item, dict)
                and item.get('type') not in REASONING_ITEM_TYPES):
            # Defense in depth for a native version ignoring an override. Never
            # accept output produced using native tools; interrupt the owned turn.
            self._state['native_tool_violation'] = {'type': str(item.get('type')), 'observed_at': _now()}
            raise LocalCodexBlocked('codex_native_tools_forbidden')
        if not isinstance(item, dict) or item.get('type') not in ACTIVITY_TYPES or not isinstance(item.get('id'), str):
            return False
        value = {'id': item['id'], 'type': item['type'], 'source': source, 'observed_at': _now()}
        if item.get('status') in ACTIVITY_STATUSES:
            value['status'] = item['status']
        elif lifecycle is not None:
            value['status'] = lifecycle
        if type(item.get('exitCode')) is int:
            value['exit_code'] = item['exitCode']
        from .code_activity import describe
        value.update(describe(item, self._state.get('workspace', '.'), self._public_text))
        activities = self._state.setdefault('activities', [])
        previous = next((entry for entry in activities if entry['id'] == item['id']), None)
        if previous and item['type'] == 'commandExecution' and not item.get('command'):
            value['summary'] = previous.get('summary', value['summary'])
        if lifecycle == 'inProgress' and not (previous or {}).get('started_at'):
            value['started_at'] = value['observed_at']
        if source == 'stream' and value.get('status') in {'completed', 'failed', 'declined'}:
            value['completed_at'] = value['observed_at']
            if previous and previous.get('started_at') and 'duration_seconds' not in value:
                value['duration_seconds'] = max(0, (datetime.fromisoformat(value['observed_at'])
                    - datetime.fromisoformat(previous['started_at'])).total_seconds())
        if previous is not None:
            previous.update(value)
        else:
            activities.append(value)
            del activities[:-100]
        return True

    def _server_error(self, error, *, will_retry=False):
        info = error.get('codexErrorInfo') if isinstance(error, dict) else None
        value = {'category': info if isinstance(info, str) and info in ERROR_CATEGORIES else 'unclassified',
                 'will_retry': will_retry is True, 'observed_at': _now()}
        if isinstance(info, dict):
            for category in STRUCTURED_ERRORS:
                if category in info:
                    value['category'] = category
                    details = info[category]
                    status = details.get('httpStatusCode') if isinstance(details, dict) else None
                    if type(status) is int and 100 <= status <= 599:
                        value['http_status'] = status
                    break
        errors = self._state.setdefault('server_errors', [])
        errors.append(value)
        from .native_failure import transient_error
        if any(not transient_error(item) for item in errors):
            # A bounded diagnostic tail must not erase a denial and turn it
            # into retry permission after enough subsequent transient notices.
            self._state['non_retryable_server_error'] = True
        del errors[:-12]

    def _turn(self, turn, *, source='stream'):
        if not isinstance(turn, dict) or not isinstance(turn.get('id'), str):
            raise LocalCodexError('codex_invalid_turn')
        if self._state.get('turn_id') and self._state['turn_id'] != turn['id']:
            raise LocalCodexBlocked('codex_turn_identity_mismatch')
        self._state['turn_id'] = turn['id']
        for item in turn.get('items', []):
            self._item(item)
            self._activity(item, source=source)
        status = turn.get('status')
        if status in {'completed', 'failed', 'interrupted'}:
            self._state['status'] = status
            self._state['error_code'] = 'codex_turn_failed' if status == 'failed' else None
            if status == 'failed':
                # Missing terminal error is unclassified, not proof that a
                # previous transient error also explains this terminal result.
                self._server_error(turn.get('error'))
        elif status == 'inProgress' and self._state['status'] not in {'completed', 'failed', 'interrupted'}:
            self._state['status'] = 'running'
        elif status != 'inProgress':
            raise LocalCodexError('codex_unknown_turn_status')

    async def _event(self, message):
        if self._state is None:
            return
        method, params = message.get('method', ''), message.get('params', {})
        if not isinstance(params, dict) or params.get('threadId') != self._state.get('thread_id'):
            return
        turn_id = params.get('turnId') or (params.get('turn') or {}).get('id')
        if self._state.get('turn_id') and turn_id != self._state['turn_id']:
            return
        if method in {'turn/started', 'turn/completed'}:
            self._turn(params.get('turn'))
        elif method in {'item/started', 'item/completed'}:
            item = params.get('item')
            activity = self._activity(item, lifecycle='inProgress' if method == 'item/started' else 'completed')
            public = (isinstance(item, dict) and item.get('type') == 'agentMessage'
                      and item.get('phase') in PUBLIC_PHASES)
            if method == 'item/completed' and public:
                self._item(item)
            if not activity and not public:
                return
        elif method == 'error':
            self._server_error(params.get('error'), will_retry=params.get('willRetry'))
        elif method == 'thread/tokenUsage/updated':
            usage = params.get('tokenUsage', {})
            self._state['usage'] = {group: {key: value for key, value in usage.get(group, {}).items()
                if key in {'inputTokens', 'outputTokens', 'cachedInputTokens', 'cacheWriteInputTokens',
                           'reasoningOutputTokens', 'totalTokens'} and type(value) is int and value >= 0}
                for group in ('last', 'total') if isinstance(usage.get(group), dict)}
            self._state['usage_scope'] = 'thread_cumulative_and_last_model_call_not_verified_turn_total'
            self._state['usage_observed_at'] = _now()
        else:
            # No reasoning, plan deltas, commands, raw tool results or stderr.
            return
        counts = self._state.setdefault('event_counts', {})
        counts[method] = counts.get(method, 0) + 1
        self._state['last_event_at'] = _now()
        await self._save()

    def _verify_thread(self, thread, *, thread_id=None):
        if not isinstance(thread, dict) or not isinstance(thread.get('id'), str):
            raise LocalCodexError('codex_invalid_thread')
        if thread_id and thread['id'] != thread_id:
            raise LocalCodexBlocked('codex_thread_identity_mismatch')
        try:
            cwd = str(Path(thread['cwd']).resolve())
        except (KeyError, TypeError, ValueError):
            raise LocalCodexBlocked('codex_thread_workspace_unverifiable') from None
        if cwd not in {self._state['workspace'], self._state['project_root']}:
            raise LocalCodexBlocked('codex_thread_workspace_mismatch')

    async def _read(self, thread_id):
        response = await self._rpc('thread/read', {'threadId': thread_id, 'includeTurns': True})
        thread = response.get('thread')
        self._verify_thread(thread, thread_id=thread_id)
        if thread.get('historyMode', 'legacy') != 'legacy':
            # Don't pretend a summary is a full read; bounded paginated hydration.
            turns, cursor = [], None
            for _ in range(20):
                page = await self._rpc('thread/turns/list', {'threadId': thread_id,
                    'limit': 100, 'itemsView': 'full', 'sortDirection': 'asc', 'cursor': cursor})
                turns.extend(page.get('data', []))
                cursor = page.get('nextCursor')
                if not cursor:
                    break
            if cursor:
                raise LocalCodexBlocked('codex_thread_history_limit')
            thread['turns'] = turns
        return thread

    async def delete_completed_reasoning_thread(self, checkpoint, workspace):
        """Delete only an owned, single completed reasoning turn after host capture.

        This never starts/resumes a turn. Reused/user-extended threads, uncertain
        results and native-tool sessions are retained for manual inspection.
        """
        workspace = _directory(workspace)
        if self._process is not None and any((self._state or {}).get(k) != checkpoint.get(k)
                for k in ('thread_id', 'turn_id', 'operation_id', 'workspace')):
            raise LocalCodexBlocked('codex_thread_cleanup_not_owned')
        if (self._busy or self._close_task is not None or checkpoint.get('status') != 'completed'
                or checkpoint.get('reasoning_only') is not True
                or checkpoint.get('auth_mode') != 'chatgpt'
                or checkpoint.get('public_output_truncated') or checkpoint.get('native_tool_violation')
                or checkpoint.get('workspace') != str(workspace)
                or checkpoint.get('project_root') != str(workspace)
                or not checkpoint.get('thread_id') or not checkpoint.get('turn_id')):
            raise LocalCodexBlocked('codex_thread_cleanup_not_eligible')
        self._state = copy.deepcopy(checkpoint)
        self._progress, self._check = None, None
        self._busy = True
        try:
            async with asyncio.timeout(self.request_timeout * 3):
                if self._process is None:
                    await self._open(workspace)
                try:
                    thread = await self._read(checkpoint['thread_id'])
                except LocalCodexBlocked as exc:
                    if exc.code != 'codex_thread_not_found':
                        raise
                    # Deletion can remove persistence before the server loses
                    # its reply. Verify absence in both active/archived lists;
                    # never recreate or resume a thread to perform cleanup.
                    for archived in (False, True):
                        page = await self._rpc('thread/list', {'cwd': str(workspace),
                            'archived': archived, 'limit': 100})
                        if (not isinstance(page.get('data'), list) or page.get('nextCursor')
                                or any(t.get('id') == checkpoint['thread_id'] for t in page['data'])):
                            raise LocalCodexBlocked('codex_thread_cleanup_absence_unconfirmed')
                    return {'status': 'deleted', 'thread_id': checkpoint['thread_id'],
                            'deleted_at': _now(), 'confirmed_absent': True}
                turns = thread.get('turns', [])
                if (len(turns) != 1 or turns[0].get('id') != checkpoint['turn_id']
                        or turns[0].get('status') != 'completed' or thread.get('isPinned') is True
                        or (thread.get('status') or {}).get('type') == 'active'):
                    raise LocalCodexBlocked('codex_thread_changed_after_capture')
                public = [i for i in turns[0].get('items', []) if i.get('type') == 'agentMessage'
                          and i.get('phase') in PUBLIC_PHASES]
                retained = {m['id']: m for m in checkpoint.get('public_messages', []) if m.get('id')}
                if not public or any(i.get('id') not in retained or self._public_text(i.get('text', ''))
                        != retained[i['id']].get('text') for i in public):
                    raise LocalCodexBlocked('codex_thread_output_not_fully_captured')
                await self._rpc('thread/delete', {'threadId': checkpoint['thread_id']})
                return {'status': 'deleted', 'thread_id': checkpoint['thread_id'], 'deleted_at': _now()}
        finally:
            try:
                await close_after_operation(self)
            finally:
                self._busy = False

    async def _models(self):
        result, cursor = [], None
        for _ in range(10):
            page = await self._rpc('model/list', {'limit': 100, 'includeHidden': False, 'cursor': cursor})
            if not isinstance(page.get('data'), list):
                raise LocalCodexError('codex_invalid_model_catalog')
            for item in page['data']:
                if not isinstance(item, dict) or not isinstance(item.get('id'), str):
                    continue
                result.append({'id': item['id'], 'model': item.get('model'),
                    'display_name': self._public_text(item.get('displayName', item['id'])),
                    'supported_reasoning_efforts': [option['reasoningEffort']
                        for option in item.get('supportedReasoningEfforts', [])
                        if isinstance(option, dict) and isinstance(option.get('reasoningEffort'), str)],
                    'default_reasoning_effort': item.get('defaultReasoningEffort'),
                    'is_default': item.get('isDefault') is True})
            cursor = page.get('nextCursor')
            if not cursor:
                return result
        raise LocalCodexBlocked('codex_model_catalog_limit')

    async def models(self, workspace=None):
        """Read-only catalog; no thread creation or generation and no account data."""
        if self._busy or self._process is not None or self._close_task is not None:
            raise LocalCodexBlocked('codex_client_already_running')
        self._busy = True
        self._state, self._check, self._progress = None, None, None
        try:
            await self._open(Path(workspace or Path.cwd()).resolve())
            return await self._models()
        finally:
            try:
                await close_after_operation(self)
            finally:
                self._busy = False

    async def account_status(self, workspace=None):
        """Read native login and catalog, never return tokens or account identity."""
        if self._busy or self._process is not None or self._close_task is not None:
            raise LocalCodexBlocked('codex_client_already_running')
        self._busy = True
        self._state, self._check, self._progress = None, None, None
        try:
            await self._open(Path(workspace or Path.cwd()).resolve())
            response = await self._rpc('account/read', {'refreshToken': False})
            account = response.get('account') or {}
            return {'auth_mode': account.get('type'), 'plan_type': account.get('planType'),
                    'models': await self._models()}
        finally:
            try:
                await close_after_operation(self)
            finally:
                self._busy = False

    @staticmethod
    def _model_choice(catalog, model, effort):
        selected = [item for item in catalog if model in {item['id'], item.get('model')}]
        if len(selected) != 1:
            raise LocalCodexBlocked('codex_selected_model_unavailable')
        if effort is not None and effort not in selected[0]['supported_reasoning_efforts']:
            raise LocalCodexBlocked('codex_selected_effort_unsupported')
        return selected[0]['model'] or selected[0]['id']

    async def _recover(self):
        # Never retain an older successful readback marker across a new read
        # that fails, cannot locate the turn, or finds it still running.
        await self._save(recovered_read_only=False)
        history = self._state.setdefault('recovery_history', [])
        history.append({'previous_status': self._state.get('status'),
            'previous_error_code': self._state.get('error_code'), 'observed_at': _now(),
            'thread_id': self._state.get('thread_id'), 'turn_id': self._state.get('turn_id')})
        del history[:-10]
        thread_id = self._state.get('thread_id')
        if not thread_id:
            await self._save(status='blocked', error_code='codex_dispatch_without_thread_id')
            return
        thread = await self._read(thread_id)
        self._state.update(reported_model=thread.get('model'), reported_effort=thread.get('reasoningEffort'),
            model_reporting_scope='thread_configuration_not_per_turn_telemetry')
        turns = thread.get('turns', [])
        if not isinstance(turns, list):
            raise LocalCodexError('codex_invalid_history')
        turn_id = self._state.get('turn_id')
        if turn_id:
            matches = [turn for turn in turns if turn.get('id') == turn_id]
        else:
            matches = [turn for turn in turns if any(isinstance(item, dict)
                       and item.get('type') == 'userMessage'
                       and item.get('clientId') == self._state['operation_id']
                       for item in turn.get('items', []))]
        if len(matches) != 1:
            await self._save(status='unknown', error_code='codex_original_turn_not_uniquely_found')
            return
        clients = {item.get('clientId') for item in matches[0].get('items', [])
                   if isinstance(item, dict) and item.get('type') == 'userMessage'
                   and item.get('clientId') is not None}
        if clients and self._state['operation_id'] not in clients:
            raise LocalCodexBlocked('codex_original_turn_operation_mismatch')
        self._turn(matches[0], source='readback')
        if matches[0].get('status') == 'inProgress' or self._state['status'] == 'running':
            await self._save(status='unknown', error_code='codex_original_turn_still_in_progress',
                             recovered_read_only=False)
        else:
            if self._state['status'] == 'interrupted' and not self._state.get('first_failure'):
                # A killed host may only have saved a running checkpoint. Record
                # the interruption when native readback proves it, never at the
                # earlier dispatch/restart time. A later explicit resume still
                # has to pass the ordinary continuation authorization gates.
                self._state['first_failure'] = {'status': 'interrupted', 'error_code': None,
                    'observed_at': _now(), 'thread_id': thread_id, 'turn_id': self._state['turn_id'],
                    'source': 'readback'}
            await self._save(recovered_read_only=True)

    @staticmethod
    def _unsubmitted_initialization(checkpoint, reasoning_only):
        """The auth marker is persisted before any reasoning thread/turn RPC."""
        if (not reasoning_only or not isinstance(checkpoint, dict)
                or set(checkpoint) - RECEIPT_FIELDS
                or checkpoint.get('version') != 1
                or checkpoint.get('reasoning_only') is not True
                or checkpoint.get('cleanup_error_code') not in (None,
                    'codex_process_shutdown_timeout', 'codex_process_shutdown_failed')
                or checkpoint.get('status') != 'unknown'
                or checkpoint.get('error_code') not in {
                    'codex_cancelled_result_unknown', 'codex_rpc_error',
                    'codex_transport_closed', 'codex_transport_eof', 'codex_read_timeout'}):
            return False
        failure = checkpoint.get('first_failure') or {}
        if (not isinstance(failure, dict) or failure.get('status') != 'unknown'
                or failure.get('error_code') != checkpoint.get('error_code')
                or failure.get('thread_id') or failure.get('turn_id')):
            return False
        # Missing thread/turn IDs alone do not establish a safe retry. Once auth
        # succeeded, a lost thread-start acknowledgement is an uncertain dispatch.
        if any(name in checkpoint for name in ('auth_mode', 'turn_dispatched_at',
                'project_id', 'model', 'model_provider', 'reported_model', 'reported_effort',
                'model_reporting_scope', 'usage_scope', 'usage_observed_at', 'initialization_recovery')):
            return False
        if (checkpoint.get('price_verified', False) is not False
                or checkpoint.get('cost_usd') is not None
                or checkpoint.get('cost_status') not in (None, 'not_reported_by_local_codex')):
            return False
        # New receipts identify the failed RPC. Old receipts may lack diagnostics,
        # so the durable pre-auth invariant above remains necessary in both cases.
        rpc_errors = checkpoint.get('rpc_errors', [])
        if not isinstance(rpc_errors, list) or len(rpc_errors) > 12:
            return False
        if not _valid_account_retries(checkpoint.get('account_read_retries', [])):
            return False
        if not _valid_initialization_connection_retries(checkpoint.get('initialization_connection_retries', [])):
            return False
        if 'initialization_diagnostic' in checkpoint:
            diagnostic = checkpoint['initialization_diagnostic']
            if (not _valid_initialization_diagnostic(diagnostic)
                    or diagnostic['stage'] == 'authenticated'
                    or diagnostic['retry_count'] != len(checkpoint.get(
                        'initialization_connection_retries' if diagnostic['stage'] == 'reconnect'
                        else 'account_read_retries', []))):
                return False
        if checkpoint['error_code'] in {'codex_transport_closed', 'codex_transport_eof', 'codex_read_timeout'}:
            diagnostic = checkpoint.get('initialization_diagnostic', {})
            if (diagnostic.get('stage') != 'reconnect'
                    or diagnostic.get('category') != {
                        'codex_transport_closed': 'initialization_transport_closed',
                        'codex_transport_eof': 'initialization_transport_eof',
                        'codex_read_timeout': 'initialization_read_timeout'}[checkpoint['error_code']]):
                return False
        for error in rpc_errors:
            if (not isinstance(error, dict)
                    or set(error) - {'transient_reason', 'failure_category'} != {
                        'method', 'code', 'category', 'observed_at'}
                    or not isinstance(error['method'], str)
                    or error['method'] not in {'initialize', 'account/read'}
                    or not isinstance(error['category'], str)
                    or error['category'] not in RPC_ERROR_CATEGORIES
                    or (error['code'] is not None and type(error['code']) is not int)
                    or not isinstance(error['observed_at'], str)):
                return False
            if 'failure_category' in error and (not isinstance(error['failure_category'], str)
                    or error['failure_category'] not in (ACCOUNT_FAILURE_CATEGORIES if error['method'] == 'account/read'
                        else {'initialize_timeout', 'initialize_internal_error_unclassified',
                            'authentication_required', 'request_rejected'})
                    or error['code'] != -32603
                    or error['category'] != 'internal_error'):
                return False
            if 'transient_reason' in error and (not isinstance(error['transient_reason'], str)
                    or error['transient_reason'] not in (ACCOUNT_RETRY_REASONS if error['method'] == 'account/read'
                        else {'initialize_timeout', 'initialize_internal_error_unclassified'})
                    or error['code'] != -32603
                    or error['category'] != 'internal_error'
                    or ('failure_category' in error
                        and error['failure_category'] != error['transient_reason'])):
                return False
            # Cancellation during the audited pre-auth retry backoff remains
            # resumable. Older unclassified failures cannot establish that path.
            if checkpoint['error_code'] != 'codex_rpc_error' and error.get(
                    'transient_reason') not in INITIALIZATION_CONNECTION_REASONS:
                return False
        return not any(checkpoint.get(name) for name in ('thread_id', 'turn_id', 'usage',
            'public_messages', 'event_counts', 'activities', 'server_errors', 'native_tool_violation', 'non_retryable_server_error',
            'recovered_read_only', 'recovery_history', 'initialization_recovery',
            'last_event_at', 'usage_observed_at', 'interrupt_requested', 'public_output_truncated'))

    def _can_retry_account(self, initial_dispatch_allowed):
        state = self._state
        return (initial_dispatch_allowed and state.get('status') == 'prepared'
            and not self._turn_dispatched_here
            and not any(name in state for name in ('auth_mode', 'turn_dispatched_at',
                'project_id', 'model', 'model_provider', 'reported_model', 'reported_effort',
                'model_reporting_scope', 'usage_scope', 'usage_observed_at'))
            and not any(state.get(name) for name in ('thread_id', 'turn_id', 'usage',
                'public_messages', 'event_counts', 'activities', 'server_errors', 'native_tool_violation', 'non_retryable_server_error',
                'recovered_read_only', 'recovery_history', 'last_event_at', 'interrupt_requested',
                'public_output_truncated'))
            and _valid_account_retries(state.get('account_read_retries', [])))


    def _can_retry_initialization_connection(self, initial_dispatch_allowed):
        return (self._state.get('reasoning_only') is True
            and self._can_retry_account(initial_dispatch_allowed)
            and _valid_initialization_connection_retries(self._state.get('initialization_connection_retries', [])))

    async def _initialize_reasoning_connection(self, workspace, *, initial_dispatch_allowed):
        """Reconnect only a proven pre-auth request, retaining both retry ledgers.

        No model/thread RPC is inside this loop. After auth or any dispatch marker,
        uncertain results retain the existing readback path. Each replacement
        process consumes a durable allowance before closing/waiting/opening.
        """
        while True:
            await _invoke(self._check)
            try:
                await self._open(workspace)
                return await self._reasoning_account(initial_dispatch_allowed=initial_dispatch_allowed)
            except LocalCodexError as error:
                reason = (error.transient_reason if isinstance(error, LocalCodexRPCError) else {
                    'codex_transport_closed': 'initialization_transport_closed',
                    'codex_transport_eof': 'initialization_transport_eof',
                    'codex_read_timeout': 'initialization_read_timeout'}.get(error.code))
                retries = self._state.get('initialization_connection_retries', [])
                eligible = (not isinstance(error, LocalCodexBlocked)
                    and self._can_retry_initialization_connection(initial_dispatch_allowed)
                    and reason in INITIALIZATION_CONNECTION_REASONS)
                if not eligible:
                    raise
                if len(retries) >= INITIALIZATION_CONNECTION_RETRY_LIMIT:
                    await self._save(initialization_diagnostic=self._initialization_diagnostic(
                        'reconnect', reason, 'retry_exhausted'))
                    raise
                await _invoke(self._check)
                await self._save(initialization_connection_retries=[*retries, {
                    'reason': reason, 'attempted_at': _now()}],
                    initialization_diagnostic=self._initialization_diagnostic(
                        'reconnect', reason, 'retry_initialization_connection', retry_count=len(retries) + 1))
                await _invoke(self._check)
                await self._close()
                await _initialization_connection_retry_wait(2 ** (len(retries) + 1))
                await _invoke(self._check)

    async def _reasoning_account(self, *, initial_dispatch_allowed):
        """Repeat bounded pre-auth account reads, never a model dispatch.

        Extra reads are audited before sending and capped across checkpoint
        recovery. This loop uses the same process, operation and reservation.
        The caller's overall timeout and active-run guard remain authoritative.
        """
        await self._save(initialization_diagnostic=self._initialization_diagnostic(
            'account_read', 'checking', 'continue' if initial_dispatch_allowed else 'read_original_turn'))
        while True:
            await _invoke(self._check)
            try:
                return await self._rpc('account/read', {'refreshToken': False})
            except LocalCodexRPCError as error:
                retries = self._state.get('account_read_retries', [])
                eligible = self._can_retry_account(initial_dispatch_allowed)
                if (error.transient_reason not in ACCOUNT_RETRY_REASONS
                        or not eligible or len(retries) >= ACCOUNT_RETRY_LIMIT):
                    if error.transient_reason in ACCOUNT_RETRY_REASONS:
                        await self._save(initialization_diagnostic=self._initialization_diagnostic(
                            'account_read', error.transient_reason,
                            'retry_exhausted' if eligible else 'read_original_turn'))
                    raise
                await _invoke(self._check)
                await self._save(account_read_retries=[*retries, {
                    'reason': error.transient_reason, 'attempted_at': _now()}],
                    initialization_diagnostic=self._initialization_diagnostic(
                        'account_read', error.transient_reason, 'retry_account_read', retry_count=len(retries) + 1))
                await _account_retry_wait(2 ** len(retries))
                await _invoke(self._check)

    async def _interrupt_owned(self):
        if (self._turn_dispatched_here and self._state and self._state.get('thread_id')
                and self._state.get('turn_id') and self._process):
            # Best effort; writing a notification/request is not proof of interruption.
            self._counter += 1
            try:
                await self._write({'id': self._counter, 'method': 'turn/interrupt', 'params': {
                    'threadId': self._state['thread_id'], 'turnId': self._state['turn_id']}})
            except LocalCodexError:
                pass

    async def interrupt(self, thread_id, turn_id):
        """Request interruption only for this instance's currently owned turn.

        A new adapter cannot control an unrelated Codex app process. The run loop
        consumes the flag, sends turn/interrupt and still requires terminal proof.
        """
        if not self._busy or not self._turn_dispatched_here or not self._state or (thread_id, turn_id) != (
                self._state.get('thread_id'), self._state.get('turn_id')):
            raise LocalCodexBlocked('codex_turn_not_owned_by_this_client')
        self._interrupt_requested = True
        return {'requested': True, 'confirmed': False}

    async def _read_after_code_timeout(self):
        """Confirm the owned code turn after its deadline, without another turn.

        An interrupt request is not a terminal receipt. Preserve the timeout
        failure first, then hydrate the original turn once under a separate,
        bounded read deadline. Reasoning recovery keeps its existing policy.
        """
        if (not self._turn_dispatched_here or self._state.get('reasoning_only')
                or not self._state.get('thread_id') or not self._state.get('turn_id')):
            return
        try:
            async with asyncio.timeout(min(30, self.request_timeout * 3)):
                await _invoke(self._check)
                await self._recover()
        except (LocalCodexError, TimeoutError) as error:
            await self._save(status='blocked' if isinstance(error, LocalCodexBlocked) else 'unknown',
                error_code=getattr(error, 'code', 'codex_timeout_readback_timeout'),
                recovered_read_only=False)

    async def run(self, run_id, workspace, instruction, *, operation_id, checkpoint=None,
                  on_progress: Callable, check_active=None, thread_id=None, project_root=None,
                  model=None, effort=None, reasoning_only=False, output_schema=None, keep_open=False, image_inputs=None):
        if self._busy or self._process is not None or self._close_task is not None:
            raise LocalCodexBlocked('codex_client_already_running')
        workspace = _directory(workspace)
        explicit_project_root = project_root is not None
        project_root = _directory(project_root) if explicit_project_root else workspace
        if not workspace.is_relative_to(project_root):
            raise LocalCodexBlocked('codex_workspace_outside_project')
        if explicit_project_root and workspace == project_root:
            raise LocalCodexBlocked('codex_workspace_must_be_isolated')
        if not all(isinstance(value, str) and value.strip() for value in (run_id, operation_id, instruction)):
            raise ValueError('codex_operation_identity_required')
        if not callable(on_progress):
            raise ValueError('codex_durable_progress_callback_required')
        if any(value is not None and (not isinstance(value, str) or not value.strip()) for value in (model, effort)):
            raise ValueError('codex_invalid_model_selection')
        identity = {'run_id': run_id, 'operation_id': operation_id, 'workspace': str(workspace),
                    'project_root': str(project_root), 'instruction_sha256': _hash(instruction),
                    'requested_model': model, 'requested_effort': effort}
        native_images = []
        if image_inputs:
            from .deletion import _plain_path
            if not reasoning_only or not isinstance(image_inputs, list) or len(image_inputs) > 2:
                raise LocalCodexBlocked('codex_invalid_images')
            for image in image_inputs:
                path = Path(image['path'])
                _plain_path(path, workspace)
                if (not path.resolve().is_relative_to(workspace) or path.stat().st_size > 8 * 1024**2
                        or hashlib.sha256(path.read_bytes()).hexdigest() != image['sha256']):
                    raise LocalCodexBlocked('codex_image_identity_mismatch')
                native_images.append({'type': 'localImage', 'path': str(path), 'detail': 'high'})
            identity['image_inputs_sha256'] = _hash(json.dumps(image_inputs, sort_keys=True))
        if checkpoint and bool(checkpoint.get('image_inputs_sha256')) != bool(image_inputs):
            raise LocalCodexBlocked('codex_image_identity_mismatch')
        if reasoning_only:
            identity.update(reasoning_only=True, output_schema_sha256=_hash(
                json.dumps(output_schema, sort_keys=True, ensure_ascii=False)))
        if checkpoint is not None:
            if not isinstance(checkpoint, dict):
                raise LocalCodexBlocked('codex_invalid_checkpoint')
            if bool(checkpoint.get('reasoning_only')) != reasoning_only:
                raise LocalCodexBlocked('codex_checkpoint_execution_mode_mismatch')
            if not isinstance(checkpoint, dict) or any(checkpoint.get(k) != v for k, v in identity.items()):
                raise LocalCodexBlocked('codex_checkpoint_identity_mismatch')
            if thread_id is not None and checkpoint.get('thread_id') != thread_id:
                raise LocalCodexBlocked('codex_checkpoint_thread_mismatch')
            if checkpoint.get('version') != 1 or checkpoint.get('status') not in TERMINAL | {
                    'prepared', 'thread_start_dispatched', 'thread_resume_dispatched', 'thread_ready',
                    'turn_start_dispatched', 'running'}:
                raise LocalCodexBlocked('codex_invalid_checkpoint')
        self._state = {key: copy.deepcopy(value) for key, value in checkpoint.items()
                       if key in RECEIPT_FIELDS} if checkpoint is not None else {
            **identity, 'version': 1, 'status': 'prepared', 'thread_id': thread_id, 'turn_id': None,
            'public_messages': [], 'usage': None, 'price_verified': False, 'cost_usd': None,
            'cost_status': 'not_reported_by_local_codex', 'created_at': _now(), 'error_code': None}
        if ('initialization_diagnostic' in self._state
                and not _valid_initialization_diagnostic(self._state['initialization_diagnostic'])):
            self._state.pop('initialization_diagnostic')
        self._progress, self._check = on_progress, check_active
        if checkpoint is not None:
            self._state['public_messages'] = []
            for message in checkpoint.get('public_messages', []):
                self._item({'type': 'agentMessage', **message})
        self._busy, self._interrupt_requested = True, False
        returned = None
        self._turn_dispatched_here = False
        resume_initialization = self._unsubmitted_initialization(checkpoint, reasoning_only)
        try:
            await _invoke(check_active)
            if checkpoint is not None and checkpoint.get('status') == 'completed':
                returned = copy.deepcopy(self._state)
                return returned
            if resume_initialization:
                audit = {'source_checkpoint': copy.deepcopy(checkpoint),
                    'source_sha256': hashlib.sha256(json.dumps(checkpoint, sort_keys=True,
                        ensure_ascii=False, allow_nan=False).encode()).hexdigest(),
                    'resumed_at': _now(), 'operation_id': operation_id,
                    'scope': 'Finish first reasoning initialization before durable auth marker; not another model turn'}
                await self._save(status='prepared', error_code=None, initialization_recovery=audit)
            async with asyncio.timeout(self.timeout):
                if reasoning_only:
                    account = (await self._initialize_reasoning_connection(workspace, initial_dispatch_allowed=(
                        checkpoint is None or resume_initialization))).get('account') or {}
                    if account.get('type') != 'chatgpt':
                        await self._save(initialization_diagnostic=self._initialization_diagnostic(
                            'account_read', 'authentication_required', 'check_login'))
                        raise LocalCodexBlocked('codex_chatgpt_login_required')
                    await self._save(auth_mode='chatgpt', initialization_diagnostic=self._initialization_diagnostic(
                        'authenticated', 'ready', 'continue' if checkpoint is None or resume_initialization
                            else 'read_original_turn'))
                else:
                    await self._open(workspace)
                if checkpoint is not None and not resume_initialization:
                    await self._recover()
                    returned = copy.deepcopy(self._state)
                    return returned
                catalog = await self._models() if model is not None or effort is not None else None
                selected_model = self._model_choice(catalog, model, effort) if model is not None else None
                if thread_id:
                    existing = await self._read(thread_id)
                    if any(turn.get('status') == 'inProgress' for turn in existing.get('turns', [])):
                        raise LocalCodexBlocked('codex_thread_already_running')
                await _invoke(check_active)
                await self._save(status='thread_start_dispatched' if not thread_id else 'thread_resume_dispatched')
                params = {'cwd': str(project_root), 'sandbox': 'read-only', 'approvalPolicy': 'never'}
                if reasoning_only:
                    params.update(modelProvider='openai', baseInstructions=(
                        'You are a research reasoning worker inside AutoResearch. Return only the requested JSON. '
                        'Use only the supplied task, evidence and schema. If you need a tool, return an '
                        'AutoResearch tool_calls JSON request; the host executes it and supplies results. '
                        'Do not use native Codex tools, read files, run commands, browse, or change state. '
                        'Never invent evidence or completed tool actions.'), config=copy.deepcopy(REASONING_CONFIG))
                    # Disable configured external tools in this thread only. Do not change
                    # the user's native configuration or expose its values in receipts.
                    configured = (await self._rpc('config/read', {'includeLayers': False})).get('config') or {}
                    for name in (configured.get('mcp_servers') or {}):
                        params['config'][f'mcp_servers.{name}.enabled'] = False
                    for name in (configured.get('plugins') or {}):
                        params['config'][f'plugins.{name}.enabled'] = False
                if selected_model:
                    params['model'] = selected_model
                if thread_id:
                    params['threadId'] = thread_id
                response = await self._rpc('thread/resume' if thread_id else 'thread/start', params)
                thread = response.get('thread')
                self._verify_thread(thread, thread_id=thread_id)
                if reasoning_only and response.get('modelProvider') != 'openai':
                    raise LocalCodexBlocked('codex_chatgpt_provider_required')
                if response.get('approvalPolicy') != 'never' or response.get('sandbox', {}).get('type') != 'readOnly':
                    raise LocalCodexBlocked('codex_initial_sandbox_not_read_only')
                await self._save(status='thread_ready', thread_id=thread['id'],
                    project_id=thread.get('projectId'), model=response.get('model'),
                    model_provider=response.get('modelProvider'), reported_model=response.get('model'),
                    reported_effort=response.get('reasoningEffort'),
                    model_reporting_scope='thread_configuration_not_per_turn_telemetry')
                if effort is not None:
                    self._model_choice(catalog, selected_model or response.get('model'), effort)
                await _invoke(check_active)
                if _directory(workspace) != workspace or _directory(project_root) != project_root:
                    raise LocalCodexBlocked('codex_workspace_changed')
                await self._save(status='turn_start_dispatched', turn_dispatched_at=_now())
                turn_params = {'threadId': thread['id'],
                    'clientUserMessageId': operation_id,
                    'input': [*native_images, {'type': 'text', 'text': instruction}], 'cwd': str(workspace),
                    'approvalPolicy': 'never', 'sandboxPolicy': {'type': 'workspaceWrite',
                        'writableRoots': [str(workspace)], 'networkAccess': False,
                        'excludeTmpdirEnvVar': True, 'excludeSlashTmp': True}}
                if selected_model:
                    turn_params['model'] = selected_model
                if reasoning_only:
                    turn_params['sandboxPolicy'] = {'type': 'readOnly', 'networkAccess': False}
                    if output_schema is not None:
                        from .prose_schema import relax,POLICY
                        effective_schema=relax(output_schema)
                        turn_params['outputSchema'] = effective_schema
                        if effective_schema!=output_schema:
                            self._state.update(output_schema_policy=POLICY,
                                effective_output_schema_sha256=_hash(json.dumps(effective_schema,sort_keys=True,ensure_ascii=False)))
                if effort is not None:
                    turn_params['effort'] = effort
                self._turn_dispatched_here = True
                response = await self._rpc('turn/start', turn_params)
                # The start response predates a turn-level effort override.
                if effort is not None:
                    self._state['reported_effort'] = None
                self._turn(response.get('turn'))
                await self._save()
                while self._state['status'] not in TERMINAL:
                    if self._interrupt_requested:
                        self._interrupt_requested = False
                        await self._interrupt_owned()
                        await self._save(interrupt_requested=True)
                    message = await self._receive(asyncio.get_running_loop().time() + self.timeout)
                    if 'id' not in message:
                        await self._event(message)
                returned = copy.deepcopy(self._state)
                return returned
        except asyncio.CancelledError:
            await self._interrupt_owned()
            await self._save(status='unknown', error_code='codex_cancelled_result_unknown')
            raise
        except (LocalCodexError, TimeoutError) as error:
            await self._interrupt_owned()
            await self._save(status='blocked' if isinstance(error, LocalCodexBlocked) else 'unknown',
                             error_code=getattr(error, 'code', 'codex_operation_timeout'))
            if isinstance(error, TimeoutError):
                await self._read_after_code_timeout()
            returned = copy.deepcopy(self._state)
            return returned
        except BaseException:
            # A failed durable callback or a pause/deletion guard must never be
            # swallowed or followed by another dispatch. Leave its prior marker.
            await self._interrupt_owned()
            raise
        finally:
            # The host can persist the final result and delete the one-shot chat
            # on its owning connection before releasing the native writer.
            primary_error = sys.exception()
            try:
                if not (keep_open and self._state.get('status') == 'completed'):
                    await self._close()
            except (LocalCodexError, OSError) as cleanup_error:
                code = ('codex_process_shutdown_timeout' if isinstance(cleanup_error, LocalCodexError)
                    and cleanup_error.code == 'codex_process_shutdown_timeout' else 'codex_process_shutdown_failed')
                if primary_error is not None:
                    primary_error.add_note(code)
                elif returned is not None and returned.get('error_code'):
                    # Preserve the failed RPC and its original receipt. The
                    # caller gets this same detached result after finally, so
                    # its subsequent persist cannot erase the cleanup detail.
                    await self._save(cleanup_error_code=code)
                    returned['cleanup_error_code'] = code
                else:
                    raise
            finally:
                self._busy = False
