# dev-env

Personal, safe-by-default installer for project-local development configuration.

## Requirements

- Python 3.12+
- Git
- `uv` for installation as a global CLI tool

The CLI itself uses only the Python standard library.

## Install from a local checkout

```bash
uv tool install --from ./dev-env-cli dev-env
```

Or install directly from your CLI Git repository:

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
dev-env config set-template "https://github.com/USERNAME/python-dev-environment-template.git"
```

A Git template is shallow-cloned for each `init`/`update`. To select a branch or tag, pass `--ref`.

## Use in any Git project

```bash
cd /path/to/project
dev-env init
```

Useful commands:

```bash
dev-env init --template /path/to/python-dev-environment-template
dev-env update
dev-env doctor
dev-env diff
dev-env config show
```

## Template metadata

Place `.dev-env-template.json` in the template repository. This file is read by the CLI but is not copied into target projects.

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

The CLI adds a clearly marked block to the repository-local Git exclude file, located using `git rev-parse --git-path info/exclude`. It does not edit the project's `.gitignore`.

Important: Git exclude rules affect only untracked files. If a path is already tracked, it remains tracked and can still be pushed. The CLI reports tracked paths but does not run `git rm --cached` automatically.

## Safety rules

- `init` copies files that do not exist; it never overwrites existing files.
- Existing, unmanaged files are skipped, even if they differ from the template.
- `update` only overwrites a managed file when its current SHA-256 still matches the hash recorded at the previous installation. Local edits are preserved and reported as conflicts.
- Files removed from the template are never automatically deleted from a project.
- Symlinks in templates are rejected.
- `.dev-env/manifest.json` records managed files and their last installed hashes.

## Current scope (0.1.0)

This version provides the safe file-installation lifecycle. The current Python template can be expanded later with more agents, Skills, OpenSpec assets, Mise tasks, and LSP/tooling configuration without changing the CLI's core behavior.
