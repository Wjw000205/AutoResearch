"""Independent Windows wall clock for a single approved diagnostic attempt."""
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback


class Incomplete(RuntimeError):
    pass


class PublicationUncertain(RuntimeError):
    """The guarded terminal transaction may have committed; reconcile its exact receipt."""


def _command(entry):
    return [sys._base_executable, '-I', '-S', '-c',
        f'import sys,json;sys.path[:]=json.loads(sys.argv[1]);from autoresearch.diagnostic_supervisor import {entry};{entry}()',
        json.dumps([str(Path(p).absolute()) for p in sys.path])]


def _cpu_remaining(read, limit, margin):
    try:
        consumed = read()
        if not math.isfinite(consumed) or consumed < 0:
            raise ValueError('invalid CPU counter')
    except Exception as exc:
        raise Incomplete('Shared CPU accounting unavailable; confirmation refused') from exc
    remaining = limit - consumed - margin
    if remaining <= 0:
        raise Incomplete('Shared CPU budget exhausted; confirmation refused')
    return consumed, remaining


async def _seal(payload, deadline, job, command):
    process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        # Keep the original kernel job and all its historical accounting.
        # One affinity core also bounds the CPU cost of the final commit by
        # its remaining wall window, even when a SQLite call cannot be polled.
        job.attach(process.pid, 1)
        out, _ = await process.communicate(json.dumps(payload, allow_nan=False).encode())
        if process.returncode or out.strip() != b'committed':
            raise Incomplete('Final receipt transaction failed: ' + out.decode(errors='replace')[-2000:])
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), max(.001, deadline-time.monotonic()))
            except TimeoutError:
                pass


