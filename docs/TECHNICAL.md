# LocalAI Agent technical guide — Windows preview

## Scope and current status

The current local prototype can record with Ctrl+Alt+X, transcribe with a
local ASR model, parse an application request, call a local Qwen model through
Hermes, and open approved Windows applications. Desktop navigation uses local
OCR first and the configured visual API when OCR cannot identify the target.
Individual Steam and Toolbox experiments are evidence of those scenarios,
not a portability or reliability guarantee.

The release installer is a **source-and-diagnostics preview**. It gives a
double-clickable installer, selectable per-user destination, an installed app
entry, hardware/dependency status, and optional credential setup. It does not
yet bootstrap the separate Python environments, Hermes runtime, CUDA/Torch,
model weights, or local application registry on an unfamiliar PC. It must not
report the hotkey as ready until these are present. A second-machine clean
installation and full voice-to-desktop run remain unverified.

## Runtime and dependency inventory

| Part | Current implementation / source | Required for |
| --- | --- | --- |
| Windows | Win64, PowerShell, Credential Manager, desktop session | Installer, hotkey and input |
| NVIDIA driver/CUDA GPU | Local Torch CUDA execution; text load checks 10 GiB free VRAM | Qwen text model |
| Python | Project environments use Python 3.11; `pyproject.toml`/`uv.lock` pin only foundation MCP | Services and model runtimes |
| Qwen text | `Qwen/Qwen3-8B` revision `b968826d9c46dd6066d109eabc6255188de91218`; local NF4 load | Text reasoning |
| ASR | `Qwen/Qwen3-ASR-1.7B` revision `7278e1e70fe206f11671096ffdd38061171dd6e5` | Speech transcription |
| Retrieval | `BAAI/bge-small-zh-v1.5` revision `7999e1d3359715c523056ef9478215996d62a620`, CPU + FAISS | Optional semantic candidates |
| Hermes | Separate local CLI environment, local tool routing | Model tool calls |
| MCP Python SDK | `mcp>=1.20,<2` in foundation project | Desktop/service protocol |
| Voice capture | `sounddevice`, Tkinter; extra ASR environment | Hotkey recording |
| Local vision/OCR | Project OCR environment and models | Clear-text desktop location |
| Visual API | ModelScope API-Inference `Qwen/Qwen3.8-Flash-Next`, token from Credential Manager | Image understanding fallback |

Download pages for the pinned public models are
[Qwen3-8B](https://huggingface.co/Qwen/Qwen3-8B),
[Qwen3-ASR-1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B), and
[BGE-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5).
Model licenses and hardware requirements should be checked at the source
before distribution. The repository never contains their weights.

Expected local files include `models/model-qwen3-8b/source/config.json`,
`models/asr-comparison/qwen3-asr-1.7b/source/config.json`,
`runtime/asr/Scripts/pythonw.exe`, `runtime/fast/Scripts/python.exe`, and
`runtime/hermes-home/bin/hermes.cmd`. Other project environments may also be
required. Installing just the source does not satisfy these checks.

## API and privacy

The visual fallback sends a bounded screenshot and prompt to the configured
ModelScope inference API. It requires the user's own token. The installer can
store that token in Windows Credential Manager and never includes it in the
release archive. Skipping the token keeps installation possible and local OCR
usable; screenshot understanding, image-only target location, and tasks that
depend on those results must report unavailable rather than guess. Other
users' tokens must never be copied from this machine.

Read-only web name resolution is attempted only after local approved alias,
FTS5, and optional BGE matching fail. A web page cannot authorize a new
executable or path; it can only point to an already approved local app.

## Packaging and repository boundary

`installer/build_payload.py` uses a strict file allowlist. It includes runtime
source modules, selected launch scripts, approved alias knowledge, the icon,
this guide, the architecture diagram, and a token-free visual config template.
It excludes evaluation scripts and reports, tests, recordings, screenshots,
runtime environments, caches, model weights, local app inventories, logs,
credentials, and the local application database. Public release history must
be created from this clean export, not by pushing the development Git history.

## Operational boundaries and known gaps

- Each interactive model, recording, or desktop step has a 180-second limit;
  a timed-out route stops rather than silently changing tools.
- Free VRAM at or below 200 MiB pauses the test route. The text model itself
  checks for 10 GiB free before loading.
- Exact approved aliases can still be wrong if ASR transcribes speech as a
  different approved app. Cross-app misrecognition protection is incomplete.
- Web-discovered names are not automatically written back to the alias file.
- The first installer does not automate model/runtime download and setup.
  Those pages and missing components are surfaced in its diagnostics.
- No automated uninstall, residual-file cleanup, or registry deletion is
  included. Those require a separate design and explicit confirmations.
