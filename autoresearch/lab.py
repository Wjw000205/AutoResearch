"""Hardware admission, authoritative literature retrieval, and durable experiment tools.

Local execution is an opt-in trusted-code mode, not a filesystem/network sandbox.
Docker execution uses an existing Python image with network disabled and no secrets.
Literature verification establishes metadata identity; it never proves a paper's claims.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import html
import io
import importlib.metadata as metadata
import ipaddress
import json
import math
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from .python_environments import inspect_python, interpreter_environment, validate_python
from urllib.parse import quote, unquote, urljoin, urlparse

import httpx
import psutil

from .storage import Store, RunDeletedError, now, redact
from .process_tree import owned_process, verified_children


GIB = 1024 ** 3
METADATA_NOTE = "Identity/metadata verified only; scientific claims and reproducibility are not verified."


class SourceUnavailableError(ValueError):
    """An explicit intermediary/challenge page is not scholarly source text."""
    def __init__(self, code: str):
        self.code = code
        super().__init__('Source is not readable document text: ' + code)


def source_text_issue(document: dict) -> str | None:
    """Classify only explicit short placeholder pages, never words in a paper.

    This does not verify scientific content or require a minimum text length.
    It also works on immutable legacy source snapshots without rewriting them.
    """
    if document.get('format') == 'pdf' or document.get('pages_total'):
        return None
    text = re.sub(r'\s+', ' ', str(document.get('text') or '')).strip().casefold()
    title = re.sub(r'\s+', ' ', str(document.get('title') or '')).strip().casefold()
    if len(text) > 2000:
        return None
    trim = lambda value: value.strip(' .!…:-')
    short = trim(text)
    if re.fullmatch(r'(?:redirecting|redirecting you|redirecting to (?:the )?(?:requested )?page|please wait)(?:[ .!…:-]*(?:redirecting|please wait))*', short):
        return 'redirect_placeholder'
    denied = {'access denied', '403 forbidden', 'forbidden', 'request rejected', 'request blocked',
              'permission denied', 'authentication required', 'unauthorized'}
    challenge = {'just a moment', 'checking your browser', 'security check', 'verify you are human',
                 'robot check', 'captcha', 'javascript required'}
    restricted = {'sign in', 'log in', 'login required', 'subscription required', 'ieee xplore login',
                  'purchase access', 'subscribe to read', 'payment required'}
    if short in denied or trim(title) in denied:
        return 'access_denied'
    if short in challenge or trim(title) in challenge:
        return 'access_challenge'
    if short in restricted or trim(title) in restricted:
        return 'access_required'
    if len(text) <= 400 and re.fullmatch(
            r'(?:please )?(?:enable|turn on) javascript(?: and cookies)?(?: to (?:continue|proceed|view (?:this|the) (?:page|site)))?[ .!…]*', text):
        return 'javascript_required'
    return None


def _html_refresh_target(content: bytes, base_url: str, content_type: str) -> str | None:
    """Follow a single declarative refresh only; never interpret script code."""
    if 'html' not in content_type.lower() and b'<html' not in content[:500].lower():
        return None
    class Refresh(HTMLParser):
        def __init__(self):
            super().__init__()
            self.refreshes, self.has_base = [], False
            self.in_script, self.script_redirect = False, False

        def handle_starttag(self, tag, attrs):
            if tag == 'script':
                self.in_script = True
            if tag == 'base' and any(name == 'href' for name, _ in attrs):
                self.has_base = True
            if tag != 'meta':
                return
            equivalent = [value for name, value in attrs if name == 'http-equiv']
            if any((value or '').strip().casefold() == 'refresh' for value in equivalent):
                values = [value for name, value in attrs if name == 'content']
                self.refreshes.append(values[0] if len(values) == len(equivalent) == 1 else None)

        def handle_endtag(self, tag):
            if tag == 'script':
                self.in_script = False

        def handle_data(self, data):
            if self.in_script and re.search(r'\b(?:(?:window|document)\.)?location(?:\.(?:href|replace|assign))?\s*(?:=|\()', data):
                self.script_redirect = True
    parser = Refresh()
    decoded = content.decode('utf-8', errors='replace')
    parser.feed(decoded)
    if not parser.refreshes:
        visible = _plain(re.sub(r'<(script|style)\b[^>]*>.*?</\1>', '', decoded, flags=re.I | re.S))
        if parser.script_redirect and len(visible) <= 400:
            raise SourceUnavailableError('javascript_redirect_unsupported')
        return None
    if len(parser.refreshes) != 1 or parser.has_base:
        raise SourceUnavailableError('ambiguous_html_redirect')
    value = parser.refreshes[0]
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*;\s*url\s*=\s*(.+?)\s*', value or '', re.I | re.S)
    if not match or float(match[1]) > 10:
        raise SourceUnavailableError('unsupported_html_redirect')
    target = match[2]
    if target[0] in "\"'":
        if len(target) < 3 or target[-1] != target[0]:
            raise SourceUnavailableError('ambiguous_html_redirect')
        target = target[1:-1]
    if (not target or len(target) > 2048 or any(ord(c) < 32 for c in target)
            or any(c in target for c in "\"'<>")):
        raise SourceUnavailableError('unsupported_html_redirect')
    return urljoin(base_url, target)

def _html_pdf_target(content, base_url, content_type):
    """Use only an explicit scholarly PDF locator, never arbitrary page links."""
    if 'html' not in content_type.lower() and b'<html' not in content[:500].lower():
        return None
    targets = []
    class Metadata(HTMLParser):
        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if tag == 'meta' and str(values.get('name', '')).lower() == 'citation_pdf_url':
                targets.append(values.get('content'))
    Metadata().feed(content[:2 * 1024 * 1024].decode('utf-8', errors='replace'))
    targets = list(dict.fromkeys(t for t in targets if isinstance(t, str) and t.strip()))
    if len(targets) != 1:
        return None
    target = urljoin(base_url, targets[0].strip())
    parsed = urlparse(target)
    if parsed.scheme not in {'https', 'http'} or parsed.username or parsed.password or len(target) > 2048:
        return None
    return target if target != base_url else None


_SOFTWARE_CACHE = {"expires": 0, "value": None}
_SOFTWARE_LOCK = threading.Lock()
_PACKAGE_NAMES = ("numpy", "scipy", "scikit-learn", "pandas", "matplotlib", "torch", "torchvision", "torchaudio",
    "tensorflow", "jax", "jaxlib", "transformers", "datasets", "accelerate", "xgboost", "lightgbm", "timm", "optuna", "pypdf")
_PACKAGE_ALIASES = {"sklearn": "scikit-learn", "pytorch": "torch", "pil": "pillow", "cv2": "opencv-python"}


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _probe_environment() -> dict:
    result = {k: v for k, v in os.environ.items() if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG"}}
    result.update({"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "PYTORCH_NVML_BASED_CUDA_CHECK": "1"})
    return result


def _isolated_package_inventory() -> dict:
    code = "import importlib.metadata as m,json,re; print(json.dumps({re.sub(r'[-_.]+','-',d.metadata['Name']).lower():d.version for d in m.distributions() if d.metadata.get('Name')}))"
    probe = subprocess.run([sys.executable, "-I", "-c", code], env=_probe_environment(),
        capture_output=True, text=True, timeout=5, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return json.loads(probe.stdout.splitlines()[-1])


def _software_profile() -> dict:
    """Read current-interpreter packages; optional GPU probe is isolated and cached."""
    with _SOFTWARE_LOCK:
        if _SOFTWARE_CACHE["value"] and time.monotonic() < _SOFTWARE_CACHE["expires"]:
            return json.loads(json.dumps(_SOFTWARE_CACHE["value"]))
        host_packages = {}
        for distribution in metadata.distributions():
            name = distribution.metadata.get("Name")
            if name:
                host_packages[re.sub(r"[-_.]+", "-", name).lower()] = distribution.version
        inventory_error = None
        try:
            packages = _isolated_package_inventory()
        except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
            packages = {}
            inventory_error = type(exc).__name__
        relevant = {name: {"installed": name in packages, "version": packages.get(name),
            "verification": "distribution_metadata; import compatibility not guaranteed"} for name in _PACKAGE_NAMES}
        torch_info = {"installed": "torch" in packages, "probe_status": "not_installed", "cuda_available": False, "cuda_build": None}
        if "torch" in packages:
            # No tensors or model allocations. A driver initialization check is confined
            # to a short-lived process, avoiding persistent GPU state in the API server.
            probe_code = "import json,torch; print(json.dumps({'cuda_build':torch.version.cuda,'cuda_available':bool(torch.cuda.is_available()),'version':torch.__version__}))"
            try:
                probe = subprocess.run([sys.executable, "-I", "-c", probe_code], env=_probe_environment(),
                    capture_output=True, text=True, timeout=8, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                torch_info.update(json.loads(probe.stdout.splitlines()[-1]), probe_status="checked_in_isolated_process")
            except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
                detail = getattr(exc, "stderr", None) or getattr(exc, "stdout", None) or str(exc)
                if isinstance(detail, bytes):
                    detail = detail.decode("utf-8", errors="replace")
                torch_info.update(probe_status="unavailable", error=type(exc).__name__, cuda_available=None,
                    error_tail=redact(str(detail))[-1500:], exit_code=getattr(exc, "returncode", None), probe_timeout_seconds=8)
        nvcc = shutil.which("nvcc")
        toolkit = None
        if nvcc:
            try:
                output = subprocess.run([nvcc, "--version"], capture_output=True, text=True, timeout=3, check=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                match = re.search(r"release\s+([\d.]+)", output.stdout)
                toolkit = match.group(1) if match else None
            except (OSError, subprocess.SubprocessError):
                pass
        result = {"python_executable": sys.executable, "python_prefix": sys.prefix,
            "python_version": platform.python_version(), "is_virtualenv": sys.prefix != sys.base_prefix,
            "packages": relevant, "installed_distributions": packages,
            "inventory_verified": inventory_error is None, "inventory_error": inventory_error,
            "excluded_from_isolated_runtime": {name: version for name, version in host_packages.items() if name not in packages},
            "gpu_runtime": {"torch": torch_info, "cuda_toolkit_version": toolkit, "nvcc_available": bool(nvcc),
                "note": "GPU hardware, driver support, installed toolkit, and framework CUDA support are distinct; no model is loaded."},
            "checked_at": now(), "provenance": "current_experiment_interpreter", "cache_seconds": 60}
        _SOFTWARE_CACHE.update(value=result, expires=time.monotonic() + 60)
        return json.loads(json.dumps(result))


def hardware_profile() -> dict:
    """Inspect actual host resources; unknown GPU information is never invented."""
    memory = psutil.virtual_memory()
    disk = shutil.disk_usage(Path.cwd())
    cpu_name = platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER") or platform.machine()
    try:
        if os.name == "nt":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                cpu_name = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        elif sys.platform.startswith("linux"):
            match = re.search(r"^model name\s*:\s*(.+)$", Path("/proc/cpuinfo").read_text(), re.M)
            if match:
                cpu_name = match.group(1).strip()
    except (OSError, ValueError):
        pass
    result = {
        "cpu_name": cpu_name,
        "cpu_cores": psutil.cpu_count(logical=True) or 1,
        "cpu_physical_cores": psutil.cpu_count(logical=False) or 1,
        "ram_total_gb": round(memory.total / GIB, 2),
        "ram_available_gb": round(memory.available / GIB, 2),
        "disk_free_gb": round(disk.free / GIB, 2),
        "gpus": [], "platform": platform.platform(), "python": platform.python_version(),
        "docker_available": bool(shutil.which("docker")), "checked_at": now(),
        "provenance": "host_measurement", "gpu_detection": "nvidia-smi unavailable",
        "software": _software_profile(),
    }
    executable = shutil.which("nvidia-smi")
    if not executable and os.name == "nt":
        candidate = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvidia-smi.exe"
        if candidate.is_file():
            executable = str(candidate)
    if executable:
        try:
            output = subprocess.run(
                [executable, "--query-gpu=name,memory.total,memory.free,driver_version,uuid", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5, check=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            for index, fields in enumerate(csv.reader(output.stdout.splitlines())):
                if len(fields) >= 3:
                    result["gpus"].append({"index": index, "name": fields[0].strip(),
                        "total_gb": round(float(fields[1]) / 1024, 2),
                        "free_gb": round(float(fields[2]) / 1024, 2),
                        "driver_version": fields[3].strip() if len(fields) > 3 else None,
                        "uuid": fields[4].strip() if len(fields) > 4 else None})
            result["gpu_detection"] = "nvidia-smi"
        except (subprocess.SubprocessError, ValueError, OSError) as exc:
            result["gpu_detection"] = f"unavailable: {type(exc).__name__}"
    return result


def assess_resources(plan: dict, hardware: dict, *, require_complete: bool = False) -> dict:
    """Conservative admission estimate. GPU memory is never summed across devices.

    Explicit estimates cannot reduce the calculated model weights/optimizer/KV budget.
    This is a capacity gate; an experiment monitor must still enforce measured usage.
    """
    if not isinstance(plan, dict):
        return {"approved": False, "reasons": ["Resource plan must be an object"], "limits": {}, "estimated_vram_gb": 0}
    resources = {**plan, **(plan.get("resources") if isinstance(plan.get("resources"), dict) else {})}
    if require_complete:
        from .resource_contract import issues
        missing = issues(resources)
        if missing:
            return {'approved': False, 'reasons': missing, 'limits': {}, 'plan_complete': False,
                'user_preferences': hardware.get('execution_preferences', {}),
                'scope': '资源计划缺少有效数值，不代表用户禁止GPU或仅允许小额CPU资源。'}
    reasons, assumptions = [], []

    dependencies = resources.get("required_packages", [])
    if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
        reasons.append("required_packages must be a list of package names or PEP 508 version requirements")
        dependencies = []
    dependencies = list(dependencies)
    if resources.get("framework"):
        dependencies.append(str(resources["framework"]))
    software = hardware.get("software", {})
    preferences = hardware.get("execution_preferences", {})
    if preferences.get("python_executable") and software.get("inventory_verified") is not True:
        reasons.append("Selected Python environment is unavailable or its inventory could not be verified")
    installed = software.get("installed_distributions", {})
    if not installed:
        installed = {name: value.get("version") for name, value in software.get("packages", {}).items()
            if isinstance(value, dict) and value.get("installed") and value.get("version")}
    required_names = set()
    for dependency in dependencies:
        dependency = dependency.strip()
        if dependency.casefold() in {"stdlib", "standard library", "python standard library"} or dependency in sys.stdlib_module_names:
            continue
        dependency = _PACKAGE_ALIASES.get(dependency.casefold(), dependency)
        try:
            from packaging.requirements import Requirement
            requirement = Requirement(dependency)
            if requirement.url or requirement.extras:
                reasons.append(f"Package requirement {dependency!r}: URL installs/extras cannot be verified; declare installed distribution versions directly")
                continue
            marker_environment = None
            if software.get('python_version'):
                marker_environment = {'python_full_version': software['python_version'],
                    'python_version': '.'.join(software['python_version'].split('.')[:2])}
            if requirement.marker and not requirement.marker.evaluate(environment=marker_environment):
                continue
            name = re.sub(r"[-_.]+", "-", requirement.name).lower()
            name = _PACKAGE_ALIASES.get(name, name)
            required_names.add(name)
            if name == "python":
                version = software.get("python_version") or hardware.get("python")
                if not version or (requirement.specifier and not requirement.specifier.contains(version, prereleases=True)):
                    reasons.append(f"Required {dependency}; experiment interpreter Python version is {version or 'unverified'}")
                continue
            if not software or software.get("inventory_verified") is False:
                reasons.append(f"Cannot verify required package {name}: current interpreter software inventory is missing")
            elif name not in installed:
                reasons.append(f"Required package {name} is not installed in the experiment interpreter; choose an installed method or prepare the dependency before execution")
            elif requirement.specifier and not requirement.specifier.contains(installed[name], prereleases=True):
                reasons.append(f"Required {dependency}; experiment interpreter has {name}=={installed[name]}")
        except ImportError:
            reasons.append("Package requirement validation needs the lightweight packaging dependency")
        except ValueError:
            reasons.append(f"Invalid required package expression: {dependency!r}")

    def number(name, default=0):
        try:
            value = float(resources.get(name, default))
            if not math.isfinite(value) or value < 0:
                raise ValueError
            return value
        except (TypeError, ValueError):
            reasons.append(f"Invalid nonnegative numeric resource field: {name}")
            return default

    ram_free = max(0.0, float(hardware.get("ram_available_gb", 0)))
    disk_free = max(0.0, float(hardware.get("disk_free_gb", 0)))
    cpu_total = max(1, int(hardware.get("cpu_cores", 1)))
    ram_budget, disk_budget = ram_free * .65, disk_free * .80
    for key, value in [('ram_limit_gb', ram_budget), ('disk_limit_gb', disk_budget), ('cpu_limit_cores', cpu_total)]:
        configured = preferences.get(key)
        if configured is not None:
            if isinstance(configured, bool) or not isinstance(configured, (int, float)) or not math.isfinite(configured) or configured <= 0:
                reasons.append(f'{key} must be finite and positive')
                configured = 0
            value = min(value, configured)
        if key == 'ram_limit_gb': ram_budget = value
        elif key == 'disk_limit_gb': disk_budget = value
        else: cpu_total = max(1, int(value))
    devices = hardware.get("gpus", [])
    if preferences.get("gpu_index") is not None:
        devices = [device for device in devices if device.get("index") == preferences["gpu_index"]]
    gpu = max(devices, key=lambda item: item.get("free_gb", 0), default={})
    gpu_budget = float(gpu.get("free_gb", 0)) * .80
    requested_budget = preferences.get("vram_limit_gb")
    if requested_budget is not None:
        if isinstance(requested_budget, bool) or not isinstance(requested_budget, (int, float)) or not math.isfinite(requested_budget) or requested_budget < 0:
            reasons.append("Task VRAM budget must be finite and nonnegative")
            gpu_budget = 0
        else:
            gpu_budget = min(gpu_budget, requested_budget)
    parameter_count = number("parameter_count", number("model_parameters", 0))
    billions = number("model_parameters_billions", parameter_count / 1e9)
    bits = number("precision_bits", 32)
    if bits not in (4, 8, 16, 32, 64):
        reasons.append("precision_bits must be one of 4, 8, 16, 32, 64")
    regime = str(resources.get("training_regime", resources.get("training", "none"))).lower()
    known_regimes = {"none", "inference", "eval", "evaluation", "frozen", "false", "full", "full_finetune",
        "full-finetune", "full_fine_tuning", "pretrain", "training", "train", "true", "lora", "qlora", "adapter", "finetune"}
    if billions and regime not in known_regimes:
        reasons.append(f"Unknown training_regime {regime!r}; specify inference, full, lora, or qlora")
    model_name = str(resources.get("model_name", resources.get("model", ""))).lower()
    is_llm = bool(resources.get("is_llm")) or any(x in model_name for x in ("llama", "qwen", "llm", "mistral", "bloom", "deepseek"))
    gpu_required = bool(resources.get("gpu_required", resources.get("requires_gpu", False)))
    if not isinstance(resources.get("gpu_required", resources.get("requires_gpu", False)), bool):
        reasons.append("gpu_required must be a JSON boolean")
    if gpu_required and "torch" in required_names:
        torch_runtime = software.get("gpu_runtime", {}).get("torch", {})
        if torch_runtime.get("cuda_available") is not True:
            reasons.append("PyTorch CUDA execution is not available or could not be verified in this interpreter; use CPU or prepare a CUDA-capable runtime")
    declared_vram = number("estimated_vram_gb", number("vram_gb", 0))
    estimated_vram = declared_vram
    estimated_cpu_memory = 0.0
    if is_llm and not billions:
        reasons.append("An LLM plan must declare model_parameters_billions or parameter_count")
    if billions:
        weights = billions * 1e9 * bits / 8 / GIB
        context_length = number("context_length", 2048)
        batch_size = number("batch_size", 1)
        large_model = is_llm or billions >= .1
        kv = max(.5, weights * context_length / 4096 * batch_size * .15) if large_model else 0
        runtime = 1 if large_model else .15
        if regime in {"full", "full_finetune", "full-finetune", "full_fine_tuning", "finetune", "pretrain", "training", "train", "true"}:
            estimate = billions * 1e9 * 20 / GIB + kv + runtime  # FP32 masters, gradients, Adam + activations.
            assumptions.append(f"Full training: 20 bytes/parameter plus activation allowance and {runtime} GiB runtime")
        elif regime in {"lora", "qlora", "adapter"}:
            estimate = weights * 1.4 + kv * 2 + 2 * runtime
            assumptions.append(f"Adapter training: 1.4x weights plus activation allowance and {2 * runtime} GiB runtime")
        else:
            estimate = weights * 1.25 + kv + runtime
            assumptions.append(f"Inference: 1.25x weights plus context/batch KV allowance and {runtime} GiB runtime")
        if gpu_required:
            estimated_vram = max(declared_vram, estimate)
        else:
            estimated_cpu_memory = estimate
        if not gpu_required and billions > .5:
            reasons.append("CPU-only model above 0.5B parameters requires a smaller model or a remote compute plan")
    ram_requested = number("ram_gb", number("memory_gb", max(.25, min(2, ram_budget))))
    if estimated_cpu_memory > ram_requested:
        reasons.append(f"Calculated CPU model memory {estimated_cpu_memory:.2f} GiB exceeds the declared RAM plan {ram_requested:.2f} GiB")
    disk_requested = number("disk_gb", .1)
    if ram_requested < 64 / 1024:
        reasons.append("The experiment RAM plan must allow at least 64 MiB for the Python runtime")
    if disk_requested < 16 / 1024:
        reasons.append("The experiment disk plan must allow at least 16 MiB for its files and logs")
    cores_requested = number("cpu_cores", min(2, cpu_total))
    timeout = number("timeout_seconds", number("timeout", 120))
    cpu_seconds = max(1, int(timeout * max(1, min(cores_requested, cpu_total))))
    if "cpu_seconds" in resources:
        requested_cpu_seconds = resources["cpu_seconds"]
        # The runner and OS limits use whole seconds. Reject fractions instead
        # of rounding a stricter declaration up or silently truncating it.
        if (isinstance(requested_cpu_seconds, bool) or not isinstance(requested_cpu_seconds, (int, float))
                or requested_cpu_seconds <= 0
                or isinstance(requested_cpu_seconds, float) and (
                    not math.isfinite(requested_cpu_seconds) or not requested_cpu_seconds.is_integer())):
            reasons.append("cpu_seconds must be a finite positive whole number of seconds")
        else:
            cpu_seconds = min(cpu_seconds, int(requested_cpu_seconds))
    if ram_requested > ram_budget or ram_budget <= 0:
        reasons.append(f"RAM needs {ram_requested:.2f} GiB, safe available budget is {ram_budget:.2f} GiB")
    if disk_requested > disk_budget or disk_budget <= 0:
        reasons.append(f"Disk needs {disk_requested:.2f} GiB, safe available budget is {disk_budget:.2f} GiB")
    if not 1 <= cores_requested <= cpu_total:
        reasons.append(f"CPU cores requested must be between 1 and {cpu_total}")
    if not 1 <= timeout <= 86400:
        reasons.append("Experiment timeout must be between 1 and 86400 seconds")
    if gpu_required and not devices:
        reasons.append("GPU required but no supported GPU was detected")
    if gpu_required and gpu_budget < 1 / 1024:
        reasons.append("GPU execution has no available task VRAM budget")
    if gpu_required and preferences.get("execution_mode") == "docker":
        reasons.append("Docker GPU passthrough is not configured")
    if (gpu_required or estimated_vram > 0) and estimated_vram > gpu_budget:
        reasons.append(f"VRAM estimate {estimated_vram:.2f} GiB exceeds a single GPU safe free budget of {gpu_budget:.2f} GiB")
    return {
        "approved": not reasons, "reasons": reasons, "assumptions": assumptions,
        "estimated_vram_gb": round(estimated_vram, 3),
        "estimated_cpu_model_gb": round(estimated_cpu_memory, 3),
        "effective_vram_budget_gb": round(gpu_budget, 3),
        "user_preferences": preferences,
        "available_capacity": {'ram_gb': round(ram_budget, 3), 'disk_gb': round(disk_budget, 3),
            'cpu_cores': cpu_total, 'vram_gb': round(gpu_budget, 3),
            'gpu_available': bool(devices) and gpu_budget > 0 and preferences.get('execution_mode') != 'docker'},
        "allocation_scope": 'limits是本方案申请的执行额度，available_capacity才是当前用户配置与实测硬件允许的容量。',
        "limits": {"memory_mb": max(64, int(min(ram_requested, ram_budget) * 1024)),
            "cpu_cores": max(1, int(min(cores_requested, cpu_total))),
            "timeout_seconds": max(1, min(timeout, 86400)),
            "cpu_seconds": cpu_seconds,
            "disk_mb": max(16, int(min(disk_requested, disk_budget) * 1024)),
            "gpu_allowed": gpu_required and not reasons,
            "gpu_memory_mb": int(gpu_budget * 1024) if gpu_required else 0,
            "torch_memory_cap": 'torch' in required_names,
            "gpu_uuid": gpu.get('uuid') if gpu_required else None,
            "python_executable": preferences.get("python_executable") or software.get("python_executable") or sys.executable,
            "gpu_index": gpu.get("index", 0) if gpu_required else None},
        "hardware_checked_at": hardware.get("checked_at"),
    }


def _plain(text: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", str(text or ""))).split())


def _title_matches(first: str, second: str) -> bool:
    first, second = (re.sub(r"[^\w]", "", unicodedata.normalize("NFKC", _plain(s)).casefold()) for s in (first, second))
    return bool(first and second) and first == second


def _author_matches(requested: str, canonical: str) -> bool:
    def words(value):
        return re.findall(r"\w+", unicodedata.normalize("NFKC", str(value)).casefold())
    first, second = words(requested), words(canonical)
    if not first or not second or first[-1] != second[-1]:
        return False
    if len(first) == 1 or len(second) == 1:
        return True  # Surname-only metadata is explicitly incomplete, but not contradictory.
    return first[0] == second[0] or (min(len(first[0]), len(second[0])) == 1 and first[0][0] == second[0][0])


def arxiv_version_match(original: dict, candidate: dict) -> dict | None:
    """Conservative bibliographic linkage; never verify the PDF's scientific claims."""
    if (candidate.get('verified') is not True or candidate.get('source') != 'arxiv'
            or candidate.get('verification', {}).get('status') != 'verified'
            or candidate.get('verification', {}).get('method') != 'arxiv_identifier_record'):
        return None
    first_doi, second_doi = _doi(original.get('doi')), _doi(candidate.get('doi'))
    if first_doi and second_doi and first_doi != second_doi:
        return None
    title_match = _title_matches(original.get('title', ''), candidate.get('title', ''))
    first, second = original.get('authors') or [], candidate.get('authors') or []
    # Compare the complete supplied author lists one-to-one. Surname-only data
    # cannot support a title-only linkage, even though verify accepts incomplete metadata.
    authors_match = (isinstance(first, list) and isinstance(second, list) and bool(first)
                     and len(first) == len(second) and len(first) <= 200)
    if authors_match:
        remaining = list(second)
        for author in first:
            match = next((i for i, known in enumerate(remaining) if _author_matches(author, known)), None)
            if match is None:
                authors_match = False
                break
            remaining.pop(match)
    detailed_authors = (authors_match and all(len(re.findall(r'\w+', str(a))) >= 2 for a in first + second))
    exact_doi = bool(first_doi and first_doi == second_doi)
    if exact_doi:
        if first and second and not authors_match:
            return None  # Explicit contradictory authors require human review.
    elif not (title_match and detailed_authors):
        return None
    return {'method': 'exact_doi' if exact_doi else 'exact_title_and_authors',
            'doi_match': exact_doi, 'title_match': title_match, 'authors_match': bool(authors_match),
            'metadata_only': True, 'version_equivalence_not_scientifically_verified': True}


