# LocalAI Agent: reviewed voice and application source snapshot

This is a source-only, Windows-focused snapshot taken from the local project's
committed voice/application baseline. It is stored beside the older installer
preview so the older release remains unchanged.

Included: hotkey and transcript review, registered application lookup and
approved aliases, five-candidate correction, bounded Hermes task entry, local
OCR and optional visual location, and the assets needed by the voice window.
The source manifest records the exact origin commit and hashes of every copied
file. Configuration included here is a template; users must provide their own
model files, runtimes, application registry, and optional visual API credential.

The voice and application path has passed bounded local checks and selected
real-machine trials. These results do not establish reliable execution for
arbitrary multi-application instructions, every desktop UI, or noisy speech.
The optional visual API fallback, fresh-PC installation, and alternate hardware
have not been validated for this snapshot. The repository's earlier installer
preview is not rebuilt or re-certified by adding this source snapshot.

This folder excludes working-tree changes, personal executable inventories,
models, runtime data, recordings, credentials, test fixtures, and evaluation
reports. The separate Folia/music work is still being integrated and is not
represented here. Do not treat this directory as a self-contained installer.

To inspect the source, start with `services/voice_hotkey.py`,
`services/task_entry.py`, `services/application_lookup.py`, and
`services/application_registry.py`. The launch scripts are under `scripts/`.
For integrity, compare files with `SOURCE-MANIFEST.json`.
