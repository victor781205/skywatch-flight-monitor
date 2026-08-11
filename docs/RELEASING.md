# Release checklist

1. Confirm the working tree contains no `.env`, database, logs, or user exports.
2. Run `python -m unittest discover -s tests -v`.
3. Run `python -m compileall -q app.py scheduler.py src tests`.
4. Update `CHANGELOG.md` and `src/config.py`.
5. Review dependency and external-service changes.
6. Create an annotated tag such as `v0.2.3`.
7. Publish GitHub release notes from the changelog.
8. After release, verify a clean installation on at least one supported OS.

Do not send a real notification as part of automated release validation.
