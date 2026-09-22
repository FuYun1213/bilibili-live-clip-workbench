"""Install the pinned official acoustic aligner used by selection/Flow3."""
from pathlib import Path
from huggingface_hub import snapshot_download
from authoritative_alignment import MODEL_PATH, MODEL_REVISION
if __name__ == "__main__":
    print(snapshot_download(repo_id="Qwen/Qwen3-ForcedAligner-0.6B",
        revision=MODEL_REVISION, local_dir=str(MODEL_PATH),
        allow_patterns=["*.json", "*.txt", "*.safetensors"], token=False))