async def supervise(store_path, data_dir, rid, code, params, trial_id, limits, cancel=None, *, worker_command=None, seal_command=None):
    from .lab import _WindowsJob
    started = time.monotonic()
    total = limits['timeout_seconds']
    reserve = limits['diagnostic_save_reserve_seconds']
    if os.name != 'nt' or limits['execution_mode'] != 'local' or not 0 < reserve < total:
        raise ValueError('Invalid supervised Windows diagnostic limits')
    if cancel is not None and cancel.is_set():
        raise asyncio.CancelledError()
    deadline = started + total
    active_deadline = deadline
    # Allow for termination/reaping within the total budget, including when
    # the helper is blocked inside a synchronous filesystem or SQLite call.
    cutoff = deadline - min(1., reserve / 4)
    command = worker_command or _command('worker')
    payload = json.dumps([str(store_path), str(data_dir), rid, code, params, trial_id, limits, started],
                         allow_nan=False).encode()
    process = None
    job = None
    work = stop = None
    primary_error = None
    publishing = False
    last_cpu = None
    phase = 'starting'
    cpu_limit = limits['cpu_seconds']
    margin = min(cpu_limit / 4, max(.25, .25 * limits['cpu_cores']))
    watchdog = None
    try:
        async with asyncio.timeout_at(asyncio.get_running_loop().time() + max(0., cutoff-time.monotonic())):
            process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=subprocess.DEVNULL, limit=4*1024**2,
                creationflags=subprocess.CREATE_NO_WINDOW)
            # The direct interpreter waits for the request on stdin. Its job
            # therefore owns the runner and all descendants before user code starts.
            job = _WindowsJob(process.pid, limits['memory_mb'], limits['cpu_seconds'], limits['cpu_cores'])
            if cancel is not None and cancel.is_set():
                raise asyncio.CancelledError()
            async def monitor_cpu():
                nonlocal last_cpu
                while True:
                    last_cpu, _ = _cpu_remaining(job.total_cpu_seconds, cpu_limit, margin)
                    await asyncio.sleep(.04)
            async def exchange():
                nonlocal active_deadline, primary_error, publishing, last_cpu, phase
                phase = 'computing'
                process.stdin.write(payload)
                await process.stdin.drain()
                process.stdin.close()
                # No disk scans, DB writes or process-tree enumeration in this supervisor.
                async with asyncio.timeout(max(0., deadline-reserve-time.monotonic())):
                    first = await process.stdout.readline()
                if not first.startswith(b'computation-stopped '):
                    raise Incomplete('Diagnostic worker stopped before closeout: ' + first.decode(errors='replace')[:2000])
                stopped = float(first.split()[1])
                if not started <= stopped <= time.monotonic():
                    raise Incomplete('Invalid diagnostic closeout timestamp')
                active_deadline = min(deadline, stopped + reserve)
                phase = 'archiving'
                async with asyncio.timeout(max(0., active_deadline-min(1., reserve/4)-time.monotonic())):
                    reply = await process.stdout.readline()
                    if reply.startswith(b'primary-error '):
                        primary_error = json.loads(reply[len(b'primary-error '):])
                        reply = await process.stdout.readline()
                    await process.wait()
                if not reply:
                    raise Incomplete('Diagnostic worker exited without a committed receipt')
                message = json.loads(reply)
                if process.returncode or 'error' in message:
                    raise Incomplete(f"Original error: {message.get('original_error')}; "
                                     f"closeout: {message.get('error', 'Diagnostic worker failed')}")
                result = message['result']
                # The heavy archive (including its SQLite writes) and worker
                # exit have now been observed. Only a killable, atomic seal can
                # promote the provisional receipt, within BOTH remaining budgets.
                result['duration_seconds'] = time.monotonic() - started
                result['duration_scope'] = 'Through durable archive and observed runner exit, before confirmation transaction'
                timing = {'worker_exited': True, 'receipt_sha256': _sha(result),
                          'duration_seconds': result['duration_seconds'], 'wall_limit_seconds':total,
                          'closeout_seconds':time.monotonic()-stopped, 'save_reserve_seconds':reserve}
                used, remaining = _cpu_remaining(job.total_cpu_seconds, cpu_limit, margin)
                last_cpu = used
                timing.update(cpu_accounting='shared_windows_job_user_plus_kernel', cpu_limit_seconds=cpu_limit,
                              cpu_through_archive_seconds=used, cpu_margin_seconds=margin, seal_cpu_available_seconds=remaining)
                seal_deadline = min(active_deadline-min(1., reserve/4), time.monotonic() + remaining)
                async with asyncio.timeout(max(0., seal_deadline-time.monotonic())):
                    phase = 'staging_confirmation'
                    await _seal([str(store_path), rid, trial_id, result, timing, seal_deadline, 'stage'],
                                active_deadline, job, seal_command or _command('seal'))
                # Staging creates no terminal experiment or confirmation marker.
                # Failures/cancellation here leave the canonical row incomplete.
                phase = 'final_checks'
                last_cpu, remaining = _cpu_remaining(job.total_cpu_seconds, cpu_limit, margin)
                if cancel is not None and cancel.is_set():
                    raise asyncio.CancelledError()
                if time.monotonic() >= active_deadline:
                    raise Incomplete('Final wall check failed before publication')
                timing.update(cpu_through_final_checks_seconds=last_cpu, final_checks_passed=True,
                              terminal_commit_protocol=2,
                              duration_seconds=time.monotonic()-started, closeout_seconds=time.monotonic()-stopped)
                publication_deadline = min(active_deadline-min(1., reserve/4), time.monotonic()+remaining)
                if time.monotonic() >= publication_deadline:
                    raise Incomplete('No bounded publication window remains')
                # This is the completion decision's linearization boundary.
                # Cancellation arriving after admission waits for this atomic
                # guarded transaction, and cannot contradict a committed result.
                publishing = True
                phase = 'publishing'
                async with asyncio.timeout(max(0., publication_deadline-time.monotonic())):
                    await _seal([str(store_path), rid, trial_id, result, timing, publication_deadline, 'publish'],
                                active_deadline, job, _command('seal'))
                return result, timing
            work = asyncio.create_task(exchange())
            watchdog = asyncio.create_task(monitor_cpu())
            if cancel is not None:
                stop = asyncio.create_task(cancel.wait())
            await asyncio.wait([work, watchdog] + ([stop] if stop else []), return_when=asyncio.FIRST_COMPLETED)
            if not publishing and watchdog.done():
                watchdog.result()
            if not publishing and cancel is not None and cancel.is_set():
                raise asyncio.CancelledError()
            result, timing = await asyncio.shield(work)
            elapsed = time.monotonic() - started
            return result, {**timing, 'return_elapsed_seconds':elapsed, 'publication_acknowledged':True,
                'scope':'Final supervision precedes the guarded atomic publication. Return time includes publication and helper exit.'}
    except TimeoutError as exc:
        if publishing:
            raise PublicationUncertain('Terminal transaction acknowledgement lost; reconcile the exact saved transaction') from exc
        error = Incomplete(f'Diagnostic deadline reached during {phase}; last checkpoint retained. '
                           f'Original error if reported: {primary_error}')
        error.diagnostic_evidence = {'phase':phase,'last_valid_cpu_seconds':last_cpu,
            'cpu_measurement_available':last_cpu is not None,'cpu_limit_seconds':cpu_limit,'reason':'deadline'}
        raise error from exc
    except (Incomplete, asyncio.CancelledError) as error:
        if publishing:
            raise PublicationUncertain('Publication was admitted after all final checks; inspect the guarded transaction outcome') from error
        error.diagnostic_evidence = {'phase':phase,'last_valid_cpu_seconds':last_cpu,'cpu_limit_seconds':cpu_limit,
            'cpu_measurement_available':last_cpu is not None,'counter_failed':'accounting unavailable' in str(error),
            'reason':'cancelled' if isinstance(error,asyncio.CancelledError) else str(error)}
        raise
    finally:
        if work and not work.done():
            work.cancel()
        if stop:
            stop.cancel()
        if watchdog:
            watchdog.cancel()
            if watchdog.done() and not watchdog.cancelled():
                watchdog.exception()
        if job:
            job.close()  # KILL_ON_JOB_CLOSE, including a blocked archive worker.
        if process and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), max(.001, active_deadline-time.monotonic()))
            except TimeoutError:
                pass  # The OS has the termination request; no unbounded join.
        if work and work.done() and not work.cancelled():
            work.exception()
        elif work and not work.done():
            try:
                await asyncio.wait_for(work, max(.001, active_deadline-time.monotonic()))
            except (asyncio.CancelledError, TimeoutError):
                pass


