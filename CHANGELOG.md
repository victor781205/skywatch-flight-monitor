# Changelog

All notable changes to SkyWatch are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

- Prepare public open-source governance and GitHub automation.
- Replace machine-specific Windows launch paths with portable project-relative paths.
- Remove the Amadeus fallback from runtime and settings because it is not an
  available credential path for this project's owner.
- Add optional Travelpayouts Data API market context to manual searches, with
  explicit cached-data labelling and no influence on alerts.
- Remove the six-hour notification cooldown. Fixed 00:00 and 12:00 reports use
  dedicated schedule-slot claims, while concurrent duplicate delivery remains blocked.
- Restore return-flight details for the top itinerary in fixed round-trip reports;
  booking-provider lookup remains target-hit only.

## [2.4.0] - 2026-08-11

### Added

- Local monthly API usage telemetry with configurable quota limits and an 80%
  one-time warning.
- Scheduled monthly usage projection in the system settings UI.
- Optional Amadeus Self-Service fallback provider.
- Provider and quota behavior tests.

### Changed

- Routine scheduled monitoring now uses one primary fare search per monitor.
- Return-itinerary and booking-provider lookups run only when the target price is
  reached.
- Preferred-airline summaries reuse the primary response instead of issuing
  additional API calls.

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
