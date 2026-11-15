# WSL Python dependencies

Environment preparation and final program execution are separate phases. The
host can prepare packages before sealing final public network access. The game
starts only after installation, process cleanup, network sealing, and offline
verification succeed. Preparation remains inside the configured wall deadline.

Declare deployment dependencies in `submission.save`'s `dependencies` argument,
or put the same declaration in the submitted directory's `c4g-program.json`:

```json
{
  "dependencies": {
    "environment": {
      "requirements": ["pyfiglet==1.0.2"],
      "indexes": ["https://pypi.org/simple"]
    }
  }
}
```

Requirements must be exact named versions. Initial support accepts binary Python
wheels, not editable installs, source distributions, VCS references, system APT
packages, or arbitrary setup commands. Indexes must be public HTTPS URLs without
credentials, queries, or fragments. For CPU PyTorch, the official CPU wheel index
can be included explicitly as `https://download.pytorch.org/whl/cpu`.

`submission.save` resolves the transitive wheel set in the baseline's Linux Python
and freezes each wheel's name, version, filename, size, and SHA-256. The resulting
host-generated `environment_lock` participates in the immutable program ID.
Do not supply that field manually. A separate content-addressed cache stores the
artifact bytes once; dependency payloads do not consume the 64 MiB code limit.

## Model and data assets

An environment may include `assets`. Each item contains a relative destination
`path`, SHA-256, and either a public HTTPS `url` or a `source_path` pointing to an
already downloaded regular file inside the development workspace. For example:

```json
{
  "environment": {
    "requirements": ["easyocr==1.7.2"],
    "assets": [
      {
        "path": "models/english_g2.pth",
        "sha256": "e2272681d9d67a04e2dff396b6e95077bc19001f8f6d3593c307b9852e1c29e8",
        "source_path": "/home/c4gprobe/run/REPLACE_WITH_SESSION/models/english_g2.pth"
      }
    ]
  }
}
```

This is a shape example: use the real session path and include every asset the
program needs. Keep large files outside the submitted code directory. The host
captures asset bytes independently; source paths remain in diagnostic evidence
and are removed from the immutable declaration. The frozen lock records stable
relative paths and hashes. Programs find assets under `C4G_ASSET_DIR`.

## Rebuilding and evidence

Each clean final clone builds its own venv. Installation uses the exact frozen
wheels with `--no-index --require-hashes --only-binary=:all:`. Missing public
artifacts may be downloaded during preparation, but must match the existing
frozen filename, version, size, and hash; preparation never silently updates the
lock. Missing locally captured assets must be supplied by their frozen hash.
The installer and descendants are stopped before the canonical final network
policy is verified. Import/requirement verification and submitted code execute
after that seal. Public DNS and direct-IP denial require real acceptance checks,
in addition to rule inspection.

Shared artifact blobs live under the profile root's parent `dependency_artifacts/`.
Per-run host `dependencies/` evidence retains the request, lock, installer logs,
timings, network transitions, and dependency checks, including failures. Managed
venvs and downloads under `.c4g-dependencies` are excluded from workspace TARs;
their hashes and shared artifacts reconstruct them. Arbitrary files outside that
reserved managed directory retain the existing archive behavior. Historical
archives are preserved.

Current bounds are 128 requested requirements, 512 resolved wheels, 256 assets,
2 GiB per artifact, and 4 GiB per environment. These do not extend model, game,
wall-clock, or code-submission budgets. Supporting a package is not evidence of
GPU access, local-LLM exclusion, or complete hostile isolation.
