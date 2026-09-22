#!/usr/bin/env python3
"""Isolated local Qwen3-ASR + FunASR VAD/CAM++ worker.

The worker intentionally runs in a dedicated Python environment because the
official qwen-asr package pins a Transformers version that should not replace
the desktop workflow's main dependency set.  It reads and writes local JSON
files only; audio is never sent to a remote transcription service.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable


MODEL_ALIASES = {
    "local:qwen3-asr-auto": "Qwen/Qwen3-ASR-0.6B",
    "local:qwen3-asr-1.7b": "Qwen/Qwen3-ASR-1.7B",
    "local:qwen3-asr-0.6b": "Qwen/Qwen3-ASR-0.6B",
}
DEFAULT_FALLBACK_MODEL = "Qwen/Qwen3-ASR-0.6B"


class LocalQwenWorkerError(RuntimeError):
    pass


def resolve_model_id(value: str) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized in MODEL_ALIASES:
        return MODEL_ALIASES[normalized]
    if str(value or "").strip().startswith("Qwen/Qwen3-ASR-"):
        return str(value).strip()
    raise LocalQwenWorkerError(f"不支持的本地 Qwen 模型：{value}")


def _cached_reference(
    reference: str,
    cache_root: Path,
    *,
    local_files_only: bool = False,
) -> str:
    candidate = cache_root / reference.rsplit("/", 1)[-1]
    if candidate.is_dir() and any(
        (candidate / name).is_file()
        for name in ("config.json", "configuration.json", "model.pt")
    ):
        return str(candidate.resolve())
    if local_files_only:
        raise LocalQwenWorkerError(
            f"本地模型未安装完整：{candidate}；请执行 setup_qwen_local.ps1"
        )
    return reference


def _looks_like_cuda_oom(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".casefold()
    return any(
        marker in text
        for marker in (
            "cuda out of memory",
            "cuda error: out of memory",
            "cublas_status_alloc_failed",
            "outofmemoryerror",
        )
    )


def _release_cuda() -> None:
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _json_default(value: Any) -> Any:
    if hasattr(value, "tolist"):
        return value.tolist()
    if hasattr(value, "item"):
        return value.item()
    return str(value)


def _load_default_model_factory() -> Callable[..., Any]:
    """Load official qwen_asr without the sibling cloud adapter shadowing it."""
    script_directory = Path(__file__).resolve().parent
    original_path = list(sys.path)
    try:
        sys.path[:] = [
            entry
            for entry in sys.path
            if not entry or Path(entry).resolve() != script_directory
        ]
        official_qwen = __import__("qwen_asr", fromlist=["Qwen3ASRModel"])
        if not hasattr(official_qwen, "Qwen3ASRModel"):
            raise ImportError(
                f"loaded non-official qwen_asr module: {official_qwen.__file__}"
            )
        from funasr import AutoModel
    except ImportError as exc:
        raise LocalQwenWorkerError(
            "隔离环境缺少兼容的官方 qwen-asr/FunASR；请运行 setup_qwen_local.ps1"
        ) from exc
    finally:
        sys.path[:] = original_path
    return AutoModel


def _honor_short_speaker_hint(model: Any, speaker_count: Any) -> None:
    """Honor an oracle count below FunASR's hard-coded 20-embedding cutoff."""
    if speaker_count in (None, ""):
        return
    backend = getattr(model, "cb_model", None)
    spectral_cluster = getattr(backend, "spectral_cluster", None)
    original_forward = getattr(backend, "forward", None)
    if not callable(spectral_cluster) or not callable(original_forward):
        return

    def forward(features, **params):
        oracle = params.get("oracle_num")
        if oracle and int(oracle) <= int(features.shape[0]) < 20:
            values = features
            if hasattr(values, "detach"):
                values = values.detach()
            if hasattr(values, "cpu"):
                values = values.cpu()
            if hasattr(values, "numpy"):
                values = values.numpy()
            return spectral_cluster(values, int(oracle))
        return original_forward(features, **params)

    backend.forward = forward