def _sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def committed_publication(store, rid, trial):
    """Read the linearized transaction after a lost acknowledgement, never execute."""
    key = rid + ':' + trial
    with store.read_snapshot() as reader:
        result = reader.get('experiments',key)
        proof = reader.get('diagnostic_wall_confirmations',key) or {}
    if (result is None or proof.get('status')!='confirmed' or proof.get('terminal_commit_protocol')!=2
            or proof.get('final_checks_passed') is not True or proof.get('receipt_sha256')!=_sha(result)):
        raise PublicationUncertain('No matching committed terminal transaction; retain incomplete and do not retry training')
    return result, {**proof,'acknowledgement_recovered':True}


def worker():
    # No SQLite or workspace access before the parent releases the stdin gate.
    args = json.load(sys.stdin.buffer)
    from .lab import ExperimentRunner
    from .storage import Store
    path, root, rid, code, params, trial, limits, started = args
    class WorkerStore(Store):
        primary_error = None

        def put(self, collection, key, value):
            if collection in ('experiments','experiment_attempts'):
                # Until result.json, event archival and both terminal records
                # succeed, even abrupt process death leaves only incomplete evidence.
                if not self.primary_error and (value.get('error') or value.get('stderr')):
                    self.primary_error = [value.get('error'), (value.get('stderr') or '')[-2000:]]
                    print('primary-error', json.dumps(self.primary_error), flush=True)
                value = {**value, 'status':'incomplete', 'diagnostic_outcome':'incomplete',
                         'retry_authorized':False, 'metrics':{}}
            return super().put(collection,key,value)

        def event(self, run_id, kind, message, data=None):
            if kind == 'experiment.finished':
                kind, message = 'experiment.archive_staged', 'Diagnostic files staged; awaiting terminal commit'
            return super().event(run_id,kind,message,data)

    store = None
    try:
        store = WorkerStore(path)
        key = rid + ':' + trial
        def claim(old):
            if old is not None:
                raise Incomplete('This supervised trial already has a dispatch claim')
            return {'run_id':rid, 'trial_id':trial, 'provisional_outcome':'incomplete', 'retry_authorized':False}
        if store.get('experiments', key) is not None:
            raise Incomplete('A supervised trial cannot overwrite an existing receipt')
        store.mutate('diagnostic_supervisor_claims', key, claim)
        runner = ExperimentRunner(store, root)
        result = asyncio.run(runner.run(rid, code, params, trial, limits, supervised_start=started,
            computation_stopped=lambda stopped: print('computation-stopped', stopped, flush=True)))
        # Includes both provisional store.put calls and the on-disk receipt.
        # The parent independently measures through this final transaction and exit.
        result['duration_seconds'] = time.monotonic() - started
        store.put('diagnostic_staged_results', key, {'run_id':rid,'result':result,'sha256':_sha(result)})
        print(json.dumps({'result':result}, allow_nan=False), flush=True)
    except BaseException:
        # Chained exceptions preserve both the computation and saving errors.
        primary = getattr(store, 'primary_error', None)
        print(json.dumps({'error':traceback.format_exc()[-6000:], 'original_error':primary}), flush=True)
        raise SystemExit(1)


