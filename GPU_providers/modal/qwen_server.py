"""
Modal app — Qwen2.5-32B-Instruct-GPTQ-Int4 via vLLM on L40S GPU.
Exposes an OpenAI-compatible chat completions endpoint and a .map()-able infer method.

Deploy:
    modal deploy GPU_providers/modal/qwen_server.py

Run batch analysis:
    modal run GPU_providers/modal/batch_analyse.py
"""

import modal

# ── Config ───────────────────────────────────────────────────────────────────
HF_MODEL_ID       = "Qwen/Qwen2.5-32B-Instruct-GPTQ-Int4"
MODEL_DIR         = "/models/qwen2.5-32b-gptq-int4"
MAX_MODEL_LEN     = 32_768
MAX_OUTPUT_TOKENS = 4_000
GPU_TYPE          = "l40s"

# ── Modal resources ──────────────────────────────────────────────────────────
app    = modal.App("gojep-qwen")
volume = modal.Volume.from_name("gojep-qwen-models", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "vllm==0.8.3",
        "huggingface_hub[hf_transfer]",
        "transformers==4.51.3",
        "accelerate",
        "sentencepiece",
    )
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1"})
)


# ── Model download (run once) ────────────────────────────────────────────────
@app.function(
    image=image,
    volumes={MODEL_DIR: volume},
    timeout=3600,
    cpu=4,
    memory=16384,
)
def download_model():
    """Download the model to the persistent volume. Run once before deploying."""
    import os
    from huggingface_hub import snapshot_download

    if os.path.exists(os.path.join(MODEL_DIR, "config.json")):
        print(f"Model already cached at {MODEL_DIR}")
        return MODEL_DIR

    print(f"Downloading {HF_MODEL_ID} ...")
    snapshot_download(
        repo_id=HF_MODEL_ID,
        local_dir=MODEL_DIR,
        ignore_patterns=["*.pt", "*.bin"],  # prefer safetensors
    )
    volume.commit()
    print(f"Download complete: {MODEL_DIR}")
    return MODEL_DIR


# ── Inference server ─────────────────────────────────────────────────────────
@app.cls(
    image=image,
    gpu=GPU_TYPE,
    volumes={MODEL_DIR: volume},
    timeout=600,
    scaledown_window=300,
    min_containers=0,
)
class QwenServer:

    @modal.enter()
    def load_model(self):
        from vllm import LLM
        import os

        if not os.path.exists(os.path.join(MODEL_DIR, "config.json")):
            raise RuntimeError(
                f"Model not found at {MODEL_DIR}. "
                "Run: modal run GPU_providers/modal/qwen_server.py::download_model"
            )

        print(f"Loading {HF_MODEL_ID} via vLLM ...")
        self.llm = LLM(
            model=MODEL_DIR,
            max_model_len=MAX_MODEL_LEN,
            gpu_memory_utilization=0.92,
            dtype="auto",
            quantization="gptq",
            enforce_eager=False,
        )
        print("Model loaded.")

    def _run_inference(self, request: dict) -> dict:
        import time
        from vllm import SamplingParams
        from transformers import AutoTokenizer

        messages    = request.get("messages", [])
        max_tokens  = request.get("max_tokens", MAX_OUTPUT_TOKENS)
        temperature = request.get("temperature", 0.1)

        # Apply chat template
        tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR)
        prompt = tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )

        sampling_params = SamplingParams(
            max_tokens=max_tokens,
            temperature=temperature,
        )

        t0 = time.time()
        outputs = self.llm.generate([prompt], sampling_params)
        elapsed = round(time.time() - t0, 2)

        content = outputs[0].outputs[0].text if outputs else ""

        return {
            "id": "chatcmpl-modal",
            "object": "chat.completion",
            "model": HF_MODEL_ID,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": outputs[0].outputs[0].finish_reason if outputs else "stop",
                }
            ],
            "usage": {},
            "_elapsed_s": elapsed,
            "_meta": request.get("_meta", {}),
        }

    @modal.method()
    def infer(self, request: dict) -> dict:
        """Programmatic inference — used with .map() for parallel batch processing."""
        return self._run_inference(request)

    @modal.fastapi_endpoint(method="POST", docs=True)
    def chat_completions(self, request: dict) -> dict:
        """OpenAI-compatible /v1/chat/completions endpoint."""
        return self._run_inference(request)


# ── Local test ───────────────────────────────────────────────────────────────
@app.local_entrypoint()
def test():
    """Quick smoke test — run with: modal run GPU_providers/modal/qwen_server.py"""
    import json
    server = QwenServer()
    response = server.infer.remote({
        "messages": [
            {"role": "system", "content": "You are a helpful assistant. Reply with valid JSON only."},
            {"role": "user",   "content": 'Return {"status": "ok", "model": "qwen"} and nothing else.'},
        ],
        "max_tokens": 64,
        "temperature": 0.0,
    })
    print(json.dumps(response, indent=2))
