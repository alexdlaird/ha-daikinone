# Daikin One for Home Assistant

[![CI](https://github.com/alexdlaird/ha-daikinone/actions/workflows/ci.yml/badge.svg)](https://github.com/alexdlaird/ha-daikinone/actions/workflows/ci.yml)
[![hassfest](https://github.com/alexdlaird/ha-daikinone/actions/workflows/hassfest.yml/badge.svg)](https://github.com/alexdlaird/ha-daikinone/actions/workflows/hassfest.yml)
[![HACS validation](https://github.com/alexdlaird/ha-daikinone/actions/workflows/hacs.yml/badge.svg)](https://github.com/alexdlaird/ha-daikinone/actions/workflows/hacs.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A fork of [shsu/ha-daikinone](https://github.com/shsu/ha-daikinone) that adds a read-only mode, composed off /
fan-only modes that preserve your circulation setting, and range handling that works with wrapper thermostats.

A Home Assistant integration for Daikin One thermostats, built on the official
**Daikin One Open API** at `https://integrator-api.daikinskyport.com` and nothing else.

It authenticates with an API key and an Integrator Token issued by Daikin. It never asks for
your Daikin account password, never stores one, and never calls the private Skyport endpoints
that the older community integrations depend on.

Why the official API? Daikin is moving accounts to SSO on the skyportcloud platform, and that
migration has been breaking password logins in the private-API integrations one account at a
time (`zlangbert/ha-daikinone` issues #121/#128/#130, `apetrycki/daikinskyport` issue #156).
The Integrator API is documented, supported by Daikin, and survives account-system changes.
The price is scope: the official API returns thermostat data only. [Known
limitations](#known-limitations) spells out exactly what that excludes, and
[docs/FEATURE_MATRIX.md](docs/FEATURE_MATRIX.md) maps every legacy feature to its fate.

## Contents

- [Supported devices](#supported-devices)
- [Use cases](#use-cases)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Data updates](#data-updates)
- [Supported functionality](#supported-functionality)
- [Actions](#actions)
- [Examples](#examples)
- [Known limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [Removing the integration](#removing-the-integration)
- [Migrating from another integration](#migrating-from-another-integration)
- [Attribution and licensing](#attribution-and-licensing)

## Supported devices

Daikin lists these thermostats as supported by the Open API:

| Thermostat | Notes |
| --- | --- |
| Daikin One+ | Communicating unitary systems; full mode set including emergency heat on dual fuel |
| Daikin One Touch | Reported by the API as model `TOUCH` |
| Daikin One Lite | Reduced feature set on the thermostat itself |
| Amana Smart Thermostat | Same API surface |
| Goodman Connected GTST | Same API surface |

The reference system this integration is developed and live-tested against is a Daikin One+
(firmware 4.0.17) controlling a DM96VC0803BNAB 80 kBTU two-stage gas furnace and a
DZ17VSA361AA 3-ton variable-speed inverter heat pump, a dual-fuel unitary setup.

That equipment list is context for what the thermostat controls, not a list of what the
integration reads. The official API exposes thermostat data only. No documented endpoint
carries furnace or heat-pump telemetry: no stage or demand percentages, no airflow, no
compressor or blower speeds, no coil temperatures, no inverter current, no runtime counters,
no energy. Those entities existed in the private-API integrations and cannot be reproduced
here.

VRV (P1P2) and single/multi-split (S21) systems work for temperature and mode control, but
Daikin documents that they ignore fan-circulation writes. See [Known
limitations](#known-limitations).

## Use cases

- Read indoor and outdoor temperature and humidity into Home Assistant for automations,
  dashboards, and statistics.
- Change HVAC mode and setpoints from Home Assistant (voice assistants, dashboards, presence
  automations) without handing a third party your Daikin password.
- Turn the thermostat's own schedule on or off from an automation. Disable it while you are on
  vacation, re-enable it when you return.
- Set fan circulation mode and speed on unitary systems.
- Alert when the thermostat goes offline, or when geofencing has taken control of the
  setpoints.

## Prerequisites

You need three values. All three come from Daikin's own apps, and none of them is your
password.

### 1. Your Daikin account email

The email address you use for the Daikin One Home app. It is case-sensitive: Daikin's token
endpoint rejects `owner@example.com` if your account is `Owner@Example.com`. Copy it exactly
as the app shows it.

### 2. The Integrator Token

1. Open the **SkyportHome** app, signed in as the home owner.
2. Go to **SkyportCare** → **home integration**.
3. Tap **get integration token**.
4. Type your account password in the app when prompted. The token is shown once. Copy it.

The Integrator Token is a very long JWE, roughly 1,700 to 2,500 characters in five
dot-separated segments. Paste the whole thing.

### 3. The API key

1. Enable the developer menu:
   - iOS: open the system **Settings** app → **SkyportHome** → turn on the developer menu
     toggle.
   - Android: open **SkyportCare** → **home integration** and tap the page description five
     times.
2. Go to **SkyportCare** → **home integration** → **developer**.
3. Agree to the B2B and Open API terms and enter an application name.
4. The API key is issued on screen. Copy it. It is short and opaque, nothing like the JWE.

Keep these secret. The Integrator Token and API key together give full control of your
thermostats. Never paste them into a YAML file, a GitHub issue, a forum post, a log, or a
shell command (shell history keeps them). Enter them only into the Home Assistant config
flow. If you think one leaked, revoke the integration token in the SkyportHome app and issue
a new one; requesting a new token invalidates the old one.

## Installation

### HACS (recommended)

1. In Home Assistant, open **HACS**.
2. Open the three-dot menu → **Custom repositories**.
3. Repository: `https://github.com/alexdlaird/ha-daikinone`, Type: **Integration** → **Add**.
4. Find **Daikin One** in the HACS integration list and **Download** it.
5. Restart Home Assistant.

### Manual

1. Download this repository.
2. Copy `custom_components/daikinone` into your Home Assistant
   `config/custom_components/` directory, so that
   `config/custom_components/daikinone/manifest.json` exists.
3. Restart Home Assistant.

### Add the integration

**Settings** → **Devices & Services** → **Add Integration** → search for **Daikin One**. The
config flow asks for the three values from [Prerequisites](#prerequisites), then verifies them
by requesting an access token and listing your devices before it creates the entry.

## Configuration

Configuration is entirely through the UI. There is no YAML configuration.

### Configuration parameters

| Parameter | Required | Description |
| --- | --- | --- |
| Email | yes | Your Daikin account email, exactly as capitalised in the app. Used (lowercased) as the config entry's unique id, so one entry per account. |
| API key | yes | The developer API key from the SkyportHome developer menu. Sent as the `x-api-key` header on every request. |
| Integrator Token | yes | The long JWE from **SkyportCare → home integration → get integration token**. Exchanged for a 15-minute access token and never sent anywhere else. |

### Options

Open the integration entry → **Configure**.

| Option | Default | Range | Description |
| --- | --- | --- | --- |
| Polling interval | `180` seconds | 180 to 3600 s, in 30 s steps | How often the integration reads every thermostat. 180 seconds is the minimum. It matches Daikin's documented limit of one poll per three minutes; the form rejects lower values and the code clamps them anyway. |
| Read-only | on | on / off | While on, every change (setpoints, mode, fan, schedule) is validated exactly as a real write, then recorded instead of sent: nothing reaches Daikin, the entities keep showing the thermostat's real state, and the thermostat's own schedule is untouched. Each kept-back request lands in the **Last intended write** sensor and fires a `daikinone_write_suppressed` event. Turn it off to let Home Assistant control the thermostat. |

Changing an option reloads the entry.

## Data updates

The integration polls Daikin's cloud (`iot_class: cloud_polling`).

Every 180 seconds (or your configured interval) it lists your locations and reads each
thermostat, with up to 10 seconds of random jitter per cycle so that many installations don't
hit Daikin in lockstep. Daikin's published usage limits are respected exactly: never poll
faster than once every three minutes, never hold more than 3 open requests at a time (the
client sits behind a semaphore of three), and wait at least 15 seconds after a write before
reading back.

After a successful write the integration shows the new value immediately, then waits 15
seconds before one confirmation read. Writes that land close together share a single
confirmation read rather than queuing one each.

Access tokens live 900 seconds and are refreshed 60 seconds early, under a single lock, so
concurrent requests never trigger more than one token request. On HTTP 429 the integration
backs off exponentially up to 30 minutes and honours a `Retry-After` header when Daikin sends
one. A thermostat that fails on its own (offline, server error) goes unavailable individually
while the rest of the account keeps updating.

## Supported functionality

One Home Assistant device is created per thermostat, named after the thermostat and prefixed
with the Daikin location name when your account has more than one location.

| Platform | Entity | Category | Enabled by default | Description |
| --- | --- | --- | --- | --- |
| `climate` | Thermostat | | yes | HVAC modes off / heat / cool / heat_cool, filtered by the thermostat's mode limit, plus fan_only when the thermostat reports a fan circulation setting (see [Off and fan only](#off-and-fan-only)). Current temperature and humidity, target setpoint or range, HVAC action, and the `emergency_heat` preset on systems that report it. Celsius natively; Home Assistant converts for display. |
| `sensor` | Indoor temperature | | yes | Indoor temperature, °C. |
| `sensor` | Indoor humidity | | yes | Indoor relative humidity, %. |
| `sensor` | Outdoor temperature | | yes | Outdoor temperature as reported by the thermostat, °C. |
| `sensor` | Outdoor humidity | | yes | Outdoor relative humidity, %. |
| `sensor` | System fan | Diagnostic | yes | Enum: `auto` or `on`. Read-only; the API exposes the fan state but no way to set it. |
| `sensor` | Last intended write | Diagnostic | yes | Timestamp of the latest write that [read-only mode](#options) kept from Daikin. Attributes: `read_only`, plus the latest would-be request per endpoint (`msp`, `fan`, `schedule`) with its time and the requesting user. |
| `switch` | Schedule | | yes | Turns the thermostat's own schedule on and off. |
| `binary_sensor` | Connectivity | Diagnostic | yes | On when the thermostat answered the last read. Stays available while the thermostat is offline so you can alert on it. |
| `binary_sensor` | Geofencing | Diagnostic | yes | Read-only: whether geofencing is enabled on the thermostat. |
| `binary_sensor` | Heating / Cooling / Dehumidifying / Fan running | Diagnostic | no | Equipment-status booleans. Disabled by default because they duplicate the climate entity's HVAC action; enable the ones you want history for. |
| `select` | Fan circulation | Config | yes | `off` / `always_on` / `schedule`. Unitary systems only. |
| `select` | Fan circulation speed | Config | yes | `low` / `medium` / `high`. Unitary systems only. |

A note for VRV and mini-split owners: the two fan selects are enabled by default because the
official API has no capability field that would let the integration detect support. Your
equipment ignores fan writes (Daikin documents that the fan runs at maximum speed on P1P2 and
S21 systems). The integration never writes to the fan endpoint unless you change one of the
selects yourself, and a rejected write raises a repair issue that tells you to disable the two
entities. You can disable them from each entity's settings page.

### Off and fan only

Daikin has no fan-only mode; fan circulation is a separate setting (its own endpoint) that is
independent of the thermostat mode. So both "off" modes are composed from the mode plus the
circulation setting:

| Home Assistant mode | Thermostat mode | Fan circulation |
| --- | --- | --- |
| `off` | off | off |
| `fan_only` | off | always on |

The entity reports whichever combination the thermostat is in, however it got there (with
circulation on `schedule`, mode off reads as `off`). Entering `off` or `fan_only` from an active
mode remembers your circulation setting (kept across restarts), and switching back to an active
mode restores it, so an always-on or scheduled circulation preference survives an off period.
`off` turns the mode off before the fan; `fan_only` sets the fan first, so a failed second write
leaves the equipment conditioning rather than doing nothing. Only writes that change something
are sent, and fan speed is never touched. Equipment without fan circulation (VRV, splits) gets
plain `off` and no `fan_only`.

### Setpoint ranges in heat or cool mode

In heat and cool mode only one setpoint is live. A range whose sides are closer than Daikin's
minimum gap (for example `target_temp_low == target_temp_high`, which some wrapper thermostats
always send) keeps the live side and pushes the other one out by the gap, the same way a single
`temperature` does. In heat_cool both sides are live, so such a range is still rejected.

Diagnostics are available on the device page and are fully redacted. See
[Troubleshooting](#troubleshooting).

## Actions

This integration does not provide custom actions, triggers, or conditions. Use Home
Assistant's standard entity actions: `climate.set_temperature`, `climate.set_hvac_mode`,
`climate.set_preset_mode`, `climate.turn_on`, `climate.turn_off`, `switch.turn_on`,
`switch.turn_off`, and `select.select_option`, with ordinary state triggers on the entities.

## Examples

### Set back the temperature overnight

```yaml
automation:
  - alias: "Overnight setback"
    triggers:
      - trigger: time
        at: "22:30:00"
    actions:
      - action: climate.set_temperature
        target:
          entity_id: climate.hallway
        data:
          hvac_mode: heat
          temperature: 18.5
```

### Suspend the thermostat schedule while away, restore it on return

```yaml
automation:
  - alias: "Pause thermostat schedule while away"
    triggers:
      - trigger: state
        entity_id: person.owner
        to: "not_home"
        for: "00:30:00"
    actions:
      - action: switch.turn_off
        target:
          entity_id: switch.hallway_schedule

  - alias: "Resume thermostat schedule when home"
    triggers:
      - trigger: state
        entity_id: person.owner
        to: "home"
    actions:
      - action: switch.turn_on
        target:
          entity_id: switch.hallway_schedule
```

Note that changing a setpoint or mode turns the schedule off at the thermostat. That is
Daikin's own behaviour, not this integration's. If you use both automations above, set the
temperature before re-enabling the schedule, not after.

## Known limitations

The following are absent because the Daikin One Open API does not expose them, and this
integration refuses to fake values it cannot read:

- Equipment telemetry of any kind: furnace, air-handler, and outdoor-unit demand percentages,
  airflow (CFM), power (W), compressor speed (RPS), fan RPM, refrigerant pressures, coil
  temperatures, inverter current, runtime counters.
- Indoor air quality: particles, VOC, ozone, AQI scores.
- Energy or runtime history.
- Schedule contents. The API can enable or disable the schedule, nothing more; its periods
  cannot be read or edited.
- Temporary or permanent holds as first-class objects, and the Away/vacation state.
- Target humidity, humidifier and dehumidifier control, One Clean, night mode, efficiency
  priority.
- Fault codes and the weather forecast shown in the app.

Behavioural quirks worth knowing:

- Writing mode or setpoints turns off the thermostat's schedule and its Away state. This is
  documented Daikin behaviour for the `/msp` endpoint, and it is why the schedule switch flips
  off after a temperature change.
- Writes are queued, not applied. Daikin answers a write with `{"message": "Write sent"}` and
  the change can take up to 15 seconds to reach the thermostat, which is why the integration
  shows the new value optimistically and re-reads after a 15 second delay. If the thermostat
  rejects the change, the value reverts at the next read.
- Fan circulation is unitary-only. VRV (P1P2) and single/multi-split (S21) systems ignore the
  fan endpoint, and there is no capability flag to detect them in advance.
- The API speaks Celsius only. Home Assistant converts for display and the integration
  converts setpoints back before writing.
- Setpoint rules are the thermostat's: heat setpoint at or below cool setpoint minus the
  delta, both within the thermostat's minimum and maximum. The integration validates before
  writing and raises a clear error instead of sending a request that would fail.
- Polling is coarse by design. One read per three minutes means state can be up to 180
  seconds stale, except right after a write, where the confirmation read closes the gap.
- There is no push, webhook, or local API. If your internet or Daikin's cloud is down, the
  entities go unavailable.

A full legacy-feature comparison is in [docs/FEATURE_MATRIX.md](docs/FEATURE_MATRIX.md).

## Troubleshooting

| Symptom | Meaning | Fix |
| --- | --- | --- |
| Config flow: invalid authentication (HTTP 400/401) | The Integrator Token is wrong or stale, or the email case does not match the account. Requesting a new token invalidates older ones. | Copy the email exactly as the app shows it and issue a fresh integration token. |
| Config flow: invalid API key (HTTP 403) | The `x-api-key` value is not accepted. | Re-copy the API key from the developer menu. Do not paste the Integrator Token here; they are different values. |
| Config flow: rate limited (HTTP 429) | Too many requests to Daikin, often from a second integration or script on the same key. | Wait a few minutes and retry. Once configured, the integration backs off on its own. |
| Config flow: no devices found | The account authenticated but owns no thermostats. | Use the home owner's account, not a guest account. |
| Config flow: cannot connect | Network failure, a 5xx from Daikin, or a malformed response. | Check connectivity and retry. If it persists, attach diagnostics to an issue. |
| Entry shows "Reauthentication required" | Credentials stopped working (401/403 during polling). | Reauthenticate and re-enter email, API key, and Integrator Token. |
| All entities unavailable at once | Account-level failure: authentication, rate limiting, or Daikin's cloud unreachable. | Check the log for the coordinator error. The integration retries with backoff. |
| One thermostat unavailable, others fine | That thermostat is offline (`DeviceOfflineException`). Its connectivity sensor stays available and reads off. | Check that thermostat's Wi-Fi. |
| A fan select raises "fan controls not supported" and a repair issue appears | Your equipment is VRV (P1P2) or single/multi-split (S21); Daikin rejects fan writes. | Disable the Fan circulation and Fan circulation speed entities. |
| A temperature change does not stick | Daikin queued the write and rejected it, or the schedule reasserted itself. | Keep the setpoint within min/max and the delta; the confirmation read 15 seconds later shows the truth. |

### Diagnostics

**Settings** → **Devices & Services** → **Daikin One** → the device → three-dot menu →
**Download diagnostics**. The file contains the polling configuration, the last error code,
the seconds remaining on the access token, and each thermostat's state.

Every identifying and secret value is redacted before the file is written: email, API key,
Integrator Token, any stored password, access tokens, `Authorization` and `Cookie` headers,
device ids, device names, and location names all appear as `**REDACTED**`. The diagnostics
file is safe to attach to a GitHub issue.

More detail, including a decision tree for unavailable entities, is in
[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

### Debug logging

```yaml
logger:
  default: warning
  logs:
    custom_components.daikinone: debug
```

Debug logs record only `METHOD path -> status`. Request bodies, response bodies, headers, and
tokens are never logged.

## Removing the integration

1. **Settings** → **Devices & Services** → **Daikin One** → three-dot menu → **Delete**. This
   removes the config entry with all its devices and entities.
2. If you installed via HACS, remove the repository from HACS and restart Home Assistant. For
   a manual install, delete `config/custom_components/daikinone/`.
3. Revoke the Integrator Token in the SkyportHome app (**SkyportCare** → **home
   integration**) by issuing a new token. Deleting the Home Assistant entry alone does not
   invalidate the token at Daikin.

To remove a single stale device, such as a thermostat you no longer own or an equipment
device left behind by a previous integration, open its device page and choose **Delete**.
Devices that vanish from your account are removed automatically at the next successful poll.

## Migrating from another integration

Coming from `zlangbert/ha-daikinone` or `apetrycki/daikinskyport`? Read
[docs/MIGRATION.md](docs/MIGRATION.md) first. The short version: `ha-daikinone` entries are
migrated in place, your climate, sensor, and select entity ids are preserved, and you are
prompted to reauthenticate with the API key and Integrator Token. `daikinskyport` uses a
different domain and needs a manual switch.

Architecture notes for contributors are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). The
integration tracks the Home Assistant integration quality scale at the platinum tier
(self-assessed; the rule-by-rule ledger is
[quality_scale.yaml](custom_components/daikinone/quality_scale.yaml)).

## Attribution and licensing

This repository is licensed under the [MIT License](LICENSE), Copyright (c) 2026 Steven Hsu.

It is an independent reimplementation against Daikin's public documentation. No code was
copied from any of the projects below.

| Project | License | What was taken |
| --- | --- | --- |
| [`zlangbert/ha-daikinone`](https://github.com/zlangbert/ha-daikinone) | No license published | Architecture inspiration only: the coordinator/entity layout and the unique-id scheme, reimplemented from documented behaviour so existing users keep their entity ids. No code reused. |
| [`apetrycki/daikinskyport`](https://github.com/apetrycki/daikinskyport) | No license published | Feature research only, used to build [docs/FEATURE_MATRIX.md](docs/FEATURE_MATRIX.md). No code reused. |
| [`jeffschubert/homebridge-daikin-oneplus`](https://github.com/jeffschubert/homebridge-daikin-oneplus) | Apache-2.0 | Scheduling and UX ideas: the background polling cadence, the post-write settle delay, and optimistic state with a coalesced confirmation read. No code reused. |

Daikin, Daikin One, Amana, and Goodman are trademarks of their respective owners. This project
is not affiliated with, endorsed by, or supported by Daikin.
