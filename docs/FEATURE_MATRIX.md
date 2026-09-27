# Feature matrix

How this integration compares with the three community projects that use Daikin's **private**
Skyport API, and why each difference exists.

Legend for the **Status** column — every legacy feature carries exactly one:

| Status | Meaning |
| --- | --- |
| **Supported by official API** | Daikin's Open API exposes it and this integration implements it. |
| **Mapped differently in Home Assistant** | The capability exists, but it surfaces as a different entity/model than in the legacy project. |
| **Not exposed by official API** | No documented endpoint or field carries this data. It cannot be implemented without the private API. |
| **Deferred** | The API supports it, but it is not implemented in this release. |
| **Removed because unsafe or undocumented** | Reachable only through undocumented/private behaviour that Daikin does not support, so it is deliberately not implemented. |

Columns: **HAD** = `zlangbert/ha-daikinone`, **DSP** = `apetrycki/daikinskyport`,
**HB** = `jeffschubert/homebridge-daikin-oneplus`, **This** = this integration.
`✓` provided, `—` not provided.

## Climate control

| Feature | HAD | DSP | HB | This | Status |
| --- | :-: | :-: | :-: | :-: | --- |
| HVAC modes off / heat / cool / auto | ✓ | ✓ | ✓ | ✓ | Supported by official API |
| Emergency (auxiliary) heat mode | ✓ | ✓ | ✓ | ✓ | Mapped differently in Home Assistant — `HVACMode.HEAT` plus the preset `emergency_heat`, matching ha-daikinone, and offered only when `modeEmHeatAvailable` is set |
| Mode limits (heat-only / cool-only systems) | ✓ | ✓ | — | ✓ | Supported by official API — `modeLimit` filters the offered `hvac_modes` |
| HVAC action (heating / cooling / drying / fan / idle) | ✓ | ✓ | ✓ | ✓ | Supported by official API — from `equipmentStatus`; undocumented values map to unknown |
| Single setpoint (heat or cool) | ✓ | ✓ | ✓ | ✓ | Supported by official API |
| Auto / heat_cool setpoint range | ✓ | ✓ | ✓ | ✓ | Supported by official API — `setpointDelta` is validated locally before the write |
| Setpoint minimum / maximum limits | ✓ | ✓ | ✓ | ✓ | Supported by official API |
| Turn thermostat on / off | ✓ | ✓ | ✓ | ✓ | Supported by official API |
| Fahrenheit display | ✓ | ✓ | ✓ | ✓ | Mapped differently in Home Assistant — the API is Celsius-only; Home Assistant converts for display and back for writes |

## Sensors

| Feature | HAD | DSP | HB | This | Status |
| --- | :-: | :-: | :-: | :-: | --- |
| Indoor temperature | ✓ | ✓ | ✓ | ✓ | Supported by official API |
| Indoor humidity | ✓ | ✓ | ✓ | ✓ | Supported by official API |
| Outdoor temperature | ✓ | ✓ | ✓ | ✓ | Supported by official API |
| Outdoor humidity | ✓ | ✓ | ✓ | ✓ | Supported by official API |
| System fan state (auto / on) | ✓ | ✓ | ✓ | ✓ | Mapped differently in Home Assistant — a diagnostic enum sensor, not a climate `fan_mode`, because the official API exposes `fan` read-only |
| Thermostat online status | ✓ (`sensor.*_online`, enum) | ✓ | ✓ | ✓ | Mapped differently in Home Assistant — a `binary_sensor` with device class connectivity; the old enum `sensor` is not recreated |
| Geofencing enabled | — | ✓ | — | ✓ | Supported by official API — read-only diagnostic binary sensor |
| Equipment running booleans (heating/cooling/dehumidifying/fan) | partial | ✓ | ✓ | ✓ | Mapped differently in Home Assistant — diagnostic binary sensors derived from `equipmentStatus`, disabled by default |

## Schedules, holds and presence

| Feature | HAD | DSP | HB | This | Status |
| --- | :-: | :-: | :-: | :-: | --- |
| Enable / disable the thermostat schedule | ✓ | ✓ | ✓ | ✓ | Supported by official API — `PUT /v1/devices/{id}/schedule` |
| Read or edit schedule periods (times, setpoints, days) | — | ✓ | ✓ | — | Not exposed by official API — schedule contents are neither readable nor writable |
| Temporary hold (hold until next period / timed) | ✓ | ✓ | ✓ | — | Not exposed by official API — there is no hold object; a `/msp` write simply turns the schedule off |
| Permanent hold | ✓ | ✓ | ✓ | — | Not exposed by official API — same reason; the closest equivalent is switching the schedule off and setting a setpoint |
| Away / vacation mode control | ✓ | ✓ | ✓ | — | Not exposed by official API — Away state is not readable and has no write endpoint; a `/msp` write clears it as a documented side effect |
| Geofencing state (read) | — | ✓ | — | ✓ | Supported by official API |
| Geofencing control (write) | — | — | — | — | Not exposed by official API |

## Fan and comfort features

