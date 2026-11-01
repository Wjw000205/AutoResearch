"""Windows-only fail-closed diagnostic startup, before numerical imports."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path

_HANDLES=[]  # Keep the nested affinity job alive until this process exits.


class Basic(ctypes.Structure):
    _fields_=[('PerProcessUserTimeLimit',ctypes.c_longlong),('PerJobUserTimeLimit',ctypes.c_longlong),
        ('LimitFlags',wintypes.DWORD),('MinimumWorkingSetSize',ctypes.c_size_t),('MaximumWorkingSetSize',ctypes.c_size_t),
        ('ActiveProcessLimit',wintypes.DWORD),('Affinity',ctypes.c_size_t),('PriorityClass',wintypes.DWORD),('SchedulingClass',wintypes.DWORD)]


def constrain(cores, kernel=None):
    if os.name!='nt' or type(cores) is not int or not 1<=cores<=2:
        raise RuntimeError('This reviewed diagnostic requires Windows and at most two CPU cores')
    k=kernel or ctypes.WinDLL('kernel32',use_last_error=True)
    signatures={
        'GetCurrentProcess':([],wintypes.HANDLE),
        'GetProcessAffinityMask':([wintypes.HANDLE,ctypes.POINTER(ctypes.c_size_t),ctypes.POINTER(ctypes.c_size_t)],wintypes.BOOL),
        'CreateJobObjectW':([ctypes.c_void_p,wintypes.LPCWSTR],wintypes.HANDLE),
        'SetInformationJobObject':([wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD],wintypes.BOOL),
        'QueryInformationJobObject':([wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD,ctypes.c_void_p],wintypes.BOOL),
        'AssignProcessToJobObject':([wintypes.HANDLE,wintypes.HANDLE],wintypes.BOOL),
        'CloseHandle':([wintypes.HANDLE],wintypes.BOOL)}
    for name,(args,result) in signatures.items():
        fn=getattr(k,name);fn.argtypes=args;fn.restype=result
    process=k.GetCurrentProcess();allowed=ctypes.c_size_t();system=ctypes.c_size_t()
    if not k.GetProcessAffinityMask(process,ctypes.byref(allowed),ctypes.byref(system)):
        raise RuntimeError('Cannot read CPU affinity; diagnostic gate stays closed')
    bits=[1<<n for n in range(ctypes.sizeof(ctypes.c_size_t)*8) if allowed.value & (1<<n)]
    mask=sum(bits[:cores])
    if not mask:raise RuntimeError('No eligible CPUs')
    job=k.CreateJobObjectW(None,None)
    if not job:raise RuntimeError('Cannot create diagnostic affinity job')
    try:
        info=Basic();info.LimitFlags=0x10;info.Affinity=mask  # JOB_OBJECT_LIMIT_AFFINITY; no breakaway.
        if not k.SetInformationJobObject(job,2,ctypes.byref(info),ctypes.sizeof(info)):
            raise RuntimeError('Cannot set Job CPU affinity; diagnostic gate stays closed')
        if not k.AssignProcessToJobObject(job,process):
            raise RuntimeError('Cannot join nested CPU job; diagnostic gate stays closed')
        actual=Basic()
        if (not k.QueryInformationJobObject(job,2,ctypes.byref(actual),ctypes.sizeof(actual),None)
                or actual.LimitFlags & 0x10 != 0x10 or actual.Affinity!=mask):
            raise RuntimeError('Job CPU affinity verification failed; diagnostic gate stays closed')
        if not k.GetProcessAffinityMask(process,ctypes.byref(allowed),ctypes.byref(system)) or allowed.value!=mask:
            raise RuntimeError('Effective CPU affinity verification failed; diagnostic gate stays closed')
        _HANDLES.append((k,job))
        return {'job_affinity_mask':mask,'effective_cores':len(bits[:cores]),'descendants_constrained':True}
    except BaseException:
        k.CloseHandle(job)
        raise


def launch():
    # The outer runner job already enforces memory/CPU time and lifetime. This
    # nested job enforces CPU affinity for this process and every descendant.
    limits=json.loads(Path('bootstrap.json').read_text(encoding='utf-8'))
    receipt=constrain(min(2,limits['cpu_cores']))
    Path('diagnostic_startup.json').write_text(json.dumps(receipt),encoding='utf-8')
    from diagnostic_probe import main
    main()
