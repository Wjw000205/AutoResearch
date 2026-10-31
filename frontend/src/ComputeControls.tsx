import { ReferenceText } from './ReferenceNames';
import { useEffect, useId, useRef, useState } from 'react';
import { LoaderCircle, RefreshCw } from 'lucide-react';
import { api, type ExecutionConfig, type Json } from './api';
import './compute.css';

export function effectiveVram(gpus: Json[], index: number | null | undefined, limit: number | null | undefined): number {
  const eligible = index == null ? gpus : gpus.filter(gpu => gpu.index === index);
  const free = Math.max(0, ...eligible.map(gpu => Number(gpu.free_gb) || 0));
  return Math.max(0, Math.min(free * 0.8, limit ?? Infinity));
}

export function GpuMemoryList({ hardware }: { hardware: Json }) {
  const gpus: Json[] = hardware.gpus ?? [];
  return <div className="compute-gpus" aria-label="本机可用显存">{gpus.length ? gpus.map(gpu => <div className="compute-gpu" key={gpu.index}><div><strong>GPU {gpu.index} · <ReferenceText value={gpu.name} /></strong><small>可用 / 总显存</small></div><span>{Number(gpu.free_gb).toFixed(2)} / {Number(gpu.total_gb).toFixed(2)} GiB</span></div>) : <p>未能读取可用显存。GPU 实验需通过硬件检查；CPU 实验仍可使用。<small>{hardware.gpu_detection}</small></p>}</div>;
}

