# dev-env

A personal, safe-by-default installer for project-local OpenCode configuration and Python development tooling.

## Requirements

- Python 3.12+
- Git
- `uv` to install the CLI as a global tool

The CLI itself uses only the Python standard library.

## Install

From a local checkout:

```bash
uv tool install --from ./dev-env-cli dev-env
```

Or directly from your CLI repository:

```bash
uv tool install git+https://github.com/USERNAME/dev-env.git
```

## Configure the default template

Local template checkout:

```bash
dev-env config set-template "$HOME/projects/python-dev-environment-template"
```

Git repository URL:

```bash
dev-env config set-template "https://github.com/mylendad/python-dev-environment-template.git"
```

A Git template is shallow-cloned for each `init`/`update`. To select a branch or tag, pass `--ref`.

## Initialize a project

Run from any directory inside a Git repository:

```bash
cd /path/to/project
dev-env init
```

The command:

1. Copies missing files from the configured template.
2. Adds personal configuration paths to the repository-local `.git/info/exclude`; it does not modify `.gitignore`.
3. Detects existing Python virtual environments.
4. Creates a `mise.toml` with missing tool selectors and common tasks, while preserving existing Mise settings and tasks.
5. Creates `ruff.toml` and `pyrightconfig.json` only when the project does not already configure Ruff or Pyright.
6. Detects globally available commands and Mise-managed binaries before offering any installation.

Useful commands:

```bash
dev-env init --yes       # explicitly approve Mise bootstrap and missing-tool installation
dev-env update           # update managed template files; does not install tools
dev-env doctor           # inspect files, versions, configs and environment
dev-env doctor --live-mcp # also try live OpenCode MCP connections
dev-env diff             # compare managed template files with the source template
dev-env config show
```

`--yes` is intentionally explicit: on Linux/macOS it may run the official Mise installer from `https://mise.run`, then install the missing project-selected tools. Without `--yes`, the CLI asks before each installation step. In a non-interactive terminal, it does not install anything without explicit `--yes`. On Windows, install Mise separately (for example, `winget install jdx.mise`) and rerun `dev-env init`.

## Python tooling

The generated `mise.toml` manages these tools:

| Tool | Purpose |
|---|---|
| Python | Project Python version, inferred from existing Mise config, `.python-version`, `requires-python`, a project venv, or the available Python |
| uv | Virtual environments, lockfiles and project dependencies |
| Ruff | Linting and formatting (Mise PyPI backend) |
| Node.js | Runtime required by the Mise npm backend for Pyright |
| Pyright | Static type checking and the Pyright language server |

The initial selectors are Python `3.12` when no project/system version can be inferred, latest uv/Ruff/Pyright, and Node.js `24`. Existing `mise.toml` selectors are preserved. If an installed global command already satisfies the project's selector, the CLI reuses it rather than reinstalling it. For a selector of `latest`, reuse-first behavior means any existing executable is accepted; the CLI does not query the registry or upgrade it. A missing executable is installed at the newest version resolved by Mise. It also asks Mise to resolve the project-selected executable with `mise which`; this avoids treating an uninstalled Mise shim as an installed tool. Mise backend dependencies are handled separately: installing a missing `pypi:` tool also installs the configured Mise-managed Python first, and installing a missing `npm:` tool also installs the configured Mise-managed Node.js first. This is required even when a compatible system Python or Node.js is available, because those backends depend on Mise-managed runtimes.

Ruff and Pyright config files are defaults only. If Ruff is already configured in `ruff.toml`, `.ruff.toml` or `[tool.ruff]`, or Pyright is configured in `pyrightconfig.json` or `[tool.pyright]`, those settings are left untouched.

### Existing virtual environments

The detector checks `UV_PROJECT_ENVIRONMENT`, project-local `.venv/`, `venv/`, `env/`, `virtualenv/`, `ENV/`, and active `VIRTUAL_ENV`/`CONDA_PREFIX`. A project-local environment takes precedence over an unrelated environment inherited from the shell. `dev-env init` only detects environments; it does not create, delete or recreate one.

For a requirements-only project, `mise run install` installs `requirements.txt` into an existing project-local environment. If none exists, it creates `.venv` first and installs into that explicit interpreter. For a uv project (`[project]` in `pyproject.toml` or an existing `uv.lock`), the task runs `uv sync`; when a lockfile already exists it runs `uv sync --locked`. That task can create `.venv` and synchronize dependencies, so run it deliberately.

For uv projects, Mise uses `python.uv_venv_auto = "source"`: it activates an existing uv environment but does not create one automatically. For non-uv projects, the generated `_.python.venv` directive also sets `create = false`. The `install` task is the explicit place that creates a missing environment.

### Mise tasks

After initialization and any required tool installation:

```bash
mise install          # install tools declared in mise.toml
mise run install      # create/reuse the venv and install/sync project dependencies
mise run lint         # ruff check .
mise run format       # ruff format .
mise run typecheck    # pyright
mise run test         # pytest
mise run check        # lint + typecheck + tests
```

`dev-env init` may generate `mise.toml` even when Mise is not installed. Install Mise, then run `mise install` or repeat `dev-env init` to install missing tools. When `dev-env` installs tools successfully, it creates/updates `mise.lock`; commit `mise.toml` and `mise.lock` if the project should reproduce the same tool versions on another machine.

