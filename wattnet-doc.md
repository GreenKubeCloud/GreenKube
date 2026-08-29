Hi Hugo,

I have now granted you access so you should be able to register and generate a Bearer token for development and testing purposes.

Here are the details to get started with the Wattnet API.

Token Service

The token service is available at: https://api.wattnet.eu/token-request/
You can also interact with it directly through the browser at that URL.

1. Register (only required once)

curl -X POST "https://api.wattnet.eu/token-request/register" \
  -H "Content-Type: application/json" \
  -d '{ "email": "your_email", "password": "your_password" }'

2. Obtain a token

Tokens are valid for 1 day and can be refreshed using the same credentials:

curl -X POST "https://api.wattnet.eu/token-request/get_token" \
  -H "Content-Type: application/json" \
  -d '{ "email": "your_email", "password": "your_password" }'

3. Query the API

curl -X GET "https://api.wattnet.eu/footprints?footprint_type=carbon…" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <your_token>"

The API provides both historical and forecasted footprint data, including carbon intensity metrics suitable for green scheduling, as well as forecasting series of green-score metrics (accounting for carbon and water footprint).


Important – Authentication note

The credentials used for the token service are currently separate from those used to access the API documentation interface. At the moment, access to the API docs only works via social login (GitHub or Google). This is something we will progressively unify as we onboard more external users.

API documentation: https://api.wattnet.eu/v1/docs (more detailed docs are in progress)

Data quality flags

Two flags in the API results are worth noting:

zone_status:
  - complete: all required inputs are present.
  - estimated: some inputs are estimated due to missing real data.
  - missing: required inputs are not available.

valid:
  - true: the data is final and will not change.
  - false: recalculation is pending due to missing country-level inputs. This state lasts at most until now - 4h.

I'm also sharing some slides for additional context:

Wattnet platform: https://indico.cern.ch/event/1526482/contributions/7027740/attachments/3270024/5841184/wattnet%20-%20SC4RC%202026%20-%20Talk.pdf
Scaphandre energy monitoring: https://docs.google.com/presentation/d/1FC4x8CKRtHbXbYdihlDS_BUq9TqKvREwODXFczn2HHI/edit?usp=sharing

Feel free to reach out if you have any questions!

Best,

Jaime