export function ComputeControls({ value, onChange, locked = false }: {
  value: ExecutionConfig; onChange: (key: string, value: string | number | null) => void; locked?: boolean;
}) {
  const id = useId();
  const [scan, setScan] = useState<Json>();
  const [scanning, setScanning] = useState(false);
  const [scanError, setScanError] = useState('');
  const [hardware, setHardware] = useState<Json>();
  const [hardwareError, setHardwareError] = useState('');
  const [checking, setChecking] = useState(true);
  const [revision, setRevision] = useState(0);
  const scanRequest = useRef<AbortController | null>(null);
  const docker = value.execution_mode === 'docker';
  const executable = docker ? '' : value.python_executable ?? '';
  async function scanEnvironments() {
    scanRequest.current?.abort();
    const controller = new AbortController(); scanRequest.current = controller;
    setScanning(true); setScanError('');
    try { const result = await api<Json>('/python-environments', { signal: controller.signal }); if (!controller.signal.aborted) setScan(result); }
    catch (error) { if (!controller.signal.aborted) setScanError((error as Error).message); }
    finally { if (!controller.signal.aborted) setScanning(false); }
  }
  useEffect(() => { void scanEnvironments(); return () => scanRequest.current?.abort(); }, []);
  useEffect(() => {
    const controller = new AbortController();
    setChecking(true); setHardware(undefined); setHardwareError('');
    const timer = setTimeout(() => {
      void api<Json>(`/hardware?python_executable=${encodeURIComponent(executable)}`, { signal: controller.signal })
        .then(result => { if (!controller.signal.aborted) setHardware(result); })
        .catch(error => { if (!controller.signal.aborted) setHardwareError((error as Error).message); })
        .finally(() => { if (!controller.signal.aborted) setChecking(false); });
    }, 400);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [executable, revision]);
  const environments: Json[] = scan?.environments ?? [];
  const gpus: Json[] = hardware?.gpus ?? [];
  const software = hardware?.software;
  const selectedListed = environments.some(env => env.executable === (value.python_executable ?? ''));
  const gpuMissing = value.gpu_index != null && !gpus.some(gpu => gpu.index === value.gpu_index);
  return <section className="compute-controls" aria-label="Python 环境与显存配置"><div className="compute-heading"><div><h3>Python 环境与显存</h3><p>为实验选择本机环境和可使用的显存预算。</p></div><button type="button" className="btn btn-small" disabled={scanning} onClick={() => { void scanEnvironments(); setRevision(n => n + 1); }}>{scanning ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}{scanning ? '正在扫描…' : '重新扫描本机'}</button></div>
    <div className="form-grid"><div className="field"><label htmlFor={`${id}-mode`}>代码执行环境</label><select id={`${id}-mode`} disabled={locked} value={value.execution_mode ?? 'local'} onChange={e => onChange('execution_mode', e.target.value)}><option value="local">本机 Python</option><option value="docker">Docker 容器</option></select></div><div className="field"><label htmlFor={`${id}-python`}>Python 虚拟环境</label><select id={`${id}-python`} disabled={locked || docker} value={value.python_executable ?? ''} onChange={e => onChange('python_executable', e.target.value)}><option value="">沿用服务环境{scan?.current_executable ? ` · ${scan.current_executable}` : ''}</option>{value.python_executable && !selectedListed && <option value={value.python_executable}>自定义 · {value.python_executable}</option>}{environments.map(env => <option key={env.executable} value={env.executable} disabled={!env.available}>{env.kind} · <ReferenceText value={env.name} /> · {env.python_version ?? '不可用'} · {env.executable}</option>)}</select><small>{docker ? 'Docker 使用镜像内的 Python；本机环境选择仅用于本地执行。' : scan ? `发现 ${environments.filter(env => env.available).length} 个可用环境。${scan.scope}` : '扫描已知安装与虚拟环境目录，也可直接填写其他环境路径。'}</small></div></div>
    {!docker && <div className="field"><label htmlFor={`${id}-path`}>解释器绝对路径（可手动填写）</label><input id={`${id}-path`} disabled={locked} value={value.python_executable ?? ''} placeholder="留空沿用服务环境；或填写虚拟环境目录 / python.exe 的完整路径" onChange={e => onChange('python_executable', e.target.value)} /><small>环境包清单将用于方案检查，后续验收与实验也使用此解释器。</small></div>}
    {scanError && <p role="alert" className="compute-error">扫描失败：<ReferenceText value={scanError} /></p>}
    <div aria-live="polite">{checking ? <p className="panel-note">正在读取环境与可用显存…</p> : hardwareError ? <p role="alert" className="compute-error"><ReferenceText value={hardwareError} /></p> : hardware && <>{!docker && software && <div className="compute-runtime"><strong>{software.inventory_verified ? `Python ${software.python_version}` : '环境检查未通过'}</strong><span>{software.python_executable}</span><small>{software.inventory_verified ? `PyTorch ${software.installed_distributions?.torch ?? '未安装'} · CUDA ${software.gpu_runtime?.torch?.cuda_available === true ? '可用' : software.gpu_runtime?.torch?.cuda_available === false ? '不可用' : '未能确认'}` : software.inventory_error}</small></div>}<GpuMemoryList hardware={hardware} /></>}</div>
    <div className="form-grid"><div className="field"><label htmlFor={`${id}-gpu`}>实验显卡</label><select id={`${id}-gpu`} disabled={locked || docker} value={value.gpu_index ?? ''} onChange={e => onChange('gpu_index', e.target.value === '' ? null : Number(e.target.value))}><option value="">自动选择空闲显存最多的一张</option>{gpuMissing && <option value={value.gpu_index!}>GPU {value.gpu_index} · {checking ? '读取中' : '当前未检测到'}</option>}{gpus.map(gpu => <option key={gpu.index} value={gpu.index}>GPU {gpu.index} · <ReferenceText value={gpu.name} /></option>)}</select></div><div className="field"><label htmlFor={`${id}-vram`}>允许使用的显存（GiB）</label><input id={`${id}-vram`} type="number" min="0" max="100000" step="0.01" disabled={locked || docker} value={value.vram_limit_gb ?? ''} placeholder="留空自动；0 表示仅 CPU" onChange={e => onChange('vram_limit_gb', e.target.value === '' ? null : Number(e.target.value))} /><small>填写预算上限，系统不会为了达到额度而占满显存。</small></div></div>
    <div className="form-grid three">{([
      ['cpu_limit_cores', 'CPU 核数上限', 1, hardware?.cpu_cores],
      ['ram_limit_gb', '内存上限（GiB）', 0.1, hardware ? Number(hardware.ram_available_gb) * 0.65 : undefined],
      ['disk_limit_gb', '磁盘上限（GiB）', 0.1, hardware ? Number(hardware.disk_free_gb) * 0.8 : undefined],
    ] as const).map(([key, label, step, available]) => <div className="field" key={key}><label htmlFor={`${id}-${key}`}>{label}</label><input id={`${id}-${key}`} type="number" min={step} step={step} disabled={locked} value={value[key] ?? ''} placeholder="留空按可用资源自动确定" onChange={event => onChange(key, event.target.value === '' ? null : Number(event.target.value))} /><small>{available != null ? `当前安全可用约 ${Number(available).toFixed(key === 'cpu_limit_cores' ? 0 : 2)}` : '运行前重新检测可用资源'}</small></div>)}</div>
    <p className="panel-note">这里设置您允许使用的资源上限。Agent 需单独声明实验的资源需求；不会把未填写的需求视为禁止 GPU 或限制为 2 核、2 GB 内存。</p>
    <p className="panel-note"><ReferenceText value={docker ? '当前 Docker 实验仅支持 CPU。' : hardware ? `当前有效预算 ${effectiveVram(gpus, value.gpu_index, value.vram_limit_gb).toFixed(2)} GiB，取填写额度与所选显卡空闲显存 80% 中的较小值。` : '每次实验前重新读取空闲显存，保留 20% 余量。'} /> 本设置不预留或独占显存；用于实验准入，PyTorch 实验同时限制其缓存分配器额度。</p>
    {locked && <p className="panel-note">代码实现或实验已启动，环境与显存配置已锁定；不同配置请用于新研究。</p>}
  </section>;
}
