# Contributing to SkyWatch

Thanks for helping improve SkyWatch. Bug reports, documentation fixes, tests,
accessibility improvements, and focused feature proposals are welcome.

## Development setup

1. Fork and clone the repository.
2. Create a virtual environment: `python -m venv .venv`.
3. Activate it and install dependencies:
   `pip install --require-hashes -r requirements-dev.lock`.
4. Copy `.env.example` to `.env` and add only the credentials needed for the
   feature you are testing.
5. Run `python -m ruff check .`, the complete unittest suite, coverage gate,
   and compile check before submitting a change.

The unit tests do not send real Telegram or Discord messages. Running
`test_telegram.py` is an explicit manual operation and requires typing `SEND`.

## Pull requests

- Open an issue first for large or user-visible changes.
- Keep each pull request focused and include tests for behavior changes.
- Never commit `.env`, API credentials, local databases, scheduler logs, or
  exported user data.
- Explain the user impact, test evidence, and any migration steps.
- Preserve the single light visual theme and accessible keyboard focus states.

## Commit style

Use short imperative subjects, for example:

- `fix: suppress duplicate Telegram delivery`
- `feat: expose scheduler heartbeat`
- `docs: clarify SerpAPI setup`

By contributing, you agree that your contribution is licensed under the MIT
License included in this repository.
