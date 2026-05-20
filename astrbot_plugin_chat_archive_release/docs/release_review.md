# Release Review Notes

Review date: 2026-05-21

This release package has been cleaned for distribution under `astrbot_plugin_chat_archive_release/`.

## Current Guardrails

- WebUI defaults to `127.0.0.1:8090`; use a strong `api_key` before binding to LAN or public interfaces.
- Media cache/proxy downloads are restricted by `allowed_media_domains` or `ARCHIVE_ALLOWED_MEDIA_DOMAINS`, block private DNS targets, and enforce per-file size limits.
- SQLite connections are returned to the pool only after pending transactions are rolled back.
- Tool-facing archive queries clamp large `limit` and `offset` values.
- Runtime files are excluded by the package-local `.gitignore`.

## Validation

- JSON config schema parses with `python3 -m json.tool`.
- Python files parse with `ast.parse`.
- `db_config.py` and `web/server.py` compile with `python3 -m py_compile`.

Node.js is not installed in the current review environment, so the WebUI JavaScript syntax check should be run separately where Node is available.
