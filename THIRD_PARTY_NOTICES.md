# Third-Party Notices

## OurAirports data

SkyWatch can download airport metadata from
[OurAirports](https://ourairports.com/data/). OurAirports states that its data
is released to the Public Domain and is provided without a guarantee of
accuracy or fitness for use. Generated `data/airports_iata.csv` and
`data/airports_iata.json` files are intentionally excluded from version
control and can be regenerated with `python scripts/download_airports.py`.

## External services

SkyWatch can interact with SerpAPI, optional Amadeus Self-Service APIs, Google
Flights links, Telegram, and Discord.
Those services are not included in the MIT license for SkyWatch and are subject
to their own terms, quotas, availability, branding, and privacy policies.

Amadeus test-environment data may be limited or cached; production access,
quotas, and overage terms are controlled by Amadeus and the user's account.

SkyWatch is an independent project and is not affiliated with or endorsed by
OpenAI, Google, SerpAPI, Amadeus, Telegram, Discord, or any airline.