def _search_relevance(query: str, paper: dict) -> dict:
    from .literature_relevance import domain_match, text
    stop = {"a", "an", "and", "or", "the", "for", "of", "in", "on", "with", "to", "via", "using", "based", "new", "latest", "recent", "study", "studies", "method", "methods"}
    def tokens(text):
        values = re.findall(r"[^\W_]+", str(text).casefold())
        values = [v for v in values if len(v) > 1 and v not in stop and not v.isdigit()]
        return {v[:-3] if v.endswith("ing") and len(v) > 6 else v[:-1] if v.endswith("s") and len(v) > 4 and not v.endswith("ss") else v for v in values}
    terms = tokens(query)
    searchable = tokens(text(paper))
    matched = sorted(terms & searchable)
    minimum = min(3, max(1, len(terms)))
    return {"query_terms": sorted(terms), "matched_terms": matched,
        "accepted": domain_match(query, paper) and (not terms or len(matched) >= minimum),
        "note": "Lexical relevance filter over available title/abstract; scientific relevance still needs review"}


def _doi(value: str | None) -> str | None:
    value = unquote(str(value or "")).strip()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/|^doi:\s*", "", value, flags=re.I)
    return value.lower() if re.fullmatch(r"10\.\d{4,9}/[^\s\x00-\x1f]+", value) else None