| Feature | HAD | DSP | HB | This | Status |
| --- | :-: | :-: | :-: | :-: | --- |
| Fan circulation mode (off / always on / schedule) | ✓ | ✓ | ✓ | ✓ | Mapped differently in Home Assistant — a `select` entity (config category), not a climate `fan_mode`; unitary systems only |
| Fan circulation speed (low / medium / high) | ✓ (`{id}-fan_speed`) | ✓ | ✓ | ✓ | Mapped differently in Home Assistant — a `select` entity; the legacy unique id `{id}-fan_speed` is preserved |
| Fan-only HVAC mode | — | — | — | ✓ | Mapped differently in Home Assistant — composed as mode off + circulation always on; leaving it restores the prior circulation |
| Target humidity / humidifier setpoint | ✓ | ✓ | ✓ | — | Not exposed by official API |
| Dehumidification setpoint / overcool limit | — | ✓ | ✓ | — | Not exposed by official API |
| One Clean (timed high-speed purge) | — | ✓ | ✓ | — | Not exposed by official API |
| Night mode / quiet mode | — | ✓ | ✓ | — | Not exposed by official API |
| Efficiency (comfort vs. eco) priority | — | ✓ | ✓ | — | Not exposed by official API |
| Thermostat display brightness / lock | — | ✓ | — | — | Not exposed by official API |

## Equipment telemetry

Everything in this section came from the private Skyport device-data payload. The official API returns
thermostat state only — there is no equipment object in any documented response, so **none of it
can be reimplemented**, regardless of effort.

| Feature | HAD | DSP | HB | This | Status |
| --- | :-: | :-: | :-: | :-: | --- |
| Air handler / furnace demand % | ✓ | ✓ | — | — | Not exposed by official API |
| Outdoor unit (heat pump / AC) demand % | ✓ | ✓ | — | — | Not exposed by official API |
| Airflow (CFM) | ✓ | ✓ | — | — | Not exposed by official API |
| Blower / indoor fan RPM | ✓ | ✓ | — | — | Not exposed by official API |
| Power draw (W) | ✓ | ✓ | — | — | Not exposed by official API |
| Compressor speed (RPS) | ✓ | ✓ | — | — | Not exposed by official API |
| Inverter current / frequency | — | ✓ | — | — | Not exposed by official API |
| Refrigerant pressures (suction / discharge) | ✓ | ✓ | — | — | Not exposed by official API |
| Coil temperatures (indoor / outdoor / discharge / liquid) | ✓ | ✓ | — | — | Not exposed by official API |
| Heat-stage / cool-stage indication | ✓ | ✓ | — | — | Not exposed by official API |
| Defrost status | — | ✓ | — | — | Not exposed by official API |
| Runtime counters (heat / cool / fan hours) | ✓ | ✓ | — | — | Not exposed by official API |
| Energy consumption history | — | ✓ | — | — | Not exposed by official API |
| Separate HA devices per equipment unit (furnace, coil, heat pump) | ✓ | ✓ | — | — | Not exposed by official API — see [MIGRATION.md](MIGRATION.md); stale equipment devices from a previous install are removed automatically |
| Equipment model / serial / control software version | ✓ | ✓ | — | — | Not exposed by official API — only the *thermostat's* model and firmware are returned, by `GET /v1/devices` |

## Air quality, alerts and extras

| Feature | HAD | DSP | HB | This | Status |
| --- | :-: | :-: | :-: | :-: | --- |
| Indoor air quality score / AQI | — | ✓ | ✓ | — | Not exposed by official API |
| Particle (PM2.5) level | — | ✓ | ✓ | — | Not exposed by official API |
| VOC level | — | ✓ | ✓ | — | Not exposed by official API |
| Ozone level | — | ✓ | ✓ | — | Not exposed by official API |
| Outdoor air quality | — | ✓ | ✓ | — | Not exposed by official API |
| Fault / alert / diagnostic codes | ✓ | ✓ | — | — | Not exposed by official API |
| Filter / UV lamp / humidifier pad service reminders | — | ✓ | — | — | Not exposed by official API |
| Weather forecast from the thermostat | — | ✓ | — | — | Not exposed by official API |
| Thermostat name / model / firmware version | ✓ | ✓ | ✓ | ✓ | Supported by official API — from `GET /v1/devices`, surfaced as Home Assistant device info |
| Multiple locations on one account | ✓ | ✓ | ✓ | ✓ | Supported by official API — device names are prefixed with the location when an account has more than one |

## Authentication and platform

| Feature | HAD | DSP | HB | This | Status |
| --- | :-: | :-: | :-: | :-: | --- |
| Email + password login (private API) | ✓ | ✓ | ✓ | — | Removed because unsafe or undocumented — the private app login endpoint is undocumented, is being retired for SSO, and storing a Daikin account password is an unnecessary risk |
| Private Skyport device-data polling | ✓ | ✓ | ✓ | — | Removed because unsafe or undocumented |
| API key + Integrator Token (official) | — | — | — | ✓ | Supported by official API |
| Config flow with reauth and reconfigure | partial | partial | n/a | ✓ | Mapped differently in Home Assistant |
| Redacted diagnostics download | — | — | n/a | ✓ | Mapped differently in Home Assistant |
| Repair issue when fan writes are unsupported | — | — | — | ✓ | Mapped differently in Home Assistant |
| Configurable polling interval | ✓ | ✓ | ✓ | ✓ | Supported by official API — floor of 180 s, Daikin's documented limit |
| Read-only mode (validate + record writes, send nothing) | — | — | — | ✓ | Mapped differently in Home Assistant — an option; on by default |

## Deferred (possible, not in this release)

| Feature | Status | Note |
| --- | --- | --- |
| A `number` entity for the polling interval | Deferred | The options flow already covers it; a second control would only add write paths. |
| Exposing `setpointDelta` as a read-only sensor | Deferred | Currently used for validation only; add if users ask for it on a dashboard. |
| A service to force an immediate refresh | Deferred | `homeassistant.update_entity` already does this, and Daikin's three-minute limit discourages manual polling. |