def _run_model(
    request: dict[str, Any],
    model_id: str,
    *,
    model_factory: Callable[..., Any],
) -> dict[str, Any]:
    source = Path(str(request["input"])).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    cache_root = Path(str(request["cache_root"])).resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    device = str(request.get("device") or "cuda:0")
    diarization = bool(request.get("diarization", True))

    local_files_only = bool(request.get("local_files_only", False))
    local_model_path = _cached_reference(
        model_id,
        cache_root,
        local_files_only=local_files_only,
    )
    options: dict[str, Any] = {
        "model": model_id,
        "model_path": local_model_path,
        "model_conf": {},
        "vad_model": _cached_reference(
            str(request.get("vad_model") or "fsmn-vad"),
            cache_root,
            local_files_only=local_files_only,
        ),
        "vad_kwargs": {
            "max_single_segment_time": int(
                request.get("max_segment_milliseconds") or 30_000
            )
        },
        "device": device,
        "disable_update": True,
        "hub": "ms",
        "dtype": "bf16" if device.startswith("cuda") else "fp32",
        "max_inference_batch_size": int(
            request.get("max_inference_batch_size") or 1
        ),
        "max_new_tokens": int(request.get("max_new_tokens") or 256),
    }
    if diarization:
        options.update(
            {
                "spk_model": _cached_reference(
                    str(request.get("speaker_model") or "cam++"),
                    cache_root,
                    local_files_only=local_files_only,
                ),
                "spk_mode": "vad_segment",
            }
        )
    speaker_count = request.get("speaker_count")
    if speaker_count not in (None, ""):
        options["preset_spk_num"] = int(speaker_count)

    print(f"Local Qwen3-ASR: loading {model_id} on {device}", flush=True)
    model = model_factory(**options)
    _honor_short_speaker_hint(model, speaker_count)
    generate_options: dict[str, Any] = {
        "input": str(source),
        "batch_size_s": int(request.get("batch_size_seconds") or 30),
        # Quiet/BGM VAD regions can make Qwen transcribe the vocabulary itself.
        # Keep decoding audio-only; reviewed spelling corrections still apply.
        "context": "",
        "return_raw_text": True,
        "sentence_timestamp": False,
    }
    language = request.get("language")
    if language:
        generate_options["language"] = str(language)
    if speaker_count not in (None, ""):
        generate_options["preset_spk_num"] = int(speaker_count)

    try:
        result = model.generate(**generate_options)
    except Exception:
        del model
        _release_cuda()
        raise
    raw = result[0] if isinstance(result, list) and result else result
    if not isinstance(raw, dict):
        raise LocalQwenWorkerError("本地 Qwen3-ASR 没有返回 JSON 对象")
    if "sentence_info" not in raw and raw.get("text") == "" and raw.get("timestamp") == []:
        # FunASR uses the same empty sentinel for no speech and failed ASR
        # batches. Confirm with VAD before accepting an empty transcript.
        vad = model.inference(
            str(source), model=model.vad_model, kwargs=dict(model.vad_kwargs)
        )
        if not (isinstance(vad, list) and len(vad) == 1
                and isinstance(vad[0], dict) and vad[0].get("value") == []):
            raise LocalQwenWorkerError("本地 Qwen 返回空结果，但 VAD 未确认无语音；请重试转写")
        raw = {**raw, "sentence_info": [], "no_speech": True}
    return raw


def run_request(
    request: dict[str, Any],
    *,
    model_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    if model_factory is None:
        model_factory = _load_default_model_factory()

    requested = resolve_model_id(str(request.get("model") or ""))
    fallback = str(request.get("fallback_model") or "").strip()
    fallback = resolve_model_id(fallback) if fallback else ""
    try:
        raw = _run_model(request, requested, model_factory=model_factory)
        used = requested
        fallback_used = False
    except Exception as exc:
        if not fallback or fallback == requested or not _looks_like_cuda_oom(exc):
            raise
        print(
            f"Local Qwen3-ASR: {requested} exceeded GPU memory; "
            f"retrying with {fallback}",
            file=sys.stderr,
            flush=True,
        )
        _release_cuda()
        raw = _run_model(request, fallback, model_factory=model_factory)
        used = fallback
        fallback_used = True

    sentence_info = raw.get("sentence_info", [])
    detected_speakers = {
        str(item.get("spk"))
        for item in sentence_info
        if isinstance(item, dict) and item.get("spk") not in (None, "")
    }
    speaker_count_hint = request.get("speaker_count")

    return {
        "provider": "qwen3-asr-local",
        "requested_model": requested,
        "model": used,
        "fallback_model": fallback,
        "fallback_used": fallback_used,
        "device": str(request.get("device") or "cuda:0"),
        "diarization_enabled": bool(request.get("diarization", True)),
        "speaker_model": str(request.get("speaker_model") or "cam++"),
        "vad_model": str(request.get("vad_model") or "fsmn-vad"),
        "speaker_count_hint": (
            int(speaker_count_hint) if speaker_count_hint not in (None, "") else None
        ),
        "detected_speaker_count": len(detected_speakers),
        "fully_local": True,
        "audio_uploaded": False,
        "context_policy": "audio-only-no-hotword-prompt-v2",
        "guard_terms": request.get("guard_terms") or str(request.get("context") or "").split("、"),
        "result": raw,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8-sig"))
    try:
        payload = run_request(request)
    except Exception as exc:
        payload = {
            "error": str(exc),
            "error_type": type(exc).__name__,
            "fully_local": True,
            "audio_uploaded": False,
        }
        args.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        print(f"Local Qwen3-ASR failed: {exc}", file=sys.stderr, flush=True)
        return 1
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    print(
        f"Local Qwen3-ASR completed with {payload['model']} "
        f"(fallback={payload['fallback_used']})",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