def _venue_names(value):
    return [_plain(v) for v in (value if isinstance(value, list) else [value]) if isinstance(v, str) and v.strip()]


def _crossref_paper(item: dict) -> dict:
    if not isinstance(item, dict):
        raise ValueError("Crossref record is not an object")
    doi = _doi(item.get("DOI"))
    date_record = item.get("published") or item.get("issued") or {}
    date = date_record.get("date-parts", []) if isinstance(date_record, dict) else []
    title = item.get("title") or []
    title = title[0] if isinstance(title, list) and title else title if isinstance(title, str) else ""
    authors = item.get("author") or []
    year = date[0][0] if isinstance(date, list) and date and isinstance(date[0], list) and date[0] else None
    parts = date[0] if isinstance(date, list) and date and isinstance(date[0], list) else []
    published = None
    if len(parts) >= 3:
        try:
            published = datetime(*parts[:3]).date().isoformat()
        except (ValueError, TypeError):
            pass
    return {"id": doi or _digest(item)[:24], "title": _plain(title),
        "authors": [_plain(" ".join([str(a.get("given") or ""), str(a.get("family") or a.get("name") or "")])) for a in authors if isinstance(a, dict)] if isinstance(authors, list) else [],
        "year": year if isinstance(year, int) and 1000 <= year <= datetime.now(timezone.utc).year + 1 else None, "doi": doi,
        "publication_date": published,
        "venue": {"names": list(dict.fromkeys([*_venue_names(item.get('container-title')),
                    *_venue_names((item.get('event') or {}).get('name'))])),
            "issn": item.get('ISSN') or [], "publication_type": item.get('type'),
            "kind": 'conference' if item.get('type') in {'proceedings-article', 'proceedings'} else
                    'journal' if item.get('type') == 'journal-article' else None},
        "url": f"https://doi.org/{doi}" if doi else item.get("URL", ""),
        "abstract": _plain(item.get("abstract", ""))[:12000], "source": "crossref",
        "citation_count": item.get('is-referenced-by-count'),
        "fulltext_url": next((link.get("URL") for link in (item.get("link") or [])
            if isinstance(link, dict) and link.get("content-type") == "application/pdf"
            and isinstance(link.get("URL"), str)), None),
        "verified": False, "verification": {"status": "pending", "note": METADATA_NOTE},
        "checked_at": now(), "provenance": "live_metadata"}


def _openalex_paper(item: dict) -> dict:
    if not isinstance(item, dict):
        raise ValueError("OpenAlex record is not an object")
    inverted = item.get("abstract_inverted_index") or {}
    words = {position: word for word, positions in inverted.items() if isinstance(positions, list)
        for position in positions if isinstance(position, int) and 0 <= position < 50000} if isinstance(inverted, dict) else {}
    doi = _doi(item.get("doi"))
    authorships = item.get("authorships") or []
    location = item.get("primary_location") if isinstance(item.get("primary_location"), dict) else {}
    oa_location = item.get("best_oa_location") if isinstance(item.get("best_oa_location"), dict) else {}
    return {"id": item.get("id", ""), "title": _plain(item.get("display_name") or item.get("title", "")),
        "authors": [(a.get("author") or {}).get("display_name", "") for a in authorships if isinstance(a, dict) and isinstance(a.get("author"), dict)] if isinstance(authorships, list) else [],
        "year": item.get("publication_year"), "doi": doi, "publication_date": item.get("publication_date"),
        "venue": {"names": _venue_names((location.get('source') or {}).get('display_name')),
            "issn": (location.get('source') or {}).get('issn') or [],
            "kind": (location.get('source') or {}).get('type'), "publication_type": item.get('type'),
            "source_id": (location.get('source') or {}).get('id')},
        "url": f"https://doi.org/{doi}" if doi else location.get("landing_page_url", item.get("id", "")),
        "abstract": " ".join(words[i] for i in sorted(words))[:12000], "source": "openalex",
        "citation_count": item.get('cited_by_count'),
        "oa_pdf_url": oa_location.get('pdf_url') if oa_location.get('is_oa') is True else None,
        "fulltext_locations": [{'url': loc['pdf_url'], 'source': 'openalex', 'access': 'open_access',
            'format': 'pdf', 'evidence': 'location.is_oa=true'} for loc in [oa_location, *(item.get('locations') or [])]
            if isinstance(loc, dict) and loc.get('is_oa') is True and loc.get('pdf_url')],
        "fulltext_url": oa_location.get("pdf_url") or location.get("pdf_url"),
        "verified": False, "verification": {"status": "pending", "note": METADATA_NOTE},
        "checked_at": now(), "provenance": "live_metadata"}


def _arxiv_papers(xml: str) -> list[dict]:
    root = ET.fromstring(xml)
    ns = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}
    papers = []
    for entry in root.findall("a:entry", ns):
        url = entry.findtext("a:id", "", ns).replace("http://", "https://")
        identifier = re.sub(r"^https?://(?:export\.)?arxiv\.org/abs/", "", url)
        if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-zA-Z.-]+/\d{7})(?:v\d+)?", identifier):
            continue
        date = entry.findtext("a:published", "", ns)
        papers.append({"id": f"arxiv:{identifier}", "arxiv_id": identifier,
            "fulltext_url": "https://arxiv.org/pdf/" + identifier,
            "oa_pdf_url": "https://arxiv.org/pdf/" + identifier,
            "fulltext_locations": [{'url': 'https://arxiv.org/pdf/' + identifier, 'source': 'arxiv',
                'access': 'open_access', 'format': 'pdf', 'evidence': 'arxiv_identifier'}],
            "title": _plain(entry.findtext("a:title", "", ns)),
            "authors": [_plain(a.findtext("a:name", "", ns)) for a in entry.findall("a:author", ns)],
            "year": int(date[:4]) if date[:4].isdigit() else None, "url": url, "publication_date": date[:10] or None,
            "doi": _doi(entry.findtext("x:doi", "", ns)),
            "abstract": _plain(entry.findtext("a:summary", "", ns))[:12000],
            "source": "arxiv", "verified": False, "verification": {"status": "pending", "note": METADATA_NOTE},
            "checked_at": now(), "provenance": "live_metadata"})
    return papers


