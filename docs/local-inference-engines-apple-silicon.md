# Local inference on Apple Silicon: a practical engine stack

**Audience:** one person using a MacBook Pro M2 Pro (32 GB unified memory, 1 TB SSD) and a MacBook Pro M5 Pro (48 GB unified memory, 1 TB SSD), currently using Splash and wanting flexible local text, image, and limited video generation.  
**Research date:** 2026-10-29. **Source policy:** first-party documentation, official project repositories, and vendor documentation only. Links immediately following a claim are its source.

## Decision in brief

1. **Use LM Studio as the default text UI on both Macs.** It is the simplest way to download, chat with, and serve local models; on Apple Silicon it can run either GGUF through llama.cpp or MLX models, and it offers OpenAI-like local endpoints. [LM Studio documentation](https://lmstudio.ai/docs/app)
2. **Use Ollama when an agent, terminal tool, or application needs a stable localhost API.** It has a small CLI, a local HTTP server, documented OpenAI/Anthropic compatibility, and model lifecycle controls. [Ollama Quickstart](https://docs.ollama.com/quickstart.md) · [Ollama FAQ](https://docs.ollama.com/faq.md)
3. **Keep Splash on the M5 Pro for the model families it explicitly supports and for coding-agent throughput, rather than treating it as the general model runtime.** Splash is a specialized Apple-Silicon engine with a supported-model matrix; its desktop app requires M3-or-newer hardware and macOS 26.4+, so it is not the common solution for the M2 Pro. [Splash README: requirements, API, and model support](https://github.com/incoai/splash)
4. **Use Draw Things first for a simple, native image/video UI; add ComfyUI only for reusable/advanced workflows.** Draw Things runs locally and offline on Mac, exposes image-to-video and local LoRA training in its product UI; Comfy Desktop supports Apple Silicon and is the flexible node-graph alternative. [Draw Things](https://drawthings.ai/) · [Comfy Desktop for macOS](https://docs.comfy.org/installation/desktop/macos)
5. **Do not install every server.** Run one text-model server at a time and exit image/video tools before loading a large LLM. Apple Silicon uses unified memory, so the CPU, GPU, and other apps compete for the same pool. [MLX unified-memory documentation](https://ml-explore.github.io/mlx/build/html/index.html)

## Terms: do not compare unlike things

| Layer | What it is | Candidates here | Practical consequence |
| --- | --- | --- | --- |
| **Inference engine/runtime** | Loads weights and performs token/image/video inference. | `llama.cpp`, MLX/MLX-LM, Ollama, Splash, vLLM Metal, Core ML/MLX diffusion runtimes. [llama.cpp README](https://github.com/ggml-org/llama.cpp) · [MLX](https://ml-explore.github.io/mlx/build/html/index.html) | This is what determines model formats, Metal use, memory behavior, and API performance. |
| **Text desktop app / model manager** | UI that bundles or drives a text runtime. | LM Studio, Jan. LM Studio drives llama.cpp GGUF and, on Apple Silicon, MLX; Jan bundles a local-model UI and OpenAI-compatible localhost server. [LM Studio](https://lmstudio.ai/docs/app) · [Jan README](https://github.com/janhq/jan) | Choose one for chat/download convenience; it is not a second model engine to run alongside Ollama. |
| **Web UI / orchestrator** | Connects to an engine/API and adds chats, RAG, accounts, or tools. | Open WebUI, LocalAI. Open WebUI explicitly has no models of its own. [Open WebUI Quick Start](https://docs.openwebui.com/getting-started/quick-start/) · [LocalAI Quickstart](https://localai.io/docs/basics/getting_started/) | Useful when a browser UI or many modalities behind one API matters; optional for a single user. |
| **Creative-generation application** | A workflow/UI plus one or more diffusion/video backends. | Draw Things, ComfyUI, Invoke. [Draw Things](https://drawthings.ai/) · [Comfy Desktop](https://docs.comfy.org/installation/desktop/macos) · [InvokeAI README](https://github.com/invoke-ai/InvokeAI) | This is the appropriate layer for image/video, not Ollama or LM Studio. |

## Recommended stack

### A. Minimum, low-friction stack — recommended starting point

| Need | M2 Pro 32 GB | M5 Pro 48 GB | Why |
| --- | --- | --- | --- |
| Friendly text chat, experimenting with models | **LM Studio** | **LM Studio** | One desktop app can download/manage models, chat, expose local APIs, and select GGUF/llama.cpp or MLX on Apple Silicon. [LM Studio](https://lmstudio.ai/docs/app) |
| Text API for terminal agents/apps | **Ollama** *or* LM Studio’s local server; choose one server | **Ollama** *or* Splash for Splash-supported workloads; choose one server | Ollama provides local API examples and local model operation; Splash provides OpenAI and Anthropic compatible APIs for its supported model set. [Ollama Quickstart](https://docs.ollama.com/quickstart.md) · [Splash API](https://github.com/incoai/splash) |
| Fast coding model | LM Studio or Ollama with a 7–14B quantized instruct/coding model | **Splash** if its supported families fit the task; otherwise LM Studio/Ollama | Splash’s own design is model-family-specific; use it where it is strong, not as the only model catalog. [Splash models](https://github.com/incoai/splash) |
| Simple local image and short image-to-video clips | **Draw Things** | **Draw Things** | Native Mac app; its official feature list includes local/offline image generation, image-to-video, and local LoRA training. [Draw Things](https://drawthings.ai/) |
| Advanced image/video workflows | **Comfy Desktop** only when needed | **Comfy Desktop** only when needed | It is a graph of composable nodes; this flexibility costs learning and model-management complexity. [Comfy nodes](https://docs.comfy.org/development/core-concepts/nodes) |

**Concrete setup order**

1. Install **LM Studio** from its official download page, download one 7–14B 4-bit instruct model, and verify a local chat first. LM Studio supports Apple Silicon, model downloads, an offline mode, local chat/RAG, and OpenAI-like endpoints. [LM Studio documentation](https://lmstudio.ai/docs/app)
2. Install **Draw Things** from the Mac App Store and begin with its in-app recommended model/settings; use it for the image/video requirement before investing in node workflows. It is available for Mac and advertises local/offline generation. [Draw Things](https://drawthings.ai/)
3. Add **Ollama** only if a terminal/client wants `localhost:11434`, or expose LM Studio’s server instead. Avoid duplicate background servers and duplicate copies of model weights. Ollama’s macOS app and `ollama run` workflow are documented officially. [Ollama Quickstart](https://docs.ollama.com/quickstart.md)
4. On the M5 Pro, retain **Splash** for its supported coding models if the machine meets Splash’s M3+/macOS requirement; otherwise use the general stack above. [Splash Quick Start](https://github.com/incoai/splash)
5. Add **Comfy Desktop** only after Draw Things cannot express a workflow (for example, a reproducible node graph, a specialist custom node, or batch pipeline). Comfy Desktop supports Apple Silicon M1+ on macOS 13+. [Comfy Desktop macOS requirements](https://docs.comfy.org/installation/desktop/macos)

### B. One-server, many-modality alternative — only if the all-in-one API matters

Use **LocalAI** instead of the separate Ollama + image/audio endpoints when a single OpenAI/Anthropic-compatible API, a built-in Web UI, agents, and many backends are more valuable than the simplest setup. Its official documentation describes a local REST replacement with a built-in UI and support for LLMs, image, audio, video, and multiple backends; the project also supplies a macOS DMG. [LocalAI Quickstart](https://localai.io/docs/basics/getting_started/) · [LocalAI README](https://github.com/mudler/LocalAI)

This is **not** the default recommendation for these two laptops: it adds a broader orchestration surface and backend management. Start with LM Studio/Ollama + Draw Things, and adopt LocalAI only when one unified API or its multimodal/agent features removes real integration work. This is a recommendation based on the projects’ stated scopes, not a claim that one is universally faster. [LocalAI README](https://github.com/mudler/LocalAI) · [LM Studio](https://lmstudio.ai/docs/app)

## Text LLM engines and apps

### Best fits

| Tool | Classification | Use it for | Install / first run | Caveats |
| --- | --- | --- | --- | --- |
| **LM Studio** | Desktop UI, model manager, local server; uses llama.cpp GGUF and MLX on Apple Silicon. [Source](https://lmstudio.ai/docs/app) | **Default simple UI**; compare GGUF vs MLX model builds; local chat/doc Q&A; optionally serve apps. | Download the macOS installer; in the app download a model, load it in Chat, then start a local server only if another app needs it. [Source](https://lmstudio.ai/docs/app) | It is a full desktop app, not the smallest headless runtime. Do not also run an equivalent Ollama model merely to obtain a chat UI. |
| **Ollama** | Local model runner/server and CLI, not a full creative UI. [Source](https://docs.ollama.com/quickstart.md) | Terminal workflows, coding agents, scripts, and a predictable local API. | Install the macOS app, then `ollama run <model>`; call local `/api/chat` or OpenAI/Anthropic compatibility endpoints as needed. [Source](https://docs.ollama.com/quickstart.md) | Defaults to 4,096-token context; larger context costs more memory. It retains models for five minutes by default and `ollama stop <model>` frees memory. [Source](https://docs.ollama.com/faq.md) |
| **llama.cpp** | Low-level C/C++ GGUF runtime and CLI/server. [Source](https://github.com/ggml-org/llama.cpp) | Maximum direct control, testing a GGUF, or embedding a native local runtime. | `curl -LsSf https://llama.app/install.sh \| sh`; then `llama cli -hf <GGUF repo>` or `llama serve -hf <GGUF repo>`. [Source](https://github.com/ggml-org/llama.cpp) | Terminal-first. It is optimized for Apple Silicon via NEON, Accelerate, and Metal and supports integer quantization; use LM Studio/Ollama unless that control is needed. [Source](https://github.com/ggml-org/llama.cpp) |
| **MLX + MLX-LM** | Apple’s array framework plus a Python/CLI LLM package. [Source](https://ml-explore.github.io/mlx/build/html/index.html) · [Source](https://github.com/ml-explore/mlx-lm) | Scripts, experiments, quantization/fine-tuning, and MLX-native model use. | `pip install mlx-lm`; use `mlx_lm.chat` or `mlx_lm.generate --model <MLX model> --prompt ...`. [Source](https://github.com/ml-explore/mlx-lm) | Developer/terminal-oriented. MLX-LM warns that models large relative to RAM can be slow and documents macOS 15+ wired-memory behavior. [Source](https://github.com/ml-explore/mlx-lm) |
| **Splash** | Specialized Apple-Silicon local inference server with built-in chat; not a broad arbitrary-model catalog. [Source](https://github.com/incoai/splash) | M5 Pro coding agents and the explicitly supported Splash model families. | On eligible machines, install the app or `brew install incoai/tap/splash`; run `splash serve --model <supported model>`. [Source](https://github.com/incoai/splash) | Desktop app/CLI require M3-or-newer and macOS 26.4+; its supported-model table should be checked before selecting it. [Source](https://github.com/incoai/splash) |
| **Jan** | Alternative local desktop chat/UI with a localhost OpenAI-compatible API. [Source](https://github.com/janhq/jan) | A simple open-source desktop alternative to LM Studio. | Download the universal macOS DMG from Jan’s official release path. [Source](https://github.com/janhq/jan) | It overlaps heavily with LM Studio. Pick **Jan or LM Studio**, not both, unless deliberately comparing them. Jan lists 32 GB as its guideline for 13B models. [Source](https://github.com/janhq/jan) |

### Useful, but not a first install

| Tool | Classification and relevance | Recommendation |
| --- | --- | --- |
| **Apple Foundation Models** | Apple’s Foundation Models framework provides app developers access to Apple’s on-device Apple Intelligence language model for text generation, structured output, tool calling, and text/image understanding; it requires macOS 26+ and an Apple-Intelligence-capable device. [Apple documentation](https://developer.apple.com/documentation/FoundationModels) | **Use when building a Swift app targeting Apple’s system model**, not as a replacement for downloadable open-weight model experimentation. It does not provide a general model picker, GGUF/MLX catalogue, or image/video generator. |
| **vLLM (native macOS CPU backend)** | Officially experimental on Apple Silicon, source-build-only, and currently CPU-only; the docs point GPU-accelerated Apple Silicon users to vLLM Metal. [vLLM CPU installation](https://docs.vllm.ai/en/latest/getting_started/installation/cpu/) | **Do not choose** as the normal laptop runtime. Its native path is a source build without Metal acceleration. |
| **vLLM Metal** | Community-maintained vLLM hardware plugin that uses MLX as its primary compute backend; requires Apple Silicon and macOS 15+. [vLLM Metal README](https://github.com/vllm-project/vllm-metal) | **Promising advanced server option**, especially when continuous batching/paged serving is the goal. Install through its Homebrew tap only after LM Studio/Ollama is insufficient. It is not the simple-UI choice. |
| **Open WebUI** | Browser UI/orchestrator; it has no models of its own and connects to Ollama or OpenAI-compatible servers. [Open WebUI Quick Start](https://docs.openwebui.com/getting-started/quick-start/) | Add it only for browser-based multi-model chat, local RAG, sharing, or accounts. On a laptop, run the text engine natively and point the container/UI at it; do **not** expect macOS Docker GPU passthrough for Ollama. [Ollama FAQ](https://docs.ollama.com/faq.md) |
| **LocalAI** | Multi-backend local AI engine/orchestrator with LLM, vision, voice, image, and video claims, APIs, UI, and a macOS app. [LocalAI README](https://github.com/mudler/LocalAI) | A valid consolidated alternative, but more operationally broad than a single-user stack. Prefer it only when its API/backend breadth is a requirement. |

## Image and video tools

| Tool | Classification | Best use on these Macs | Install / setup | Caveat |
| --- | --- | --- | --- | --- |
| **Draw Things** | Native creative-generation application and inference stack. [Source](https://drawthings.ai/) | **First choice for simple image generation and short image-to-video.** Its official site says it runs locally/offline on Mac and lists image-to-video, infinite canvas, and local LoRA training. [Source](https://drawthings.ai/) | Install from the Mac App Store; download a model inside the app and start with its recommended settings. [Source](https://drawthings.ai/) | It is an application rather than a general, documented OpenAI-style inference service. Treat long/high-resolution video as a high-memory, long-running workload. |
| **ComfyUI / Comfy Desktop** | Node-graph workflow application. Nodes are composable modules connected into a workflow; custom nodes expand its capabilities. [Source](https://docs.comfy.org/development/core-concepts/nodes) | **Second choice for control, repeatable graphs, and specialist model workflows** after Draw Things. | Download the macOS DMG, drag it to Applications, then create an instance; requirements are Apple Silicon M1+ and macOS 13+. [Source](https://docs.comfy.org/installation/desktop/macos) | More flexible means more complexity: workflow imports can require unavailable custom nodes and model files. [Source](https://docs.comfy.org/development/core-concepts/nodes) |
| **Invoke** | Locally hosted creative engine with a web UI, canvas, and workflows. [Source](https://github.com/invoke-ai/InvokeAI) | A **more conventional canvas/web-UI alternative** if Draw Things feels too app-centric and Comfy graphs feel too technical. | Install the official Launcher from the macOS ARM64 DMG, then install/manage Invoke through the Launcher. [Official installation source](https://github.com/invoke-ai/InvokeAI/blob/main/docs/src/content/docs/start-here/installation.mdx) | Consider it an alternative creative suite, not a runtime to combine with ComfyUI for the same task. Keep only the models/workflows you actually use. |
| **DiffusionKit** | Developer library for Core ML/MLX image diffusion in Python/Swift. [Source](https://github.com/argmaxinc/DiffusionKit) | Prototype or integrate image inference into code. | Clone/setup its Python environment or use the Swift package as documented. [Source](https://github.com/argmaxinc/DiffusionKit) | **Not recommended for a new production workflow:** its official repository is archived/read-only as of 2026-03-21. [Source](https://github.com/argmaxinc/DiffusionKit) |
| **Apple Core ML Stable Diffusion** | Apple research reference package for converting PyTorch weights to Core ML and running Stable Diffusion from Python or Swift. [Source](https://github.com/apple-aiml-research/ml-stable-diffusion) | Build/integrate a custom Core ML image app. | Follow the repository’s conversion and Python/Swift instructions. [Source](https://github.com/apple-aiml-research/ml-stable-diffusion) | A developer reference, not a simple end-user UI. Use Draw Things/Comfy/Invoke unless building software. |

### Video reality check

The requested Macs can make **short local clips**, and Draw Things advertises image-to-video locally; LocalAI and Comfy workflows can expose broader video pipelines. [Draw Things](https://drawthings.ai/) · [LocalAI README](https://github.com/mudler/LocalAI)  
However, do not plan around desktop-class local video as an interactive replacement for a powerful discrete GPU or cloud service: video generation has substantially more temporal state and inference work than a still image. This is a capacity-planning judgment rather than a published benchmark. Start with short, low-resolution image-to-video; close the LLM server first; only then trial longer/higher-resolution models. The shared-memory constraint underlying that recommendation is documented by MLX. [MLX unified-memory documentation](https://ml-explore.github.io/mlx/build/html/index.html)

## Memory and model-size guidance

### How to reason about it

Apple Silicon’s unified memory is shared between CPU and GPU operations in MLX, so the advertised 32/48 GB is **not** a model-only budget. [MLX unified-memory documentation](https://ml-explore.github.io/mlx/build/html/index.html)  
For a rough lower bound, 4-bit weights require `parameter_count × 0.5 bytes`: 7B ≈ 3.5 GB, 14B ≈ 7 GB, 27B ≈ 13.5 GB, 32B ≈ 16 GB, and 70B ≈ 35 GB before quantization metadata, runtime buffers, vision components, and KV cache. This is arithmetic, not a promise of fit or speed. Actual context memory rises with context length; for example, Ollama documents that larger context needs more memory and that parallel requests multiply context allocation. [Ollama Quickstart](https://docs.ollama.com/quickstart.md) · [Ollama FAQ](https://docs.ollama.com/faq.md)

| Machine | Everyday target | Occasional trial | Avoid as a normal interactive workload | Operational rule |
| --- | --- | --- | --- | --- |
| **M2 Pro, 32 GB** | **3–8B** for fast chat/utility; **7–14B 4-bit** for the best balance of quality, context, and headroom. Jan’s published minimums likewise map 16 GB to 7B and 32 GB to 13B. [Source](https://github.com/janhq/jan) | **20–27B at 4-bit**, single user, modest context, most other apps closed. Test rather than assume it will remain responsive. | 32B+ long-context agents; simultaneous diffusion/video plus a large LLM; multiple loaded LLMs. | Reserve a practical safety margin for macOS/browser/editor and unload the model when switching modality. Ollama provides `ollama stop`; Splash has `--max-memory`; MLX-LM warns very-RAM-large models may be slow. [Ollama FAQ](https://docs.ollama.com/faq.md) · [Splash settings](https://github.com/incoai/splash) · [MLX-LM](https://github.com/ml-explore/mlx-lm) |
| **M5 Pro, 48 GB** | **7–14B** for speed; **14–32B 4-bit** as the practical high-quality range with usable headroom. | A 27–35B class quantized model, a larger context, or Splash’s supported 27/35B families, one main workload at a time. Splash publishes M5 Pro 48 GB results for selected models, but those are not general guarantees for arbitrary weights. [Source](https://github.com/incoai/splash) | 70B-class weights as a daily interactive model, especially with long context, agents, or creative tools concurrently. | Treat the additional 16 GB as more context/headroom and somewhat larger models—not permission to run all services concurrently. Monitor memory and choose a smaller quantization/context first. [MLX unified-memory documentation](https://ml-explore.github.io/mlx/build/html/index.html) |

### Context and concurrency rules

* Begin at **4K–8K context** on the M2 and **8K–16K** on the M5, then increase only after checking responsiveness. Ollama defaults to 4,096 tokens and documents `OLLAMA_CONTEXT_LENGTH`/`num_ctx`. [Ollama FAQ](https://docs.ollama.com/faq.md)
* One user request per loaded large model is the default sensible setting. Ollama says that parallel requests increase required context memory by `OLLAMA_NUM_PARALLEL × OLLAMA_CONTEXT_LENGTH`. [Ollama FAQ](https://docs.ollama.com/faq.md)
* Prefer 4-bit weights for capacity; evaluate a 5–8-bit variant only after the model and context are known to fit. llama.cpp supports 1.5–8-bit integer quantization specifically to reduce memory use. [llama.cpp README](https://github.com/ggml-org/llama.cpp)
* A very long context can be a worse trade than a better base model. MLX-LM offers bounded/rotating KV cache and configurable prefill steps precisely to reduce memory pressure, at a quality or prefill-speed trade-off. [MLX-LM long prompts](https://github.com/ml-explore/mlx-lm)

### Storage guidance for the 1 TB SSD

Do not let every app download the same model family in GGUF, MLX, Ollama, and app-specific formats. A 4-bit 32B model alone has a ~16 GB weight-only lower bound; image/video checkpoints, output caches, and duplicate formats add up quickly. This follows the sizing arithmetic above and the fact that Ollama stores models under `~/.ollama/models` by default, while Comfy Desktop has separate shared model/output directories. [Ollama FAQ](https://docs.ollama.com/faq.md) · [Comfy Desktop storage locations](https://docs.comfy.org/installation/desktop/macos)

Suggested 1 TB allocation policy: reserve **250–350 GB free** for macOS, swaps, project work, temporary conversion files, and media outputs; budget the remaining AI storage deliberately; keep one preferred text format per model; delete failed experiments and duplicate checkpoints after verification. This is operational guidance, not a vendor capacity requirement.

## Setup recipes

### Recipe 1 — simple text, no always-on server

1. Install LM Studio and download a 7–14B 4-bit model in the format it recommends for the chosen runtime. LM Studio supports downloading models within the app and running both GGUF/llama.cpp and MLX on Apple Silicon. [LM Studio](https://lmstudio.ai/docs/app)
2. Use Chat directly. Do **not** start Ollama unless an external tool requires an API.
3. If an app needs an API, start LM Studio’s local server and point the app to its OpenAI-like endpoint. [LM Studio](https://lmstudio.ai/docs/app)

### Recipe 2 — terminal/agent API with Ollama

```bash
# Install the official macOS app first, then:
ollama run <model>

# Inspect loaded-model processor/memory placement:
ollama ps

# Release the model before image/video generation:
ollama stop <model>
```

The documented local API is at `http://localhost:11434`; Ollama also documents OpenAI and Anthropic-compatible local endpoints. [Ollama Quickstart](https://docs.ollama.com/quickstart.md)  
Keep it bound to loopback unless there is a specific LAN requirement: Ollama defaults to `127.0.0.1:11434`, and exposing it requires deliberate host/authentication handling. [Ollama FAQ](https://docs.ollama.com/faq.md)

### Recipe 3 — M5 Pro coding performance while retaining flexibility

```bash
# Eligible M3+ / macOS 26.4+ system only
brew install incoai/tap/splash
splash serve --model unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M
```

Use this only after checking Splash’s current supported-model table and set `--max-memory` to leave space for the desktop. Splash documents this server flow, its localhost API, and its memory cap option. [Splash README](https://github.com/incoai/splash)  
For a non-Splash model family, stop Splash and load it with LM Studio/Ollama/llama.cpp instead; this keeps the system flexible without claiming a format is supported by Splash when it is not.

### Recipe 4 — local images and short clips

1. Install Draw Things from the Mac App Store and download one image model within the app. It is the least technical local/offline path. [Draw Things](https://drawthings.ai/)
2. For a first video experiment, start with Draw Things’ **image-to-video** feature and keep the clip/resolution modest. [Draw Things](https://drawthings.ai/)
3. If a desired workflow is unavailable, install Comfy Desktop and import/build its node workflow; keep the project’s model/checkpoint folders on the SSD and avoid duplicate model trees. Comfy Desktop can manage multiple local instances and shared model storage. [Comfy Desktop macOS](https://docs.comfy.org/installation/desktop/macos)

## Caveats and security

* **“Local” needs verification.** Ollama distinguishes local and cloud models; its cloud features can be disabled with `OLLAMA_NO_CLOUD=1` or `disable_ollama_cloud`. [Ollama FAQ](https://docs.ollama.com/faq.md)
* **A localhost model API is still an execution boundary.** Ollama binds locally by default; changing `OLLAMA_HOST` exposes it more broadly. Do not put an unauthenticated inference server on a LAN or internet-facing interface. [Ollama FAQ](https://docs.ollama.com/faq.md)
* **Do not trust arbitrary model code.** MLX-LM notes that some tokenizers require `trust_remote_code`; inspect the source/author before accepting it. [MLX-LM supported-model guidance](https://github.com/ml-explore/mlx-lm)
* **Custom Comfy nodes are code.** Missing custom nodes are common in imported graphs, and Comfy’s documentation points users to node-management/install paths; install only trusted extensions and pin a working workflow environment. [Comfy nodes](https://docs.comfy.org/development/core-concepts/nodes)
* **Docker on macOS changes the trade-off.** Use Docker for a UI/orchestrator such as Open WebUI, but use native text runtimes for Metal acceleration; Ollama documents that Docker Desktop on macOS does not provide GPU acceleration because GPU passthrough is unavailable. [Ollama FAQ](https://docs.ollama.com/faq.md)

## Primary-source reference index

* [Splash official repository and documentation](https://github.com/incoai/splash)
* [Ollama Quickstart](https://docs.ollama.com/quickstart.md) and [FAQ](https://docs.ollama.com/faq.md)
* [LM Studio documentation](https://lmstudio.ai/docs/app)
* [llama.cpp official repository](https://github.com/ggml-org/llama.cpp)
* [Apple MLX documentation](https://ml-explore.github.io/mlx/build/html/index.html) and [MLX-LM](https://github.com/ml-explore/mlx-lm)
* [Apple Foundation Models documentation](https://developer.apple.com/documentation/FoundationModels)
* [Draw Things official site](https://drawthings.ai/)
* [Comfy Desktop for macOS](https://docs.comfy.org/installation/desktop/macos) and [Comfy nodes](https://docs.comfy.org/development/core-concepts/nodes)
* [Invoke official repository](https://github.com/invoke-ai/InvokeAI)
* [DiffusionKit official repository](https://github.com/argmaxinc/DiffusionKit)
* [Apple Core ML Stable Diffusion reference](https://github.com/apple-aiml-research/ml-stable-diffusion)
* [Jan official repository](https://github.com/janhq/jan)
* [Open WebUI Quick Start](https://docs.openwebui.com/getting-started/quick-start/)
* [LocalAI Quickstart](https://localai.io/docs/basics/getting_started/) and [official repository](https://github.com/mudler/LocalAI)
* [vLLM CPU installation](https://docs.vllm.ai/en/latest/getting_started/installation/cpu/) and [vLLM Metal official project](https://github.com/vllm-project/vllm-metal)
