# Release checklist

1. Confirm the working tree contains no `.env`, database, logs, or user exports.
2. Regenerate both hash-locked dependency files with the documented `uv pip
   compile` commands in their headers; review the complete dependency diff.
3. Install `requirements-dev.lock` with `--require-hashes` in a clean environment.
4. Run `python -m ruff check .`.
5. Run `python -m coverage run -m unittest discover -s tests -v` and
   `python -m coverage report --fail-under=55 --show-missing`.
6. Run `python -m compileall -q app.py scheduler.py telegram_bot.py src tests`.
7. Update `CHANGELOG.md`, `README.md`, and `src/config.py` to the same version.
8. Review database migrations, backup compatibility, dependencies, and external
   service changes.
9. Verify the desktop and 390-pixel mobile layouts in a local browser.
10. Create an annotated tag such as `v3.1.0` and publish release notes from the
    changelog after the full GitHub Actions matrix passes.
11. Verify a clean installation on at least one supported OS.

Do not send a real notification as part of automated release validation.
