# Release

This repository is a release collection for multiple AstrBot plugin projects.

Each project must live in its own top-level directory. Do not place plugin source files directly in the repository root.

## Current Structure

```text
release/
├── README.md
├── .gitignore
└── astrbot_plugin_chat_archive_release/
    ├── README.md
    ├── CHANGELOG.md
    ├── metadata.yaml
    ├── main.py
    ├── _conf_schema.json
    ├── requirements.txt
    ├── contrib/
    ├── docs/
    ├── tests/
    └── web/
```

## Project Directories

- `astrbot_plugin_chat_archive_release/`: AstrBot chat archive plugin release package.

## Required Push Structure

When adding or updating a release project, push it as a complete top-level project directory:

```text
release/
└── astrbot_plugin_<project_name>_release/
    ├── README.md
    ├── CHANGELOG.md
    ├── metadata.yaml
    ├── main.py
    ├── _conf_schema.json
    ├── requirements.txt
    └── ...
```

Required rules:

- Keep repository-level files only in the root, such as `README.md` and `.gitignore`.
- Keep each plugin's source, assets, docs, deployment files, and web resources inside that plugin's own directory.
- Do not push Python cache files, local databases, runtime data, logs, virtual environments, or generated media caches.
- Use `git add -A` after moving project files so Git records renames instead of leaving root-level deletions and untracked copies.

Recommended push flow:

```bash
git status --short
git add -A
git status --short
git commit -m "Archive release project layout"
git push
```