class LiteratureService:
    """Shared persistent web-tool cache, independent of model/provider inference caches."""

    def __init__(self, store: Store, *, transport=None, timeout: float = 12):
        self.store, self.transport, self.timeout = store, transport, timeout
        self.last_errors: list[dict] = []
        self._arxiv_fallback_lock = asyncio.Lock()
        self._arxiv_fallback_last_request = 0.0

    async def _arxiv_fallback_pause(self):
        # Official API guidance asks for three seconds between consecutive calls.
        delay = 3 - (time.monotonic() - self._arxiv_fallback_last_request)
        if delay > 0:
            await asyncio.sleep(delay)
        self._arxiv_fallback_last_request = time.monotonic()

    async def find_arxiv_version(self, paper: dict) -> dict:
        """One bounded official query plus at most five identifier verifications.

        arXiv documents ti:/all:, not a doi: search field. DOI is therefore a
        discovery term only; linkage checks the returned arxiv:doi metadata.
        """
        title = ' '.join(re.findall(r'[^\W_]+', str(paper.get('title') or '')))[:500]
        doi = _doi(paper.get('doi'))
        terms = ([f'ti:"{title}"'] if title else [])
        if doi and '"' not in doi and '\\' not in doi:
            terms.append(f'all:"{doi}"')
        if not terms:
            return {'status': 'search_failed', 'query_count': 0, 'candidates_checked': 0,
                    'reason': 'No usable identity query'}
        query = ' OR '.join('(' + term + ')' for term in terms)
        async with self._arxiv_fallback_lock:
            try:
                await self._arxiv_fallback_pause()
                response = await self._fetch('https://export.arxiv.org/api/query',
                    {'search_query': query, 'start': 0, 'max_results': 5})
                root = ET.fromstring(response.text)
                if root.tag != '{http://www.w3.org/2005/Atom}feed':
                    raise ValueError('arXiv returned a non-Atom response')
                parsed = _arxiv_papers(response.text)
                entries = root.findall('{http://www.w3.org/2005/Atom}entry')
                if len(parsed) != len(entries):
                    raise ValueError('arXiv returned an error or invalid identifier entry')
                candidates = parsed[:5]
            except (httpx.HTTPError, ValueError, ET.ParseError) as exc:
                return {'status': 'search_failed', 'candidates_checked': 0,
                        'error_type': type(exc).__name__}
            matches, errors, checked = {}, [], 0
            for candidate in candidates:
                await self._arxiv_fallback_pause()
                verified = await self.verify(candidate, arxiv_only=True)
                checked += 1
                if verified.get('verification', {}).get('status') == 'unverified':
                    errors.append('Identifier verification unavailable')
                    continue
                basis = arxiv_version_match(paper, verified)
                if basis:
                    identifier = re.sub(r'v\d+$', '', verified['arxiv_id'])
                    previous = matches.get(identifier)
                    version = lambda p: int((re.search(r'v(\d+)$', p['arxiv_id']) or [None, '0'])[1])
                    if previous is None or version(verified) > version(previous['paper']):
                        matches[identifier] = {'paper': verified, 'identity_match_basis': basis}
            report = {'candidates_checked': checked, 'candidate_limit': 5, 'query_count': 1,
                      'query': query, 'errors': errors, 'bounded_search': True}
            if errors:
                return {**report, 'status': 'search_failed'}  # Unchecked candidates leave identity ambiguity.
            if len(matches) > 1:
                return {**report, 'status': 'ambiguous'}
            if not matches:
                return {**report, 'status': 'no_match'}
            return {**report, 'status': 'matched', **next(iter(matches.values()))}

    def _cached(self, key: str, ttl: int):
        record = self.store.get("tool_cache", key)
        if record and time.time() - record.get("timestamp", 0) < ttl:
            return record.get("value")
        return None

    def _cache(self, key: str, value):
        self.store.put("tool_cache", key, {"id": key, "tool": "literature", "timestamp": time.time(), "value": value})

    async def _fetch(self, url: str, params: dict | None = None) -> httpx.Response:
        params = dict(params or {})
        headers = {"User-Agent": "AutoResearch/0.1 (local scholarly metadata client)"}
        host = urlparse(url).hostname
        if host == 'api.semanticscholar.org' and os.environ.get('SEMANTIC_SCHOLAR_API_KEY'):
            headers['x-api-key'] = os.environ['SEMANTIC_SCHOLAR_API_KEY']
        if host == 'api.openalex.org' and os.environ.get('OPENALEX_API_KEY'):
            headers['Authorization'] = 'Bearer ' + os.environ['OPENALEX_API_KEY']
        if host == 'eutils.ncbi.nlm.nih.gov':
            params['tool'] = 'AutoResearch'
            if os.environ.get('NCBI_API_KEY'):
                params['api_key'] = os.environ['NCBI_API_KEY']
        if self.transport is None and host in {'export.arxiv.org', 'api.semanticscholar.org', 'eutils.ncbi.nlm.nih.gov'}:
            # Share pacing between search, verification and snowballing. Mock
            # transports stay CPU-only and deterministic.
            if not hasattr(self, '_metadata_pacing'):
                self._metadata_pacing = {}
            state = self._metadata_pacing.setdefault(host, {'lock': asyncio.Lock(), 'last': 0})
            interval = 3 if host == 'export.arxiv.org' else 1 if host == 'api.semanticscholar.org' else .34
            async with state['lock']:
                await asyncio.sleep(max(0, interval - (time.monotonic() - state['last'])))
                state['last'] = time.monotonic()
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport,
                follow_redirects=False, headers=headers) as client:
            async with client.stream("GET", url, params=params) as response:
                response.raise_for_status()
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 4 * 1024 * 1024:
                        raise ValueError("Metadata response exceeds 4 MiB")
                    chunks.append(chunk)
                headers = {key: value for key, value in response.headers.items() if key.lower() not in {"content-encoding", "content-length"}}
                return httpx.Response(response.status_code, headers=headers, content=b"".join(chunks), request=response.request)

    async def search(self, query: str, limit: int = 5, *, sort: str = "relevance", year_from: int | None = None, year_to: int | None = None, date_from: str | None = None, date_to: str | None = None, ccf_ab_only: bool = False) -> list[dict]:
        return (await self.search_with_report(query, limit, sort=sort, year_from=year_from, year_to=year_to, date_from=date_from, date_to=date_to, ccf_ab_only=ccf_ab_only))["papers"]

    async def search_with_report(self, query: str, limit: int = 5, *, sort: str = "relevance", year_from: int | None = None, year_to: int | None = None, date_from: str | None = None, date_to: str | None = None, ccf_ab_only: bool = False) -> dict:
        rag = getattr(self, 'paper_rag', None)
        options = dict(sort=sort, year_from=year_from, year_to=year_to, date_from=date_from, date_to=date_to, ccf_ab_only=ccf_ab_only)
        # Reuse validated local evidence before consulting any network cache/provider.
        if rag is not None and str(query).strip():
            if sort not in {'relevance', 'recent'}:
                raise ValueError('Literature sort must be relevance or recent')
            for bound in (date_from, date_to):
                if bound is not None:
                    datetime.strptime(bound, '%Y-%m-%d')
            current_year = datetime.now(timezone.utc).year
            if any(v is not None and (type(v) is not int or not 1800 <= v <= current_year + 1) for v in (year_from, year_to)):
                raise ValueError('Invalid literature year bounds')
            if (year_from and year_to and year_from > year_to) or (date_from and date_to and date_from > date_to):
                raise ValueError('Invalid literature date range')
            local = rag.search(str(query)[:500], max(1, min(int(limit), 120)), **options)
            freshness_required = sort == 'recent' or date_to is not None
            # A passage can mention a requested title without being that paper.
            # Such a RAG hit is useful evidence, not a reason to suppress discovery.
            from .paper_rag import search_intent
            subject, aspects = search_intent(query)
            def title_match(paper):
                match = _search_relevance(subject, {'title': paper.get('title', '')})
                terms = match['query_terms']
                return bool(terms) and len(match['matched_terms']) / len(terms) >= .85
            def content_match(paper):
                if _search_relevance(subject, paper)['accepted']:
                    return True
                fields = [value.get('fields', {}) for value in paper.get('rag_evidence', {}).values()
                          if isinstance(value, dict)]
                if not fields:
                    return False
                field_text = json.dumps(fields, ensure_ascii=False)
                match = _search_relevance(subject, {**paper,
                    'abstract': str(paper.get('abstract') or '') + ' ' + field_text})
                # Chinese method names can be embedded in a longer prose token.
                # Check those exact spans in the validated facet fields as well.
                matched = set(match['matched_terms']) | {term for term in match['query_terms']
                    if re.search(r'[\u3400-\u9fff]', term) and term in field_text}
                return bool(match['query_terms']) and len(matched) >= min(3, len(match['query_terms']))
            qualified = [p for p in local if p.get('verified') is True and (
                (aspects and not subject) or title_match(p) or (
                    p.get('rag_score', 0) >= .72 and content_match(p)))]
            required = max(1, min(int(limit), 120))
            sufficient = len(qualified) >= required
            browsing = bool(aspects) and not subject
            if browsing or (local and sufficient and (not freshness_required or rag.query_fresh(str(query)[:500], options))):
                self.last_errors = []
                hits = qualified[:required]
                return {'papers': hits, 'diagnostics': {'errors': [], 'sources': {'paper_rag': {'status': 'ok'}},
                    'result_count': len(hits), 'checked_at': now(), 'network_searched': False,
                    'rag': {'hit_count': len(local), 'qualified_count': len(qualified), 'used': bool(hits),
                            'missing_count': 0, 'fallback_reason': None,
                            **({'browse_only': True, 'network_requires_paper_or_topic': True} if browsing else {})},
                    'scope': '本地历史分析召回；不声称本次联网发现或检索穷尽。'}}
            missing = max(0, required - len(qualified))
            refresh = sufficient and freshness_required
            report = await self._network_search_with_report(query, required if refresh else max(1, missing), **options)
            rag.record_query(str(query)[:500], options, report)
            # Retain local analyses when a fresh query returns a matching identity.
            from .paper_rag import aliases
            merged = []
            seen = set()
            # Qualified local analyses retain priority. A weak incidental mention
            # must not displace the requested paper discovered on the network.
            for paper in [*qualified, *report['papers'], *local]:
                keys = set(aliases(paper))
                if not keys & seen:
                    reused = next((p for p in qualified if keys & set(aliases(p))), None)
                    merged.append(reused or paper)
                    seen.update(keys)
            if sort == 'recent':
                merged.sort(key=lambda p: p.get('publication_date') or '', reverse=True)
            merged = merged[:required]
            return {'papers': merged, 'diagnostics': {**report['diagnostics'], 'result_count': len(merged),
                'rag': {'hit_count': len(local), 'qualified_count': len(qualified), 'used': bool(qualified),
                        'missing_count': missing, 'network_limit': required if refresh else max(1, missing),
                        'fallback_reason': ('freshness_refresh' if refresh else
                            'insufficient_count' if qualified else 'partial_match') if local else 'no_qualified_match'},
                'network_searched': not report['diagnostics'].get('cache_hit', False)}}
        return await self._network_search_with_report(query, limit, **options)

    async def _network_search_with_report(self, query: str, limit: int = 5, *, sort: str = "relevance", year_from: int | None = None, year_to: int | None = None, date_from: str | None = None, date_to: str | None = None, ccf_ab_only: bool = False) -> dict:
        """Return source diagnostics with this exact query, including cached errors."""
        query, limit = str(query).strip()[:500], max(1, min(int(limit), 120))
        if not query:
            self.last_errors = []
            return {"papers": [], "diagnostics": {"errors": [], "sources": {}, "result_count": 0}}
        if sort not in {"relevance", "recent"}:
            raise ValueError("Literature sort must be relevance or recent")
        current_year = datetime.now(timezone.utc).year
        for year in (year_from, year_to):
            if year is not None and (type(year) is not int or not 1800 <= year <= current_year + 1):
                raise ValueError("Literature year bounds must be integers between 1800 and next year")
        if year_from is not None and year_to is not None and year_from > year_to:
            raise ValueError("year_from must not exceed year_to")
        for bound in (date_from, date_to):
            if bound is not None:
                datetime.strptime(bound, '%Y-%m-%d')
        if date_from and date_to and date_from > date_to:
            raise ValueError('date_from must not exceed date_to')
        search_settings = {"query": query, "sort": sort, "year_from": year_from, "year_to": year_to, "date_from": date_from, "date_to": date_to, "ccf_ab_only": ccf_ab_only}
        cache_key = "literature:search:v12:" + _digest([query.casefold(), limit, sort, year_from, year_to, date_from, date_to, ccf_ab_only])
        cached = self._cached(cache_key, 3600)
        if cached is not None:
            self.last_errors = cached["diagnostics"]["errors"]
            return {"papers": [{**p, "cache_hit": True, "provenance": "live_metadata_cached"} for p in cached["papers"]],
                    "diagnostics": {**cached["diagnostics"], "cache_hit": True}}

        from .codex_literature.retrieval import LiteratureRetriever
        from .codex_literature.retrieval.dedup import deduplicate
        from .codex_literature.retrieval.ranking import rank_papers
        sources, batches = await LiteratureRetriever(self).fetch_candidates(query, min(40, max(8, limit * 4)),
            sort=sort, year_from=year_from, year_to=year_to, date_from=date_from, date_to=date_to, ccf_ab_only=ccf_ab_only)
        errors, candidates = [], []
        # Round-robin sources so a broad Crossref response cannot consume the entire result set.
        for source, result in zip(sources, batches):
            if isinstance(result, BaseException):
                errors.append({"source": source, "error": type(result).__name__,
                    "status_code": result.response.status_code if isinstance(result, httpx.HTTPStatusError) else None,
                    "checked_at": now()})
        for rank in range(max([len(batch) for batch in batches if isinstance(batch, list)] or [0])):
            for batch in batches:
                if isinstance(batch, BaseException) or rank >= len(batch):
                    continue
                paper = batch[rank]
                if not paper.get("title"):
                    continue
                paper["relevance"] = _search_relevance(query, paper)
                if not paper["relevance"]["accepted"]:
                    continue
                if year_from is not None or year_to is not None:
                    if type(paper.get("year")) is not int or not (year_from or 1800) <= paper["year"] <= (year_to or current_year + 1):
                        continue  # Never mislabel out-of-range/unknown dates as a recent evidence set.
                if date_from or date_to:
                    from .literature_rounds import publication_date
                    published = publication_date(paper)
                    if published is None or not (date_from or '1800-01-01') <= published.isoformat() <= (date_to or '9999-12-31'):
                        continue
                if ccf_ab_only:
                    from .venue_policy import assess
                    if not assess(paper, candidate=True)["eligible"]:
                        continue
                candidates.append(paper)
        candidates = rank_papers(deduplicate(candidates), [query], mode=sort)
        if sort == "recent":
            candidates.sort(key=lambda p: (p.get("publication_date") or str(p.get("year") or "")), reverse=True)
        papers = []
        # Bound identifier lookups, but refill when authoritative metadata rejects
        # a candidate so an ineligible top hit cannot consume every result slot.
        candidate_pool = candidates[:min(40, max(6, limit * 3))]
        rejected = []
        batch_size = min(6, limit)
        for offset in range(0, len(candidate_pool), batch_size):
            originals = candidate_pool[offset:offset + batch_size]
            batch = []
            for start in range(0, len(originals), 6):
                batch.extend(await asyncio.gather(*(self.verify(p) for p in originals[start:start + 6])))
            for original, checked in zip(originals, batch):
                if checked.get('verified') is True:
                    # These locations came from actual source adapters, not a
                    # model's literature.verify arguments.
                    for field in ('oa_pdf_url', 'fulltext_locations'):
                        if original.get(field):
                            checked[field] = original[field]
            if ccf_ab_only:
                from .venue_policy import filter_papers
                batch = filter_papers(batch)
            papers.extend(p for p in batch if p.get('verified') is True)
            rejected.extend(p for p in batch if p.get('verified') is not True)
            if len(papers) >= limit:
                break
        # Rejected records remain visible only when verified candidates cannot
        # fill the request. They never crowd a verified record out of its slot.
        papers = (papers + rejected)[:limit]
        papers = rank_papers(papers, [query], mode=sort)
        if sort == 'recent':
            papers.sort(key=lambda p: p.get('publication_date') or str(p.get('year') or ''), reverse=True)
        if year_from is not None or year_to is not None:
            papers = [p for p in papers if type(p.get('year')) is int
                and (year_from or 1800) <= p['year'] <= (year_to or current_year + 1)]
        if date_from or date_to:
            # Identifier lookup may correct a stale or supplied publication date.
            from .literature_rounds import publication_date
            papers = [p for p in papers if publication_date(p) is not None
                and (date_from or '1800-01-01') <= publication_date(p).isoformat() <= (date_to or '9999-12-31')]
        for paper in papers:
            paper["search"] = {**search_settings, "coverage_note": "Bounded metadata search, not an exhaustive systematic review"}
            self.store.put("literature", _digest(paper["id"]), paper)
        self.last_errors = errors
        diagnostics = {**search_settings, "errors": errors, "result_count": len(papers), "checked_at": now(),
            'metadata_rejections': [{'id': p['id'], 'verification': p.get('verification')} for p in rejected],
            "cache_hit": False, "sources": {source: {"status": "error" if isinstance(batch, BaseException) else "ok",
                "candidate_count": 0 if isinstance(batch, BaseException) else len(batch),
                "returned_count": sum(p.get("source") == source for p in papers)}
                for source, batch in zip(sources, batches)}}
        self.store.put("literature_status", _digest(query), {"id": _digest(query), **diagnostics})
        report = {"papers": papers, "diagnostics": diagnostics}
        if papers:
            self._cache(cache_key, report)
        return report

    async def verify(self, paper: dict, *, arxiv_only: bool = False) -> dict:
        """Re-fetch an identifier from a fixed authoritative API and compare its title."""
        result = {**paper, "verified": False, "checked_at": now(), "provenance": "live_metadata"}
        for field in ('oa_pdf_url', 'fulltext_locations', 'fulltext_access', 'is_oa'):
            result.pop(field, None)
        result.pop('ccf', None)
        result.pop('venue', None)
        cache_key = ("literature:verify:arxiv:v3:" if arxiv_only else "literature:verify:v6:") + _digest({k: paper.get(k) for k in ("id", "title", "doi", "arxiv_id", "url", "authors", "year", "fulltext_url", "pmid", "semantic_scholar_id")})
        cached = self._cached(cache_key, 86400)
        if cached is not None:
            from .venue_policy import annotate
            return annotate({**cached, "cache_hit": True, "provenance": "live_metadata_cached"})
        canonical, method, evidence_url = None, None, None
        errors = []
        doi = _doi(paper.get("doi"))
        if doi and not arxiv_only:
            evidence_url = "https://api.crossref.org/works/" + quote(doi, safe="")
            try:
                response = await self._fetch(evidence_url)
                canonical = _crossref_paper(response.json()["message"])
                if canonical.get("doi") != doi:
                    canonical = None
                    errors.append("DOI returned by registry does not match requested DOI")
                else:
                    method = "crossref_doi_registry"
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                errors.append(f"Crossref lookup unavailable: {type(exc).__name__}")
        arxiv_id = paper.get("arxiv_id")
        if not arxiv_id:
            url = urlparse(str(paper.get("url", "")))
            if url.hostname in {"arxiv.org", "export.arxiv.org", "www.arxiv.org"}:
                arxiv_id = re.sub(r"^/(abs|pdf)/", "", url.path).removesuffix(".pdf")
        if canonical is None and arxiv_id and re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-zA-Z.-]+/\d{7})(?:v\d+)?", str(arxiv_id)):
            evidence_url = "https://export.arxiv.org/api/query?id_list=" + quote(str(arxiv_id), safe="")
            try:
                response = await self._fetch("https://export.arxiv.org/api/query", {"id_list": arxiv_id})
                found = _arxiv_papers(response.text)
                if found and re.sub(r"v\d+$", "", found[0]["arxiv_id"]) == re.sub(r"v\d+$", "", str(arxiv_id)):
                    canonical, method = found[0], "arxiv_identifier_record"
            except (httpx.HTTPError, ValueError, ET.ParseError) as exc:
                errors.append(f"arXiv lookup unavailable: {type(exc).__name__}")
        openalex_id = str(paper.get("id", "")).removeprefix("https://openalex.org/")
        if not arxiv_only and canonical is None and re.fullmatch(r"W\d+", openalex_id):
            evidence_url = "https://api.openalex.org/works/" + openalex_id
            try:
                response = await self._fetch(evidence_url)
                indexed = _openalex_paper(response.json())
                if str(indexed["id"]).endswith("/" + openalex_id) and (not doi or indexed.get("doi") == doi):
                    canonical, method = indexed, "openalex_index_record"
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                errors.append(f"OpenAlex lookup unavailable: {type(exc).__name__}")
        if not arxiv_only and canonical is None and re.fullmatch(r'\d+', str(paper.get('pmid') or '')):
            from .codex_literature.retrieval import pubmed
            try:
                found = await pubmed.lookup(self._fetch, [str(paper['pmid'])])
                canonical = next((p for p in found if p.get('pmid') == str(paper['pmid'])), None)
                if canonical:
                    method, evidence_url = 'pubmed_identifier_record', pubmed.BASE + 'efetch.fcgi?db=pubmed&id=' + str(paper['pmid'])
            except (httpx.HTTPError, ValueError, KeyError, TypeError, ET.ParseError) as exc:
                errors.append(f'PubMed lookup unavailable: {type(exc).__name__}')
        if not arxiv_only and canonical is None and paper.get('semantic_scholar_id'):
            from .codex_literature.retrieval import semantic_scholar
            try:
                indexed = await semantic_scholar.lookup(self._fetch, paper)
                if indexed and indexed.get('semantic_scholar_id') == paper['semantic_scholar_id']:
                    canonical, method = indexed, 'semantic_scholar_identifier_record'
                    evidence_url = semantic_scholar.BASE + '/' + quote(str(paper['semantic_scholar_id']), safe='')
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                errors.append(f'Semantic Scholar lookup unavailable: {type(exc).__name__}')
        if canonical:
            title_match = bool(canonical["title"]) and (not paper.get("title") or _title_matches(str(paper["title"]), canonical["title"]))
            identifier_match = not paper.get("doi") or bool(doi and doi == canonical.get("doi"))
            canonical_authors = canonical.get("authors") or []
            supplied_authors = paper.get("authors") or []
            author_match = not supplied_authors or (isinstance(supplied_authors, list) and all(
                any(_author_matches(str(author), str(known)) for known in canonical_authors) for author in supplied_authors))
            try:
                year_match = not paper.get("year") or bool(canonical.get("year") and abs(int(paper["year"]) - int(canonical["year"])) <= 1)
            except (TypeError, ValueError):
                year_match = False
            result["verified"] = title_match and identifier_match and author_match and year_match
            result["verification"] = {"status": "verified" if result["verified"] else "metadata_mismatch",
                "method": method, "evidence_url": evidence_url, "canonical_title": canonical["title"],
                "canonical_year": canonical.get("year"), "title_match": title_match,
                "identifier_match": identifier_match, "author_match": author_match, "year_match": year_match,
                "note": METADATA_NOTE, "metadata_sha256": _digest(canonical)}
            if arxiv_only:
                # Linkage must inspect the authoritative identifier record, not
                # rely on potentially stale DOI/author fields from search results.
                for field in ('title', 'authors', 'doi', 'arxiv_id', 'url', 'fulltext_url', 'source'):
                    result[field] = canonical.get(field)
            if result["verified"]:
                result['publication_date'] = canonical.get('publication_date')
                result['venue'] = canonical.get('venue')
                result['verification']['venue_metadata'] = canonical.get('venue')
                for field in ('oa_pdf_url', 'fulltext_locations', 'is_oa'):
                    if canonical.get(field):
                        result[field] = canonical[field]
                for field in ("title", "authors", "year", "doi", "url", "abstract", "fulltext_url"):
                    if not result.get(field):
                        result[field] = canonical.get(field)
        else:
            result["verification"] = {"status": "unverified", "errors": errors,
                "note": METADATA_NOTE, "reason": "No matching authoritative identifier record was retrieved"}
        result.setdefault("id", doi or _digest(paper)[:24])
        from .venue_policy import assess
        result['ccf'] = assess(result)
        if canonical:
            self._cache(cache_key, result)
        return result

    async def _validate_public_url(self, url: str):
        if any(ord(c) < 32 for c in url):
            raise ValueError('Reference URL contains control characters')
        parsed = urlparse(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Reference URL must be public HTTP(S), without credentials")
        if parsed.port not in (None, 80, 443):
            raise ValueError("Reference URL must use port 80 or 443")
        if parsed.hostname.lower() == "localhost" or parsed.hostname.lower().endswith((".localhost", ".local")):
            raise ValueError("Private/local URL targets are forbidden")
        addresses = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        from .public_dns import synthetic_answers, resolve_public
        resolved = list(dict.fromkeys(address[4][0] for address in addresses))
        if synthetic_answers(parsed.hostname, resolved):
            try:
                return await resolve_public(parsed.hostname, transport=self.transport)
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                raise SourceUnavailableError('tun_public_dns_unavailable') from None
        if not addresses or any(not ipaddress.ip_address(address[4][0].split("%")[0]).is_global for address in addresses):
            raise ValueError("Private, loopback, link-local, and reserved addresses are forbidden")
        return list(dict.fromkeys(address[4][0] for address in addresses))

    async def ingest_url(self, url: str, *, persist: bool = True, cache: bool = True,
                         link_metadata: bool = True, allow_empty_pdf: bool = False,
                         include_content: bool = False, prefer_pdf: bool = False) -> dict:
        """Retrieve bounded public paper text. External text is untrusted evidence data."""
        if include_content and (persist or cache):
            raise ValueError('Raw source bytes require nonpersistent, uncached retrieval')
        original, current = str(url).strip(), str(url).strip()
        cache_key = ("literature:document:pdf:" if prefer_pdf else "literature:document:") + _digest(original)
        cached = self._cached(cache_key, 3600) if cache else None
        if cached and source_text_issue(cached) is None:
            return {**cached, "cache_hit": True}
        total_bytes, html_redirects, pdf_locator = 0, 0, None
        async with httpx.AsyncClient(timeout=20, transport=self.transport, follow_redirects=False, trust_env=False,
                headers={"User-Agent": "AutoResearch/0.1 (reference retrieval)"}) as client:
            for hop in range(5):
                addresses = await self._validate_public_url(current)
                # Pin the validated address for this hop so a second DNS resolution cannot
                # redirect to loopback/private space. Preserve TLS SNI/certificate checking.
                parsed_url = httpx.URL(current)
                pinned_url = parsed_url.copy_with(host=addresses[0])
                async with client.stream("GET", pinned_url, headers={"Host": parsed_url.netloc.decode("ascii")},
                        extensions={"sni_hostname": parsed_url.host}) as response:
                    if response.is_redirect:
                        target = response.headers.get("location")
                        if not target:
                            raise ValueError("Redirect missing Location")
                        current = urljoin(current, target)
                        continue
                    response.raise_for_status()
                    chunks = []
                    async for chunk in response.aiter_bytes():
                        total_bytes += len(chunk)
                        if total_bytes > 12 * 1024 * 1024:
                            raise ValueError("Reference exceeds 12 MiB limit")
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    content_type = response.headers.get('content-type', '')
                    target = _html_refresh_target(content, current, content_type)
                    if target is not None:
                        # Same loop, hop limit, DNS validation and pinned connection
                        # as HTTP redirects. No scripts, embedded redirect query
                        # parameters, authentication challenges or paywalls are solved.
                        title = re.search(rb'<title[^>]*>(.*?)</title>', content, re.I | re.S)
                        issue = source_text_issue({'text': '', 'title': _plain(title[1].decode('utf-8', 'replace')) if title else ''})
                        if issue in {'access_denied', 'access_challenge', 'access_required'}:
                            raise SourceUnavailableError(issue)
                        current, html_redirects = target, html_redirects + 1
                        continue
                    if prefer_pdf and pdf_locator is None:
                        target = _html_pdf_target(content, current, content_type)
                        if target:
                            pdf_locator = {'landing_url': current, 'pdf_url': target,
                                           'basis': 'publisher_citation_pdf_url'}
                            current = target
                            continue  # Reuse the same hop, size and public-address checks.
                    document = self._document(content, Path(urlparse(current).path).name or "Web reference", content_type, allow_empty_pdf=allow_empty_pdf)
                    if pdf_locator and document.get('format') != 'pdf':
                        raise SourceUnavailableError('publisher_pdf_link_did_not_return_pdf')
                    issue = source_text_issue(document)
                    if issue:
                        raise SourceUnavailableError(issue)
                    document.update({"url": current, "original_url": original, "source": "web_reference", "provenance": "live_web_document",
                                     "redirect_hops": hop, "html_redirects_followed": html_redirects})
                    if pdf_locator:
                        document['pdf_locator'] = pdf_locator
                    document['source_links'] = self._source_links(content, current, response.headers.get('content-type', ''), document['text'])
                    arxiv_url = urlparse(current)
                    doi_match = re.search(r'10\.\d{4,9}/[^\s<>"\]]+', document["text"][:20000])
                    metadata = {"url": current}
                    is_arxiv_url = arxiv_url.hostname in {"arxiv.org", "export.arxiv.org"}
                    if doi_match and not is_arxiv_url:
                        metadata["doi"] = doi_match.group().rstrip(".,;)")
                    if link_metadata and (metadata.get("doi") or is_arxiv_url):
                        identity = await self.verify(metadata)
                        identity["identifier_relation"] = "arxiv_url" if is_arxiv_url else "doi_mentioned_in_document"
                        document["linked_metadata"] = identity
                        if is_arxiv_url and identity["verified"] and identity.get("title"):
                            document["title"] = identity["title"]
                        document["verification"]["linked_identifier_verified"] = identity["verified"]
                        document["verification"]["note"] = "Document fetched; linked identifier checked separately. The document's identity and claims are not independently verified."
                    if cache:
                        self._cache(cache_key, document)
                    if persist:
                        self.store.put("references", document["id"], document)
                    if include_content:
                        document['_content_bytes'] = content
                    return document
        raise ValueError("Too many reference redirects")

    @staticmethod
    def _source_links(content: bytes, base_url: str, content_type: str, text: str) -> list[dict]:
        """Expose bounded, untrusted discovery links without fetching them."""
        found = []
        def add(value):
            if len(found) >= 40 or not isinstance(value, str) or len(value) > 2048:
                return
            try:
                value = urljoin(base_url, html.unescape(value).strip())
                parsed = urlparse(value)
            except ValueError:
                return
            if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username or parsed.password:
                return
            host = parsed.hostname.lower()
            if host == 'localhost' or host.endswith(('.localhost', '.local')):
                return
            try:
                if not ipaddress.ip_address(host).is_global:
                    return
            except ValueError:
                pass
            value = parsed._replace(fragment='').geturl()
            if value and not any(row['url'] == value for row in found):
                found.append({'url': value, 'verified': False, 'retrieved': False,
                              'relation': 'link_in_retrieved_source'})
        class Links(HTMLParser):
            def handle_starttag(self, tag, attrs):
                if tag in {'a', 'link'}:
                    for name, value in attrs:
                        if name == 'href':
                            add(value)
        if 'html' in content_type.lower() or b'<html' in content[:500].lower():
            Links().feed(content[:2 * 1024 * 1024].decode('utf-8', errors='replace'))
        for match in re.finditer(r'https?://[^\s<>"\]\)]+', text[:150000]):
            add(match.group().rstrip('.,;'))
            if len(found) >= 40:
                break
        return found

    def ingest_file(self, path: str | Path) -> dict:
        target = Path(path).resolve(strict=True)
        if not target.is_file() or target.stat().st_size > 12 * 1024 * 1024:
            raise ValueError("Reference must be a file no larger than 12 MiB")
        document = self._document(target.read_bytes(), target.name,
            "application/pdf" if target.suffix.lower() == ".pdf" else "text/plain", allow_empty_pdf=True)
        document.update({"source": "user_file", "provenance": "user_supplied_untrusted", "url": None, "file_name": target.name})
        self.store.put("references", document["id"], document)
        return document

    @staticmethod
    def _document(content: bytes, name: str, content_type: str, *, allow_empty_pdf: bool = False) -> dict:
        is_pdf = content.startswith(b"%PDF-") or "application/pdf" in content_type
        is_article_xml = False
        pages_total, pages_extracted = None, None
        raw_pages = []
        if is_pdf:
            try:
                from pypdf import PdfReader
            except ImportError as exc:
                raise ValueError("PDF extraction requires pypdf; install the project dependencies") from exc
            reader = PdfReader(io.BytesIO(content))
            pages_total, pages_extracted = len(reader.pages), min(100, len(reader.pages))
            raw_pages = [(page.extract_text() or '').replace('\r\n', '\n').replace('\r', '\n') for page in reader.pages[:100]]
            text = "\n".join(raw_pages)
            title = str((reader.metadata or {}).get("/Title") or name)
        else:
            decoded = content.decode("utf-8", errors="replace")
            match = re.search(r"<title[^>]*>(.*?)</title>", decoded, re.I | re.S)
            title = _plain(match.group(1)) if match else name
            if 'xml' in content_type.lower() and '<article' in decoded[:2000]:
                root = ET.fromstring(decoded)
                body = root.find('body')
                if root.tag != 'article' or body is None:
                    raise ValueError('Full-text XML requires a JATS article body')
                is_article_xml = True
                title_node = root.find('.//article-title')
                if title_node is not None:
                    title = ' '.join(title_node.itertext())
                text = '\n'.join(' '.join(node.itertext()).strip() for node in root
                    if node.tag in {'front', 'body', 'back'})
            elif "html" in content_type or "<html" in decoded[:500].lower():
                decoded = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", decoded, flags=re.I | re.S)
                text = _plain(decoded)
            else:
                text = decoded
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        if is_pdf and not text.strip():
            text = ''
        if not text.strip() and not (is_pdf and allow_empty_pdf):
            raise ValueError("No readable reference text found (scanned PDFs require OCR)")
        retained = text[:150000]
        pages, cursor = [], 0
        for number, page_text in enumerate(raw_pages, 1):
            start, end = min(cursor, len(retained)), min(cursor + len(page_text), len(retained))
            visible = retained[start:end]
            pages.append({'page': number, 'page_number': number, 'start': start, 'end': end, 'text': visible,
                          'text_sha256': hashlib.sha256(visible.encode('utf-8')).hexdigest(),
                          'text_truncated': len(visible) < len(page_text), 'empty': not page_text.strip()})
            cursor += len(page_text) + 1
        empty_pages = [page['page'] for page in pages if page['empty']]
        return {"id": hashlib.sha256(content).hexdigest()[:24], "title": title[:500], "text": retained,
            "text_truncated": len(text) > 150000 or bool(pages_total and pages_total > pages_extracted),
            "extracted_chars_before_limit": len(text),
            "extracted_text_sha256": hashlib.sha256(text.encode('utf-8')).hexdigest(),
            "text_sha256": hashlib.sha256(retained.encode('utf-8')).hexdigest(),
            "extraction_note": "Text hash covers extracted text only; PDF pages and retained characters may be limited. Content hash covers all fetched bytes.",
            "pages": pages, "empty_text_pages": empty_pages, "ocr_performed": False,
            "extraction_status": "no_text_requires_ocr" if is_pdf and not text.strip() else "partial_text" if empty_pages or len(text) > 150000 or bool(pages_total and pages_total > pages_extracted) else "text_extracted",
            "format": "pdf" if is_pdf else "xml" if is_article_xml else "html" if 'html' in content_type.lower() else "text",
            "fulltext_xml": is_article_xml,
            "pages_total": pages_total, "pages_extracted": pages_extracted, "verified": False,
            "verification": {"status": "source_retrieved", "note": "Untrusted source text; document identity and scientific claims have not been verified."},
            "checked_at": now(), "content_sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}


# Real, deterministic data generation and fitting; used only for explicit demo runs.
# The same immutable split is reused across trials, tuning ranks val_mse.
BASELINE_CODE = r'''import hashlib, json, math, random, sys
from pathlib import Path

p = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
seed = int(p.get("seed", 42))
split_seed = int(p.get("split_seed", 2025))
samples = max(120, min(10000, int(p.get("samples", 800))))
epochs = max(1, min(3000, int(p.get("epochs", 120))))
learning_rate = float(p.get("learning_rate", 0.03))
variant = str(p.get("variant", "proposed"))
phase = str(p.get("phase", "validation"))
if phase not in ("validation", "test"):
    raise ValueError("phase must be validation or test")
degree = max(1, min(4, int(p.get("degree", 1 if variant == "baseline" else 2))))
l2 = max(0.0, float(p.get("l2", 0.001)))
if variant == "ablation":
    l2 = 0.0
rng = random.Random(split_seed)
data = []
for i in range(samples):
    x = rng.uniform(-1.0, 1.0)
    y = 0.7 + 1.8*x - 1.1*x*x + rng.gauss(0, 0.12)
    data.append(([x**d for d in range(degree + 1)], y, i))
rng.shuffle(data)
ntrain, nval = int(samples*.6), int(samples*.2)
train, validation, test = data[:ntrain], data[ntrain:ntrain+nval], data[ntrain+nval:]
model_rng = random.Random(seed)
weights = [model_rng.uniform(-.01, .01) for _ in range(degree + 1)]
for epoch in range(epochs):
    grad = [0.0] * len(weights)
    for x, y, _ in train:
        error = sum(w*v for w, v in zip(weights, x)) - y
        for j in range(len(weights)):
            grad[j] += 2*error*x[j]/len(train)
    for j in range(len(weights)):
        weights[j] -= learning_rate*(grad[j] + (2*l2*weights[j] if j else 0))
def mse(rows):
    return sum((sum(w*v for w,v in zip(weights,x))-y)**2 for x,y,_ in rows)/len(rows)
metrics = {"train_mse": mse(train), "train_samples": len(train)}
if phase == "test":
    metrics.update(test_mse=mse(test), test_samples=len(test))
else:
    metrics.update(val_mse=mse(validation), validation_samples=len(validation))
Path("split_manifest.json").write_text(json.dumps({"split_seed":split_seed,"train":[r[2] for r in train],
    "validation":[r[2] for r in validation],"test":[r[2] for r in test]}), encoding="utf-8")
Path("model.json").write_text(json.dumps({"weights":weights,"degree":degree}), encoding="utf-8")
def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
evaluation = {"dataset_id": "synthetic-quadratic-v1:" + fingerprint(sorted((r[2], r[0][1], r[1]) for r in data)),
    "split_fingerprints": {name: fingerprint(sorted(r[2] for r in rows)) for name, rows in
        [("train",train),("validation",validation),("test",test)]}, "seed": seed, "phase": phase, "actual_params": p}
Path("evaluation.json").write_text(json.dumps(evaluation), encoding="utf-8")
print(json.dumps({"metrics": metrics, "evaluation": evaluation, "artifacts": ["split_manifest.json", "model.json", "evaluation.json"],
    "dataset": "synthetic quadratic regression with seeded noise", "selection_metric": "val_mse", "phase": phase,
    "provenance": "real_cpu_execution_on_synthetic_data"}, allow_nan=False))
'''


_BOOTSTRAP = r'''import json, os, runpy, sys, time
from pathlib import Path
config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
deadline = time.monotonic() + 20
while not Path(config["start_file"]).exists():
    if time.monotonic() > deadline:
        raise RuntimeError("Experiment parent did not release execution gate")
    time.sleep(.02)
if os.name != "nt":
    import resource
    memory = int(config["memory_mb"])*1024*1024
    resource.setrlimit(resource.RLIMIT_AS, (memory,memory))
    if config["cpu_seconds"] is not None:
        cpu = int(config["cpu_seconds"])
        resource.setrlimit(resource.RLIMIT_CPU, (cpu,cpu+1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (int(config["disk_mb"])*1024*1024,)*2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (128,128))
sys.argv = [config["code_file"], *config.get("entry_arguments", [config["params_file"]])]
if config.get("code_bundle"):
    sys.path.insert(0, str(Path(config["code_file"]).resolve().parent))
if config.get("gpu_allowed") and config.get("gpu_memory_mb") and config.get("torch_memory_cap"):
    import torch
    total = torch.cuda.get_device_properties(0).total_memory
    torch.cuda.set_per_process_memory_fraction(min(1.0, config["gpu_memory_mb"] * 1024 * 1024 / total), 0)
runpy.run_path(config["code_file"], run_name="__main__")
'''


class _WindowsJob:
    """A Windows job kills descendants on parent exit and enforces aggregate limits."""
    def __init__(self, pid: int, memory_mb: int, cpu_seconds: int | None, cpu_cores: int, process_limit: int = 32):
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes, kernel.CreateJobObjectW.restype = [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes, kernel.AssignProcessToJobObject.restype = [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL
        kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
        self.kernel, self.handle = kernel, kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000 | 0x200 | 0x8  # kill on close, memory, process count
        if cpu_seconds is not None:
            info.BasicLimitInformation.LimitFlags |= 0x4
            info.BasicLimitInformation.PerJobUserTimeLimit = cpu_seconds * 10_000_000
        info.BasicLimitInformation.ActiveProcessLimit = process_limit
        info.JobMemoryLimit = memory_mb * 1024 * 1024
        process = None
        try:
            if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise OSError(ctypes.get_last_error(), "Setting Windows job limits failed")
            process = kernel.OpenProcess(0x100 | 0x1 | 0x400, False, pid)
            if not process or not kernel.AssignProcessToJobObject(self.handle, process):
                raise OSError(ctypes.get_last_error(), "Assigning experiment to Windows job failed")
            try:
                psutil.Process(pid).cpu_affinity(psutil.Process().cpu_affinity()[:cpu_cores])
            except (psutil.Error, AttributeError):
                pass
        except BaseException:
            self.close()
            raise
        finally:
            if process:
                kernel.CloseHandle(process)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class ExperimentRunner:
    def __init__(self, store: Store, data_dir: str | Path):
        self.store, self.data_dir = store, Path(data_dir).resolve()
        self.root = self.data_dir / "experiments"
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}
        self._active: dict[str, dict] = {}
        self._recover()

    def _recover(self):
        for result in self.store.list("experiments"):
            if result.get("status") != "running":
                continue
            owner = result.get("owner_pid")
            try:
                alive = owner and abs(psutil.Process(owner).create_time() - result.get("owner_started", 0)) < 1
            except psutil.Error:
                alive = False
            if not alive:
                pid = result.get("pid")
                try:
                    process = owned_process(pid, result.get("process_started"))
                    if process:
                        for child in reversed(verified_children(process)):
                            try:
                                child.kill()
                            except psutil.Error:
                                pass
                        process.kill()
                except psutil.Error:
                    pass
                result.update(status="interrupted", finished_at=now(), error="Parent process stopped; this attempt is preserved. Resume creates a new attempt.")
                key = result.get("storage_key", f"{result['run_id']}:{result['id']}")
                if self.store.get("run_deletions", result['run_id']):
                    continue
                try:
                    self.store.batch([
                        ("experiments", key, result),
                        ("experiment_attempts", f"{key}:{result.get('attempt', 1)}", result),
                    ])
                except RunDeletedError:
                    # A concurrent deletion may install its durable gate after
                    # the read above. Its cleanup owns the remaining records.
                    continue

    @staticmethod
    def _identifier(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(value)):
            raise ValueError("Run and trial IDs must be 1-100 ASCII letters, digits, underscores, or hyphens")
        return str(value)

    @staticmethod
    def _environment(path: Path, limits: dict) -> dict:
        allowed = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PATH", "SYSTEMDRIVE", "LANG", "LC_ALL", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS"}
        env = {name: value for name, value in os.environ.items() if name.upper() in allowed}
        tmp = path / "tmp"
        tmp.mkdir(exist_ok=True)
        env.update({"TEMP": str(tmp), "TMP": str(tmp), "TMPDIR": str(tmp),
            "HOME": str(tmp), "USERPROFILE": str(tmp), "APPDATA": str(tmp), "LOCALAPPDATA": str(tmp),
            "OMP_NUM_THREADS": str(limits["cpu_cores"]), "OPENBLAS_NUM_THREADS": str(limits["cpu_cores"]),
            "MKL_NUM_THREADS": str(limits["cpu_cores"]), "NUMEXPR_NUM_THREADS": str(limits["cpu_cores"]),
            "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
            "CUDA_VISIBLE_DEVICES": str(limits.get("gpu_uuid") or limits.get("gpu_index", 0)) if limits.get("gpu_allowed") else "-1"})
        if limits.get("python_executable"):
            env = interpreter_environment(limits["python_executable"], env)
            env.update(OMP_NUM_THREADS=str(limits["cpu_cores"]), OPENBLAS_NUM_THREADS=str(limits["cpu_cores"]))
        return env

    async def _terminate(self, process, *, job=None, docker_name=None):
        if docker_name and shutil.which("docker"):
            try:
                cleanup = await asyncio.create_subprocess_exec("docker", "rm", "-f", docker_name,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await asyncio.wait_for(cleanup.wait(), 8)
            except (OSError, asyncio.TimeoutError):
                pass
        active = next((item for item in self._active.values() if item.get("process") is process), {})
        parent = owned_process(process.pid, active.get("process_started"))
        children = verified_children(parent) if parent else []
        if job:
            job.close()
        try:
            for child in reversed(children):
                try:
                    child.kill()
                except psutil.Error:
                    pass
            if os.name != "nt" and parent and parent.is_running():
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    process.kill()
            else:
                # The subprocess handle retains the original process identity
                # even after Job Object cleanup releases its PID.
                process.kill()
        except (psutil.Error, OSError):
            pass
        try:
            await asyncio.wait_for(process.wait(), 5)
        except asyncio.TimeoutError:
            pass

    async def cancel(self, run_id: str):
        for active in list(self._active.values()):
            if active["run_id"] == run_id:
                active["cancel"].set()
                if active.get("process"):
                    await self._terminate(active["process"], job=active.get("job"), docker_name=active.get("docker_name"))

    async def run(self, run_id: str, code: str, params: dict, trial_id: str, limits: dict,
                  cancel_event: asyncio.Event | None = None) -> dict:
        async def transport_io_call(function, *args):
            # Cancellation must not release the trial lock while a disk/DB
            # worker is still writing its checkpoint or holding large buffers.
            task = asyncio.create_task(asyncio.to_thread(function, *args))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        break
                try:
                    task.result()
                except Exception:
                    pass
                raise

        invocation_started = time.monotonic()
        run_id, trial_id = self._identifier(run_id), self._identifier(trial_id)
        mode = limits.get("execution_mode")
        if mode not in {"local", "docker"}:
            raise ValueError("Execution requires explicit limits.execution_mode='local' (trusted code) or 'docker'")
        if mode == "docker" and limits.get("gpu_allowed"):
            raise ValueError("Docker GPU passthrough is not configured; use a CPU plan or reviewed local GPU execution")
        transport = limits.get('comparison_transport')
        transport_io, params_bytes = None, None
        if transport is not None:
            from . import comparison_transport as comparison
            comparison.validate_contract(transport.get('contract'), plan=transport.get('plan'))
            wire = params.get('comparison_inputs') if isinstance(params, dict) else None
            if (not isinstance(wire, dict) or set(wire) != {'protocol_version', 'role', 'records'}
                    or type(wire['protocol_version']) is not int or wire['protocol_version'] != 1
                    or wire['role'] not in ('producer', 'validation_finalize', 'test_finalize')
                    or not isinstance(wire['records'], list) or len(wire['records']) > comparison.MAX_RECORDS):
                raise ValueError('Invalid admitted comparison transport input')
            params_bytes = await transport_io_call(comparison.canonical, params)
            transport_io = comparison.io_limits(limits, params_size=len(params_bytes))
            if len(params_bytes) > transport_io['params_bytes']:
                raise ValueError('Comparison parameters exceed 256 MiB')
        elif not isinstance(params, dict) or len(json.dumps(params, allow_nan=False)) > 65536:
            raise ValueError("Trial parameters must be a JSON object no larger than 64 KiB")
        if not isinstance(code, str) or not code.strip() or len(code.encode()) > 250000:
            raise ValueError("Experiment Python source must be nonempty and no larger than 250 KiB")
        compile(code, "experiment.py", "exec")
        binding = limits.get('experiment_binding') or {}
        if binding and (set(binding) != {'research_cycle', 'code_revision', 'code_sha256', 'experiment_plan_sha256'}
                or type(binding.get('research_cycle')) is not int or binding['research_cycle'] < 1
                or type(binding.get('code_revision')) is not int or binding['code_revision'] < 0
                or binding.get('code_sha256') != hashlib.sha256(code.encode('utf-8')).hexdigest()
                or not re.fullmatch(r'[a-f0-9]{64}', str(binding.get('experiment_plan_sha256', '')))):
            raise ValueError('Invalid host experiment binding')
        code_bundle = limits.get('code_bundle')
        if code_bundle:
            from .code_bundle import validate as validate_bundle
            await transport_io_call(validate_bundle, code_bundle, code)
        from .runtime_training_policy import runner_authorized
        without_timing = runner_authorized(self.store, run_id, limits)
        if not without_timing and (limits.get('timeout_seconds', 120) is None or limits.get('cpu_seconds', 120) is None):
            raise ValueError('Nullable experiment time limits require an authenticated runtime policy')
        normalized = {**limits, "memory_mb": int(limits.get("memory_mb", 1024)),
            "cpu_cores": int(limits.get("cpu_cores", 1)),
            "timeout_seconds": None if without_timing else float(limits.get("timeout_seconds", limits.get("timeout", 120))),
            "cpu_seconds": None if without_timing else int(limits.get("cpu_seconds", 120)), "disk_mb": int(limits.get("disk_mb", 128))}
        numeric_limits = [normalized[k] for k in ('memory_mb', 'cpu_cores', 'disk_mb')]
        if not without_timing:
            numeric_limits += [normalized['timeout_seconds'], normalized['cpu_seconds']]
        if any(not math.isfinite(v) or v <= 0 for v in numeric_limits):
            raise ValueError("All experiment resource limits must be finite and positive")
        if transport_io:
            normalized.update(memory_mb=transport_io['child_memory_mb'], comparison_io=transport_io)
        if mode == "local":
            normalized["python_executable"] = validate_python(limits.get("python_executable", "")) or sys.executable
        normalized["torch_memory_cap"] = bool(limits.get("torch_memory_cap")) or any(re.match(r"^(torch|pytorch)(?:$|[\s<>=!~;\[])", str(p), re.I)
            for p in limits.get("required_packages", []))
        key = f"{run_id}:{trial_id}"
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            fingerprint_input = {"code": code, "params": params}
            if code_bundle:
                fingerprint_input['code_bundle_sha256'] = code_bundle['sha256']
            fingerprint = (await transport_io_call(_digest, fingerprint_input)
                           if transport else _digest(fingerprint_input))
            previous = self.store.get("experiments", key)
            if previous:
                if previous.get("fingerprint") != fingerprint:
                    raise ValueError("A trial ID is immutable; changed code or parameters require a new trial ID")
                if binding and any(key in previous and previous[key] != value for key, value in binding.items()):
                    raise ValueError('A saved trial cannot be rebound to another plan, code or research cycle')
                if binding and previous.get('status') == 'completed' and not previous.get('experiment_plan_sha256'):
                    raise ValueError('Completed trial has no saved plan binding; audit before reuse')
                if previous.get("status") not in {"interrupted"}:
                    return previous
            if cancel_event and cancel_event.is_set():
                raise asyncio.CancelledError("Experiment cancelled before process launch")
            attempt = int((previous or {}).get("attempt", 0)) + 1
            from .check_workspace import attempt_path
            path = attempt_path(self.root,run_id,trial_id,attempt,params,normalized)
            if not path.resolve().is_relative_to(self.root):
                raise ValueError("Trial path escapes experiment root")
            while path.exists():
                attempt += 1
                path = attempt_path(self.root,run_id,trial_id,attempt,params,normalized)
            path.mkdir(parents=True)
            # Keep the executed source identical to the reviewed UTF-8 bytes,
            # including CRLF on Windows, without text-mode newline conversion.
            (path / "experiment.py").write_bytes(code.encode("utf-8"))
            if code_bundle:
                from .code_bundle import stage as stage_bundle
                await transport_io_call(stage_bundle, code_bundle, code, path, normalized['disk_mb'] * 1024 * 1024)
            dataset_evidence = []
            yaml_receipt = None
            if normalized.get('hyperparameter_yaml'):
                from .hyperparameter_yaml import stage_trial
                yaml_receipt = stage_trial(path, normalized['hyperparameter_yaml'], params)
            if 'dataset_assets' in params:
                from .datasets import stage_assets
                dataset_evidence = await transport_io_call(stage_assets, self.store, self.data_dir, run_id,
                    params, path, normalized['disk_mb'] * 1024 * 1024)
                if cancel_event and cancel_event.is_set():
                    raise asyncio.CancelledError('Experiment cancelled during dataset preparation')
            if transport_io:
                await transport_io_call((path / 'params.json').write_bytes, params_bytes)
                params_bytes = None
            else:
                (path / "params.json").write_text(json.dumps(params, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
            (path / "bootstrap.py").write_text(_BOOTSTRAP, encoding="utf-8")
            bootstrap = {**normalized, "start_file": ".start", "code_file": "experiment.py", "params_file": "params.json"}
            if normalized.get('execution_interface'):
                from .experiment_config import INTERFACE, command_arguments
                if normalized['execution_interface'] != INTERFACE:
                    raise ValueError('Unknown experiment configuration interface')
                bootstrap['entry_arguments'] = command_arguments(params)
            (path / "bootstrap.json").write_text(json.dumps(bootstrap), encoding="utf-8")
            selected = normalized.get("python_executable")
            params_hash = await transport_io_call(_digest, params) if transport_io else _digest(params)
            environment = {"software": (inspect_python(selected) if selected and selected != sys.executable else _software_profile()) if mode == "local" else {"provenance": "docker_image", "image": limits.get("docker_image", "python:3.12-slim")}, "platform": platform.platform(),
                "code_sha256": hashlib.sha256(code.encode()).hexdigest(), "params_sha256": params_hash}
            if normalized.get('check_workspace_layout'):
                environment['check_workspace_layout']=normalized['check_workspace_layout']
            if dataset_evidence:
                environment['dataset_assets'] = dataset_evidence
            if yaml_receipt:
                environment['hyperparameter_yaml'] = yaml_receipt
            (path / "environment.json").write_text(json.dumps(environment, ensure_ascii=False, indent=2), encoding="utf-8")
            result = {"id": trial_id, "storage_key": key, "run_id": run_id, "status": "running", "metrics": {},
                "stdout": "", "stderr": "", "duration_seconds": 0, "artifact_path": str(path), "params": params,
                "attempt": attempt, "fingerprint": fingerprint, "started_at": now(), "limits": normalized,
                "owner_pid": os.getpid(), "owner_started": psutil.Process().create_time(),
                "provenance": "real_experiment_execution", "execution_mode": mode,
                "environment_path": str(path / "environment.json"),
                "isolation": "Docker container, no network" if mode == "docker" else "Trusted local subprocess; filesystem/network access is not sandboxed"}
            if dataset_evidence:
                result['dataset_assets'] = dataset_evidence
            result.update(binding)
            if previous:
                self.store.put("experiment_attempts", f"{key}:{previous['attempt']}", previous)
            if transport_io:
                try:
                    await transport_io_call(self.store.put, 'experiments', key, result)
                except asyncio.CancelledError:
                    result.update(status='interrupted', error='Comparison preparation cancelled before process launch')
                    await transport_io_call(self.store.put, 'experiments', key, result)
                    raise
            else:
                self.store.put("experiments", key, result)
            self.store.event(run_id, "experiment.started", f"Starting trial {trial_id}, attempt {attempt}", {"trial_id": trial_id, "mode": mode})
            started = invocation_started if transport_io or dataset_evidence else time.monotonic()
            internal_cancel = asyncio.Event()
            active = {"run_id": run_id, "cancel": internal_cancel}
            self._active[key] = active
            process, job, docker_name, readers = None, None, None, []
            try:
                if cancel_event and cancel_event.is_set():
                    raise asyncio.CancelledError('Comparison preparation cancelled before process launch')
                if (transport_io or dataset_evidence) and normalized['timeout_seconds'] is not None and time.monotonic() - started > normalized['timeout_seconds']:
                    raise ValueError('Comparison input preparation exceeded the admitted wall time')
                env = self._environment(path, normalized)
                if mode == "docker":
                    if not shutil.which("docker"):
                        raise RuntimeError("Docker is unavailable; install/start Docker or explicitly enable trusted local execution")
                    image = str(limits.get("docker_image", "python:3.12-slim"))
                    if not re.fullmatch(r"[A-Za-z0-9._/:@-]{1,200}", image):
                        raise ValueError("Invalid Docker image reference")
                    docker_name = "autoresearch-" + _digest([key, attempt])[:24]
                    command = ["docker", "run", "--rm", "--pull=never", "--name", docker_name,
                        "--network", "none", "--read-only", "--cap-drop=ALL", "--security-opt", "no-new-privileges",
                        "--memory", f"{normalized['memory_mb']}m", "--memory-swap", f"{normalized['memory_mb']}m",
                        "--cpus", str(normalized["cpu_cores"]), "--pids-limit", "32", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
                        "--mount", f"type=bind,source={path},target=/workspace", "--workdir", "/workspace",
                        "--user", f"{os.getuid()}:{os.getgid()}" if os.name != "nt" else "65534:65534",
                        "--env", f"OMP_NUM_THREADS={normalized['cpu_cores']}", "--env", f"OPENBLAS_NUM_THREADS={normalized['cpu_cores']}",
                        image, "python", "-I", "-u", "bootstrap.py", "bootstrap.json"]
                else:
                    command = [normalized["python_executable"], "-I", "-X", "utf8", "-u", "bootstrap.py", "bootstrap.json"]
                launch = asyncio.create_task(asyncio.create_subprocess_exec(*command, cwd=str(path), env=env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    **({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True})))
                try:
                    process = await asyncio.shield(launch)
                except asyncio.CancelledError:
                    # Retain the actual process handle even if cancellation arrives while
                    # asyncio is building its transport. The bootstrap gate is still closed.
                    process = await launch
                    raise
                active.update(process=process, docker_name=docker_name,
                              process_started=psutil.Process(process.pid).create_time())
                if os.name == "nt" and mode == "local":
                    job = _WindowsJob(process.pid, normalized["memory_mb"], normalized["cpu_seconds"], normalized["cpu_cores"])
                    active["job"] = job
                if os.name != "nt" and mode == "local":
                    try:
                        allowed = psutil.Process().cpu_affinity()
                        psutil.Process(process.pid).cpu_affinity(allowed[:normalized["cpu_cores"]])
                    except (psutil.Error, AttributeError):
                        pass
                result.update(pid=process.pid, process_started=active["process_started"])
                self.store.put("experiments", key, result)
                if internal_cancel.is_set() or (cancel_event and cancel_event.is_set()):
                    raise asyncio.CancelledError("Experiment cancelled before releasing startup gate")
                (path / ".start").write_text("released", encoding="utf-8")
                output_size = [0]
                output_exceeded = asyncio.Event()

                async def drain(stream, name):
                    if transport_io:
                        # Keep the protocol bytes complete on disk, not in an
                        # unbounded pipe buffer or the UI stdout preview. Parse
                        # the full archive later; diagnostics remain redacted.
                        tail = bytearray()
                        with (path / f'{name}.txt').open('wb') as archive:
                            while True:
                                block = await stream.read(65536)
                                if not block:
                                    break
                                output_size[0] += len(block)
                                if output_size[0] > transport_io['output_bytes']:
                                    output_exceeded.set()
                                    continue
                                archive.write(block)
                                tail.extend(block)
                                del tail[:-65536]
                        return redact(tail.decode('utf-8', errors='replace'))
                    captured = bytearray()
                    while True:
                        block = await stream.read(4096)
                        if not block:
                            break
                        output_size[0] += len(block)
                        if output_size[0] > 2 * 1024 * 1024:
                            output_exceeded.set()
                        else:
                            captured.extend(block)
                    # Redact after reassembly: secrets can straddle pipe read boundaries.
                    cleaned = redact(captured.decode("utf-8", errors="replace"))
                    (path / f"{name}.txt").write_text(cleaned, encoding="utf-8")
                    return cleaned[-65536:]

                readers = [asyncio.create_task(drain(process.stdout, "stdout")), asyncio.create_task(drain(process.stderr, "stderr"))]
                reason, peak_memory, max_cpu = None, 0, 0
                while process.returncode is None:
                    if internal_cancel.is_set() or (cancel_event and cancel_event.is_set()):
                        reason = "cancelled"
                    elif normalized["timeout_seconds"] is not None and time.monotonic() - started > normalized["timeout_seconds"]:
                        reason = "timeout"
                    elif output_exceeded.is_set():
                        reason = "output_limit"
                    else:
                        try:
                            parent = owned_process(process.pid, active["process_started"])
                            members = [parent, *verified_children(parent)] if parent else []
                            memory, cpu = 0, 0
                            for member in members:
                                try:
                                    memory += member.memory_info().rss
                                    times = member.cpu_times()
                                    cpu += times.user + times.system
                                except psutil.Error:
                                    pass
                            peak_memory, max_cpu = max(peak_memory, memory), max(max_cpu, cpu)
                            if mode == "local" and memory > normalized["memory_mb"] * 1024 * 1024:
                                reason = "memory_limit"
                            elif mode == "local" and normalized["cpu_seconds"] is not None and cpu > normalized["cpu_seconds"]:
                                reason = "cpu_limit"
                            total_size = sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())
                            if total_size > normalized["disk_mb"] * 1024 * 1024:
                                reason = "disk_limit"
                        except (psutil.Error, OSError):
                            pass
                    if reason:
                        await self._terminate(process, job=job, docker_name=docker_name)
                        break
                    await asyncio.sleep(.08)
                await process.wait()
                # Descendants must not survive a successfully exiting parent either.
                if job:
                    job.close()
                elif os.name != "nt" and mode == "local":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                output = await asyncio.wait_for(asyncio.gather(*readers), 5)
                result.update(stdout=output[0], stderr=output[1], exit_code=process.returncode,
                    peak_memory_mb=round(peak_memory / 1024 / 1024, 2), cpu_seconds=round(max_cpu, 3))
                if output_exceeded.is_set():
                    reason = reason or "output_limit"
                disk_used = sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())
                if disk_used > normalized["disk_mb"] * 1024 * 1024:
                    reason = reason or "disk_limit"
                if internal_cancel.is_set() or (cancel_event and cancel_event.is_set()):
                    reason = "cancelled"
                if reason:
                    result.update(status=reason, error=f"Experiment stopped: {reason}")
                elif process.returncode != 0:
                    result.update(status="failed", error=f"Experiment exited with code {process.returncode}")
                else:
                    parsed = None
                    if transport_io:
                        candidate = await transport_io_call(comparison.read_final_output, path / 'stdout.txt', transport_io['output_bytes'])
                        if normalized['timeout_seconds'] is not None and time.monotonic() - started > normalized['timeout_seconds']:
                            raise ValueError('Comparison archival/decoding exceeded the admitted wall time')
                        if isinstance(candidate, dict):
                            parsed = candidate
                        def output_hash():
                            with (path / 'stdout.txt').open('rb') as source:
                                return hashlib.file_digest(source, 'sha256').hexdigest()
                        result['output_archive'] = {'path': str(path / 'stdout.txt'), 'complete': True,
                            'bytes': (path / 'stdout.txt').stat().st_size,
                            'sha256': await transport_io_call(output_hash)}
                    else:
                        final_lines = result["stdout"].rstrip().splitlines()
                        try:
                            candidate = json.loads(final_lines[-1]) if final_lines else None
                            if isinstance(candidate, dict):
                                parsed = candidate
                        except json.JSONDecodeError:
                            pass
                    metrics = parsed.get("metrics") if parsed else None
                    if not isinstance(metrics, dict) or not metrics or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in metrics.values()):
                        result.update(status="failed", error="Last stdout line must be a JSON object with nonempty finite numeric metrics")
                    else:
                        artifact_files = []
                        for item in parsed.get("artifacts", []):
                            artifact = (path / str(item)).resolve()
                            if not artifact.is_relative_to(path) or not artifact.is_file() or (path / str(item)).is_symlink():
                                raise ValueError("Experiment output artifact must be an existing file inside this attempt")
                            artifact_files.append(str(artifact))
                        result.update(status="completed", metrics=metrics, artifacts=artifact_files,
                            experiment_metadata={k: v for k, v in parsed.items() if k not in {"metrics", "artifacts"}})
            except asyncio.CancelledError:
                if process:
                    await self._terminate(process, job=job, docker_name=docker_name)
                result.update(status="interrupted", error="Orchestration interrupted; attempt preserved for safe resume")
                raise
            except Exception as exc:
                if process:
                    await self._terminate(process, job=job, docker_name=docker_name)
                result.update(status="failed", error=redact(f"{type(exc).__name__}: {exc}"))
            finally:
                if job:
                    job.close()
                if readers:
                    try:
                        captured = await asyncio.wait_for(asyncio.gather(*readers, return_exceptions=True), 3)
                        for name, text in zip(("stdout", "stderr"), captured):
                            if isinstance(text, str):
                                result[name] = text
                    except asyncio.TimeoutError:
                        for reader in readers:
                            reader.cancel()
                        await asyncio.gather(*readers, return_exceptions=True)
                self._active.pop(key, None)
                result.update(duration_seconds=round(time.monotonic() - started, 3), finished_at=now())
                if transport_io:
                    def persist_result():
                        self.store.put('experiments', key, result)
                        self.store.put('experiment_attempts', f'{key}:{attempt}', result)
                    await transport_io_call(persist_result)
                else:
                    self.store.put("experiments", key, result)
                    self.store.put("experiment_attempts", f"{key}:{attempt}", result)
                temporary_result = path / "result.json.tmp"
                archive_result = result
                if transport_io:
                    # The complete output already has one lossless archive.
                    # A small manifest avoids duplicating echoed 256-MiB inputs
                    # twice more in this file and exceeding the same disk budget.
                    archive_result = {k: v for k, v in result.items() if k not in {'params', 'experiment_metadata', 'limits'}}
                    archive_result.update(params_archive='params.json', metadata_archive='stdout.txt',
                                          comparison_io=transport_io)
                temporary_result.write_text(json.dumps(redact(archive_result), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
                temporary_result.replace(path / "result.json")
                self.store.event(run_id, "experiment.finished", f"Trial {trial_id}: {result['status']}", {"trial_id": trial_id, "status": result["status"], "metrics": result["metrics"]})
            return result