def seal():
    try:
        path, rid, trial, result, timing, deadline, phase = json.load(sys.stdin.buffer)
        import ctypes
        from .lab import _WindowsJob
        from .storage import Store
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        def check_commit():
            # NULL queries the sealing process's immediate (shared) Windows job.
            used, _ = _cpu_remaining(lambda: _WindowsJob.query_cpu_seconds(kernel, None),
                                     timing['cpu_limit_seconds'], timing['cpu_margin_seconds'])
            if used < timing['cpu_through_archive_seconds'] or time.monotonic() >= deadline:
                raise Incomplete('CPU ledger regressed or seal deadline expired')
            return used
        timing['cpu_at_seal_entry_seconds'] = check_commit()
        store = Store(path)
        key = rid + ':' + trial
        archive = store.get('diagnostic_staged_results', key)
        frozen = archive['result']
        if (archive['sha256'] != _sha(frozen) or {**frozen,'duration_seconds':result['duration_seconds'],
                'duration_scope':result['duration_scope']} != result or timing['receipt_sha256'] != _sha(result)):
            raise Incomplete('Terminal receipt differs from its staged archive')
        previous = store.get('experiments', key)
        if previous.get('status') != 'incomplete' or time.monotonic() >= deadline:
            raise Incomplete('Cannot seal late or already finalized diagnostic')
        if phase == 'stage':
            puts = [('diagnostic_prepared_confirmations',key,{'run_id':rid, **timing, 'status':'prepared'})]
        elif phase == 'publish' and timing.get('final_checks_passed') is True:
            staged = store.get('diagnostic_prepared_confirmations',key) or {}
            if staged.get('receipt_sha256') != timing['receipt_sha256']:
                raise Incomplete('Final publication lost its prepared receipt')
            puts = [('experiments',key,result), ('experiment_attempts',key+':'+str(result['attempt']),result),
                    ('diagnostic_wall_confirmations',key,{'run_id':rid, **timing, 'status':'confirmed'})]
        else:
            raise Incomplete('Unsupported confirmation phase')
        store.batch(puts=puts,
                    expected=[('experiments',key,previous),('diagnostic_wall_confirmations',key,None)],
                    before_commit=check_commit)
        print('committed', flush=True)
    except Exception:
        print(traceback.format_exc()[-2000:], flush=True)
        raise SystemExit(1)
