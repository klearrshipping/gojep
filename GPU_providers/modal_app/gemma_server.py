"""
Modal app — Gemma 4 26B (A4B MoE, Q8_0 GGUF) on L40S GPU
Exposes an OpenAI-compatible /v1/chat/completions endpoint.

Deploy:
    modal deploy modal_app/gemma_server.py

Get endpoint URL:
    modal app show gojep-gemma

Run once to download the model to the volume:
    modal run modal_app/gemma_server.py::download_model
"""

import modal

# ── Config ──────────────────────────────────────────────────────────────────
HF_REPO       = "unsloth/gemma-4-26B-A4B-it-GGUF"
GGUF_FILENAME = "gemma-4-26B-A4B-it-Q8_0.gguf"
MODEL_DIR     = "/models"
MODEL_PATH    = f"{MODEL_DIR}/{GGUF_FILENAME}"

N_CTX           = 65_536   # 64K context — fits ~37K token chunks + system prompt + 6K output
N_GPU_LAYERS    = -1       # -1 = all layers on GPU
THINKING_BUDGET = 2_048    # max reasoning tokens before model must produce output
MAX_OUTPUT_TOKENS_DEFAULT = 4_096

# ── Modal resources ──────────────────────────────────────────────────────────
app    = modal.App("gojep-gemma")
volume = modal.Volume.from_name("gojep-models", create_if_missing=True)

image = (
    # CUDA runtime image — has libcudart.so.12, no compiler (avoids CC=clang issue)
    modal.Image.from_registry("nvidia/cuda:12.4.0-runtime-ubuntu22.04", add_python="3.11")
    .apt_install("libgomp1", "curl")
    .pip_install("huggingface_hub[hf_transfer]")
    .env({"HF_HUB_ENABLE_HF_TRANSFER": "1"})
    .pip_install(
        # Pre-built CUDA 12.4 wheel — matches the cuda runtime version
        "llama-cpp-python",
        extra_index_url="https://abetlen.github.io/llama-cpp-python/whl/cu124",
    )
    .pip_install("fastapi", "uvicorn[standard]", "sse-starlette", "pydantic>=2.0")
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
    """Download the GGUF model to the persistent volume. Run once before deploying."""
    import os
    from huggingface_hub import hf_hub_download

    if os.path.exists(MODEL_PATH):
        size_gb = os.path.getsize(MODEL_PATH) / 1e9
        print(f"Model already cached: {MODEL_PATH} ({size_gb:.1f} GB)")
        return MODEL_PATH

    print(f"Downloading {HF_REPO}/{GGUF_FILENAME} ...")
    hf_hub_download(
        repo_id=HF_REPO,
        filename=GGUF_FILENAME,
        local_dir=MODEL_DIR,
    )
    volume.commit()
    size_gb = os.path.getsize(MODEL_PATH) / 1e9
    print(f"Download complete: {MODEL_PATH} ({size_gb:.1f} GB)")
    return MODEL_PATH


# ── Inference server ─────────────────────────────────────────────────────────
@app.cls(
    image=image,
    gpu="l40s",
    volumes={MODEL_DIR: volume},
    timeout=600,           # max 10 min per request (well above any single chunk)
    scaledown_window=300,  # keep warm for 5 min between requests
    min_containers=0,      # scale to zero when idle
)
class GemmaServer:

    @modal.enter()
    def load_model(self):
        import os
        from llama_cpp import Llama

        if not os.path.exists(MODEL_PATH):
            raise RuntimeError(
                f"Model not found at {MODEL_PATH}. "
                "Run: modal run modal_app/gemma_server.py::download_model"
            )

        print(f"Loading model from {MODEL_PATH} ...")
        self.llm = Llama(
            model_path=MODEL_PATH,
            n_ctx=N_CTX,
            n_gpu_layers=N_GPU_LAYERS,
            flash_attn=True,   # reduces KV cache memory usage significantly
            verbose=False,
        )
        print("Model loaded.")

    def _run_inference(self, request: dict) -> dict:
        """Shared inference logic used by both the web endpoint and .map()."""
        import time

        messages        = request.get("messages", [])
        max_tokens      = request.get("max_tokens", MAX_OUTPUT_TOKENS_DEFAULT)
        temperature     = request.get("temperature", 0.1)
        thinking_budget = request.get("thinking_budget", THINKING_BUDGET)

        t0 = time.time()
        result = self.llm.create_chat_completion(
            messages=messages,
            max_tokens=max_tokens + thinking_budget,
            temperature=temperature,
        )
        elapsed = round(time.time() - t0, 2)

        content = result["choices"][0]["message"]["content"] or ""
        content = _strip_thinking(content)

        return {
            "id": result.get("id", "chatcmpl-modal"),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": GGUF_FILENAME,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": result["choices"][0].get("finish_reason", "stop"),
                }
            ],
            "usage": result.get("usage", {}),
            "_elapsed_s": elapsed,
            # Pass through any caller metadata so the batch runner can reassemble results
            "_meta": request.get("_meta", {}),
        }

    @modal.method()
    def infer(self, request: dict) -> dict:
        """
        Programmatic inference — used with .map() for parallel batch processing.

        Pass _meta dict in request to tag results (e.g. folder name, chunk index).
        Results are returned in the same order as inputs.
        """
        return self._run_inference(request)

    @modal.fastapi_endpoint(method="POST", docs=True)
    def chat_completions(self, request: dict) -> dict:
        """
        OpenAI-compatible /v1/chat/completions endpoint.
        Compatible with any HTTP client using the OpenAI API format.
        """
        return self._run_inference(request)


def _strip_thinking(text: str) -> str:
    """
    Remove Gemma 4 thinking blocks from output.
    The model wraps reasoning in <think>...</think> tags.
    We strip those and return only the final answer.
    """
    import re
    # Remove <think>...</think> blocks (may span multiple lines)
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return cleaned.strip()


# ── Local test ───────────────────────────────────────────────────────────────
@app.local_entrypoint()
def test():
    """Quick smoke test — run with: modal run modal_app/gemma_server.py"""
    import json
    server = GemmaServer()
    response = server.chat_completions.remote({
        "messages": [
            {"role": "system", "content": "You are a helpful assistant. Reply with valid JSON only."},
            {"role": "user",   "content": 'Return {"status": "ok", "model": "gemma4"} and nothing else.'},
        ],
        "max_tokens": 64,
        "temperature": 0.0,
    })
    print(json.dumps(response, indent=2))
