# SkyWatch Flight Monitor

[![Tests](https://github.com/victor781205/skywatch-flight-monitor/actions/workflows/tests.yml/badge.svg)](https://github.com/victor781205/skywatch-flight-monitor/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-15965a.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-087443.svg)](https://www.python.org/)

SkyWatch is a local-first, self-hosted flight-fare monitoring dashboard. It
combines Streamlit, SQLite, scheduled flight searches, price history, and
Telegram/Discord alerts in a single light operational interface.

SkyWatch 是可自行部署的本機航班票價監控工作台，提供繁體中文介面、價格歷史、
目標價監控，以及 Telegram／Discord 通知。

> SkyWatch is an independent project. Fare data can be delayed or inaccurate;
> always confirm the final itinerary and price with the booking provider.

## Why SkyWatch

- Search one-way and round-trip fares across approximately 8,800 IATA airports.
- Compare cabin classes, passenger counts, stops, duration, and historical range.
- Persist quote history locally in SQLite.
- Run monitoring at fixed Asia/Taipei times or a configurable interval.
- Send scheduled reports and target-price alerts through Telegram or Discord.
- Conserve free API quota by using one primary search per routine monitor run.
- Track successful monthly API requests and warn once when usage reaches 80%.
- Prevent duplicate alerts with process locks, atomic database claims, cooldowns,
  and per-channel exact-message fingerprints.
- Inspect scheduler health, latest notification, and next run from the UI.
- Keep credentials, travel data, databases, and logs outside version control.

## Interface

The responsive UI uses one coherent light theme and includes six views:

1. Dashboard
2. Flight search
3. Price history
4. Monitor management
5. Notifications and system settings
6. Help and onboarding

## Requirements

- Python 3.10 or newer
- A [SerpAPI](https://serpapi.com/) API key for live flight search
- Optional Telegram bot credentials and/or Discord webhook

Each active monitor consumes external search quota when it runs. Review your
provider limits before increasing the schedule frequency.

## Quick start

```bash
git clone https://github.com/victor781205/skywatch-flight-monitor.git
cd skywatch-flight-monitor
python -m venv .venv
```

Activate the environment:

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

```bash
# macOS / Linux
source .venv/bin/activate
```

Install dependencies and configure the application:

```bash
pip install -r requirements.txt
cp .env.example .env
```

On Windows Command Prompt, use `copy .env.example .env` instead of `cp`.

Edit `.env`:

```env
SERPAPI_API_KEY=
SERPAPI_MONTHLY_LIMIT=250
API_USAGE_WARNING_PERCENT=80

MONITOR_FETCH_DETAILS_ON_HIT=true
DISCORD_WEBHOOK_URL=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

ALWAYS_NOTIFY=true
NOTIFY_COOLDOWN_HOURS=6
MESSAGE_DEDUPE_HOURS=24
SCHEDULE_HOURS=0,12
CHECK_INTERVAL_MINUTES=
```

Start the web application:

```bash
python -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501
```

Open <http://127.0.0.1:8501/>.

Start the independent scheduler in another terminal:

```bash
python scheduler.py
```

Windows users can also run `start_skywatch.bat` and `start_scheduler.bat`.
The accompanying `.vbs` files start those commands without a visible console.

## Notification behavior

`ALWAYS_NOTIFY=true` sends a report on every scheduled monitor run. When it is
false, SkyWatch notifies only when the target is reached or the price changes.

Duplicate prevention is layered:

1. APScheduler permits one in-process job instance.
2. A filesystem lock permits one scheduler process to check monitors.
3. SQLite atomically claims each notification window.
4. Each external channel claims a fingerprint of the complete message.
5. Successful external notifications participate in the configured cooldown.

Routine scheduled reports use only the primary search response. For round trips,
SkyWatch requests the top itinerary's return and booking details only after the
target price is reached. It no longer performs one detail request per preferred
airline during every scheduled run.

## Flight-data providers and quota

SerpAPI remains the default because its Google Flights response is practical for
self-hosted monitoring. Set `SERPAPI_MONTHLY_LIMIT` to the limit shown on your
plan. SkyWatch counts successful requests locally, projects scheduled monthly
usage, and sends one warning per provider and month at
`API_USAGE_WARNING_PERCENT`.

Provider keys are never committed or included in usage telemetry. Additional
fare providers should only be enabled after confirming that the project owner
can obtain and legally use production credentials.

The UI process does not start a second scheduler. See
[Architecture](docs/ARCHITECTURE.md) for the complete flow.

## Airport data

The first airport search downloads and builds a local index from
[OurAirports](https://ourairports.com/data/). You can refresh it manually:

```bash
python scripts/download_airports.py
```

Generated airport indexes are not committed. OurAirports states that the data
is in the Public Domain and carries no guarantee of accuracy or fitness. See
[Third-Party Notices](THIRD_PARTY_NOTICES.md).

## Testing

```bash
python -m unittest discover -s tests -v
python -m compileall -q app.py scheduler.py src tests
```

The automated test suite does not send real notifications. The manual
`test_telegram.py` utility requires the operator to type `SEND` before it makes
a network request.

## Security and privacy

- Never commit `.env`, credentials, `data/*.db`, logs, or exported user data.
- Rotate any token that may have been disclosed.
- The default server binds only to loopback and does not include multi-user
  authentication. Add appropriate authentication and network controls before
  remote exposure.
- Report vulnerabilities using GitHub private vulnerability reporting; see
  [Security Policy](SECURITY.md).

## Project structure

```text
skywatch-flight-monitor/
├── .github/                 # CI, issue forms, PR template, ownership
├── docs/                    # Architecture and release documentation
├── scripts/                 # Data maintenance utilities
├── src/                     # Core application modules
├── tests/                   # Unit tests
├── app.py                   # Streamlit UI
├── scheduler.py             # Production scheduler service
├── style.css                # Unified light design system
└── requirements.txt
```

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md), open an issue for substantial changes,
and keep pull requests focused. Conduct in project spaces is governed by the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Maintainer

[Victor — @victor781205](https://github.com/victor781205)

## License

SkyWatch source code is released under the [MIT License](LICENSE). External
services and downloaded data remain subject to their own terms and notices.
