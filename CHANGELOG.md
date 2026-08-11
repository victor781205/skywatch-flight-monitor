# Changelog

All notable changes to SkyWatch are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

- Prepare public open-source governance and GitHub automation.
- Replace machine-specific Windows launch paths with portable project-relative paths.

## [0.2.3] - 2026-08-11

### Added

- Unified six-page light UI for dashboard, search, history, monitoring,
  settings, and help.
- Asia/Taipei scheduler timezone and machine-readable health heartbeat.
- Per-channel exact-message fingerprinting for Telegram and Discord delivery.
- Tests for scheduler safety and notification deduplication.

### Fixed

- Prevented duplicate quote notifications when multiple processes trigger the
  same monitor.
- Exposed the latest notification and next scheduled run in the UI.

## [0.2.2] - 2026-08-10

- Separated the web process from the production scheduler.
- Added cross-process job locking, notification claims, and cooldown checks.
