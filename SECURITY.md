# Security Policy

## Supported version

Security fixes are applied to the latest released version of SkyWatch.

## Reporting a vulnerability

Please do not disclose vulnerabilities in a public issue. Use GitHub's private
vulnerability reporting feature for this repository. If it is unavailable,
contact the maintainer through the public GitHub profile
[@victor781205](https://github.com/victor781205) and request a private channel.

Include affected versions, reproduction steps, impact, and any proposed fix.
Do not include real API keys, Telegram bot tokens, chat IDs, or user travel
data. The maintainer will acknowledge a complete report as soon as practical
and coordinate disclosure after a fix is available.

## Deployment notes

- Keep `.env` outside version control and rotate any credential that may have
  been exposed.
- The default web server binds to `127.0.0.1`; review authentication and network
  controls before exposing it beyond the local computer.
- Telegram, Discord, SerpAPI, and booking links are external services with
  separate security and privacy policies.