## Detection and doctor

`dev-env doctor` checks:

- Mise, Python, uv, Ruff, Node.js, Pyright and `pyright-langserver` availability and versions;
- the project-selected Mise executable via read-only `mise which` where available;
- `mise.toml`, expected tool selectors and common tasks;
- the detected virtual environment;
- `pyproject.toml`, Ruff TOML configuration and Pyright JSON configuration;
- project and global OpenCode configuration, configured LSP/MCP commands, manifest and Git exclude rules.

It does not run linting, type checking, tests, or a full MCP session. `doctor --live-mcp` additionally runs `opencode mcp list` from the project directory. That can start local MCP processes or make network requests, so it is opt-in. A static configuration check does not guarantee that an MCP server can connect.

## OpenCode configuration discovery

On Linux and other POSIX systems, global config is read from `$XDG_CONFIG_HOME/opencode/opencode.json` or `~/.config/opencode/opencode.json`; `opencode.jsonc` is also supported. On Windows, `%APPDATA%/opencode/` is checked. If `OPENCODE_CONFIG` is set, that custom config is inspected separately.

The detector understands legacy OpenCode MCP entries directly under `mcp` and the newer `mcp.servers` layout. It does not modify global OpenCode files.

## Template metadata

Place `.dev-env-template.json` in the template repository. For compatibility with the existing `python-dev-environment-template`, `dev-env-template.json` is also accepted. Metadata is read by the CLI but is not copied into target projects.

Example:

```json
{
  "template": {
    "name": "python-dev-environment-template",
    "version": "0.1.0",
    "include": ["AGENTS.md", "opencode.json", ".opencode", "openspec", ".gitignore"],
    "exclude": ["/AGENTS.md", "/opencode.json", "/.opencode/", "/openspec/", "/.gitignore", "/.dev-env/"]
  }
}
```

`include` is an explicit allowlist. Directories are copied recursively. Missing paths are ignored.

## Git behavior

The CLI adds a clearly marked block to the repository-local Git exclude file, located using `git rev-parse --git-path info/exclude`. It preserves template exclusions and adds Python virtual-environment/cache exclusions. It does not edit the project's `.gitignore`.

Important: Git exclude rules affect only untracked files. If a path is already tracked, it remains tracked and can still be pushed. The CLI reports tracked paths but does not run `git rm --cached` automatically.

## Safety rules

- `init` copies files that do not exist; it never overwrites existing files.
- Existing, unmanaged files are skipped, even if they differ from the template.
- `update` only overwrites a managed file when its current SHA-256 still matches the hash recorded at the previous installation. Local edits are preserved and reported as conflicts.
- Files removed from the template are never automatically deleted from a project.
- Symlinks in templates and managed destination paths are rejected.
- `.dev-env/manifest.json` records managed files, their last installed hashes, and Git exclude patterns.
- Global OpenCode configuration is read-only.
- Existing `mise.toml`, Ruff/Pyright settings, and virtual environments are preserved; only missing tool/task entries or default config files are added.

## OpenSpec integration

OpenSpec is managed in two scopes:

- The OpenSpec CLI is installed once per user machine.
- Each project is initialized with the OpenCode adapter and keeps its own `openspec/` data and generated `.opencode/` skills/commands.

`dev-env init` checks for the `openspec` executable. If it is missing, the CLI asks before running the official standalone installer from `https://openspec.dev/cli/install.sh`; the installer defaults to `~/.local/bin`. On non-interactive terminals, installation is skipped unless `--yes` is supplied. The installer runs without `sudo`.

After the CLI is available, `dev-env init` runs:

```bash
openspec init --tools opencode
```

The explicit tool selection avoids the interactive tool picker, so this also works on headless Ubuntu servers. OpenSpec's `init` is safe to rerun: it refreshes the OpenCode integration and adds it when the project was initialized for another tool.

`dev-env update` runs `openspec update` only when `openspec/config.yaml` exists. It sets `OPENSPEC_NO_UPDATE_CHECK=1` so refreshing project-generated files does not unexpectedly trigger a global CLI upgrade prompt. Update the global CLI separately using the official OpenSpec installation instructions.

Useful commands:

```bash
dev-env init                 # ask before installing a missing OpenSpec CLI
dev-env init --yes           # approve missing tool installation, including OpenSpec
dev-env init --skip-openspec # skip OpenSpec for this run
dev-env update               # refresh generated OpenSpec files in initialized projects
dev-env update --skip-openspec
dev-env doctor               # check CLI, openspec/config.yaml, OpenCode skills and commands
```

`doctor` checks the OpenSpec CLI version, `openspec/config.yaml`, `.opencode/skills/openspec-*/SKILL.md`, and `.opencode/commands/opsx-*.md`. It does not execute OpenSpec workflows or change global configuration.

The project-local OpenSpec data and OpenCode integration files are excluded through `.git/info/exclude` by default, matching this template's personal/local-only configuration policy. If you want specifications and archived changes to be shared with teammates, remove `/openspec/` from the local exclude block or change the template policy before using it in a collaborative repository.
