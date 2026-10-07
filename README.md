# LocalAI Agent for Windows — preview

This is an early, local-first voice/desktop agent. The installer copies the
reviewed application source, shows hardware and dependency checks, and can
store a user-supplied visual API credential in Windows Credential Manager.
It does **not** bundle model weights, Python environments, Hermes, personal
application paths, test recordings, evaluation data, or credentials.

On first launch, the app shows missing prerequisites instead of claiming the
voice hotkey is ready. Complete the setup checklist before using Ctrl+Alt+X.
The current model path requires Windows, an NVIDIA CUDA GPU with at least
10 GiB *free* VRAM before text-model load, local model files, and separate
Python/Hermes runtime environments. Other hardware has not been validated.

Model sources: [Qwen3-8B](https://huggingface.co/Qwen/Qwen3-8B),
[Qwen3-ASR-1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B), and optional
[BGE-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5).
The exact expected folders and revisions are in [the technical guide](docs/TECHNICAL.md).

The visual API is optional for installation. Without it, local OCR may still
locate clear text, but image understanding and visual fallback cannot run;
desktop tasks requiring those capabilities must stop with an explicit error.
Never put a token in a repository or text configuration file.

See [architecture](docs/ARCHITECTURE.md) and [technical details](docs/TECHNICAL.md).
