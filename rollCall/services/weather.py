"""
Forecast heads-up for a game — information only, it never cancels anything.

Where: the rollcall's existing free-text location (/location). Exact
coordinates are used when the text has them ("12.97,77.59", or a maps link
with @lat,lon / q=lat,lon); otherwise the place name is looked up with
Open-Meteo's geocoder, restricted to the group's country (from its timezone)
so "Turf 3" can't resolve to another continent. Lookups are cached.

Sources are independent forecast MODELS, not apps — many weather apps resell
the same model, so five apps are not five opinions:
  * Open-Meteo: ECMWF, GFS and ICON in one request (keyless)
  * MET Norway Locationforecast (keyless; asks for an identifying User-Agent)
The verdict is agreement ("3 of 4 forecasts expect rain"), not an average,
which would hide a split.

Every outbound call goes to a fixed host with a short timeout; any failure
just drops that source. Nothing here raises to a caller for a network fault.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import pytz

import db
from services.common import game_time

RAIN_MM = 0.5            # mm in an hour that counts as "rain expected"
_FORECAST_TTL = 1800     # seconds a forecast is reused
_GEO_TTL = 7 * 86400     # seconds a resolved place is reused
_GEO_MISS_TTL = 86400    # retry an unresolvable place after a day
_TIMEOUT = 10
_USER_AGENT = "RollCall/10.9 github.com/amitvmane/RollCall"

_OPEN_METEO_MODELS = {
    "ecmwf_ifs025": "ECMWF",
    "gfs_seamless": "GFS",
    "icon_seamless": "ICON",
}

_forecast_cache: dict = {}

_COORD_RE = re.compile(r"(-?\d{1,2}\.\d{2,})\s*,\s*(-?\d{1,3}\.\d{2,})")


async def _fetch_json(url: str, params: dict) -> dict | None:
    """GET a JSON document. Patched in tests; the only network call here."""
    import aiohttp
    try:
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": _USER_AGENT}) as s:
            async with s.get(url, params=params) as r:
                if r.status != 200:
                    logging.warning("[weather] %s answered %s", url, r.status)
                    return None
                return await r.json(content_type=None)
    except Exception as exc:
        logging.warning("[weather] %s failed: %s", url, exc)
        return None


def _country_for_tz(tz_name: str) -> str | None:
    for cc, zones in pytz.country_timezones.items():
        if tz_name in zones:
            return cc.upper()
    return None


def parse_coordinates(text: str) -> tuple[float, float] | None:
    m = _COORD_RE.search(text or "")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return lat, lon
    return None


async def resolve_location(text: str, tz_name: str = "Asia/Kolkata") -> dict | None:
    """{"lat", "lon", "label"} for a location string, or None."""
    text = (text or "").strip()
    if not text:
        return None
    coords = parse_coordinates(text)
    if coords:
        return {"lat": coords[0], "lon": coords[1], "label": text}

    cc = _country_for_tz(tz_name)
    key = "geo:" + hashlib.sha1(f"{text.lower()}|{cc}".encode()).hexdigest()
    cached = db.get_system_config(key)
    if cached:
        try:
            c = json.loads(cached)
            ttl = _GEO_MISS_TTL if c.get("none") else _GEO_TTL
            if time.time() - c.get("at", 0) < ttl:
                return None if c.get("none") else {k: c[k] for k in ("lat", "lon", "label")}
        except (ValueError, KeyError):
            pass

    # Most specific first: the whole text, then each comma part, then the
    # longer single words ("Indiranagar Turf 3" -> "Indiranagar").
    parts = [p.strip() for p in text.split(",") if p.strip()]
    words = sorted({w for w in re.findall(r"[^\W\d_]{4,}", text)}, key=len, reverse=True)
    found, data = None, None
    for candidate in [text, *parts, *words][:8]:
        params = {"name": candidate, "count": 1, "language": "en", "format": "json"}
        if cc:
            params["countryCode"] = cc
        data = await _fetch_json("https://geocoding-api.open-meteo.com/v1/search", params)
        hit = ((data or {}).get("results") or [None])[0]
        if hit and "latitude" in hit and "longitude" in hit:
            label = ", ".join(x for x in (hit.get("name"), hit.get("admin1")) if x)
            found = {"lat": float(hit["latitude"]), "lon": float(hit["longitude"]), "label": label or candidate}
            break
    if data is None and found is None:
        return None  # network trouble, not "no such place" — don't cache the miss
    db.set_system_config(key, json.dumps({**(found or {"none": 1}), "at": time.time()}))
    return found


def _hour_index(times: list, target: datetime) -> list[int]:
    """Indexes of hourly slots within ±1h of target (times are UTC ISO)."""
    out = []
    for i, t in enumerate(times):
        try:
            dt = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if abs((dt - target).total_seconds()) <= 3600:
            out.append(i)
    return out


async def _open_meteo(lat: float, lon: float, when: datetime) -> tuple[list, int | None, float | None]:
    data = await _fetch_json("https://api.open-meteo.com/v1/forecast", {
        "latitude": round(lat, 4), "longitude": round(lon, 4),
        "hourly": "precipitation,precipitation_probability,temperature_2m",
        "models": ",".join(["best_match", *_OPEN_METEO_MODELS]),
        "timezone": "GMT", "forecast_days": 7,
    })
    hourly = (data or {}).get("hourly") or {}
    idx = _hour_index(hourly.get("time") or [], when)
    if not idx:
        return [], None, None

    def _at(key):
        series = hourly.get(key) or []
        return [series[i] for i in idx if i < len(series) and series[i] is not None]

    sources = []
    for model, label in _OPEN_METEO_MODELS.items():
        values = _at(f"precipitation_{model}")
        if values:
            sources.append({"name": label, "mm": round(max(values), 1)})
    prob = _at("precipitation_probability_best_match")
    temp = _at("temperature_2m_best_match")
    return sources, (max(prob) if prob else None), (round(temp[len(temp) // 2], 1) if temp else None)


async def _met_norway(lat: float, lon: float, when: datetime) -> list:
    data = await _fetch_json("https://api.met.no/weatherapi/locationforecast/2.0/compact",
                             {"lat": round(lat, 4), "lon": round(lon, 4)})
    series = ((data or {}).get("properties") or {}).get("timeseries") or []
    times = [s.get("time") for s in series]
    values = []
    for i in _hour_index(times, when):
        mm = (((series[i].get("data") or {}).get("next_1_hours") or {}).get("details") or {}).get("precipitation_amount")
        if mm is not None:
            values.append(mm)
    return [{"name": "MET Norway", "mm": round(max(values), 1)}] if values else []


async def forecast(lat: float, lon: float, when: datetime) -> dict | None:
    """Agreement across models for the hour of `when` (aware datetime)."""
    when = when.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    key = (round(lat, 2), round(lon, 2), when.isoformat())
    hit = _forecast_cache.get(key)
    if hit and time.time() - hit[0] < _FORECAST_TTL:
        return hit[1]
    om_sources, prob, temp = await _open_meteo(lat, lon, when)
    sources = om_sources + await _met_norway(lat, lon, when)
    if not sources:
        return None
    result = {
        "sources": sources,
        "rainy": sum(1 for s in sources if s["mm"] >= RAIN_MM),
        "total": len(sources),
        "max_mm": max(s["mm"] for s in sources),
        "probability": prob,
        "temperature": temp,
    }
    _forecast_cache[key] = (time.time(), result)
    for k in [k for k, v in _forecast_cache.items() if time.time() - v[0] >= _FORECAST_TTL]:
        _forecast_cache.pop(k, None)
    return result


def summary_line(result: dict) -> str:
    if result["rainy"]:
        head = f"🌧 {result['rainy']} of {result['total']} forecasts expect rain (up to {result['max_mm']:g} mm)"
    else:
        head = f"☀️ No rain expected — {result['total']} of {result['total']} forecasts agree"
    extras = []
    if result.get("probability") is not None:
        extras.append(f"chance {int(result['probability'])}%")
    if result.get("temperature") is not None:
        extras.append(f"{result['temperature']:g}°C")
    return head + (" · " + " · ".join(extras) if extras else "")


async def rollcall_weather(rc) -> dict:
    """Forecast for one rollcall: {"ok": bool, "text": str, "result"?: dict}.
    "text" explains what's missing when ok is False."""
    when = game_time(rc)
    if not getattr(rc, "location", None):
        return {"ok": False, "text": "No location set — add one with /location so I know where to look."}
    if when is None:
        return {"ok": False, "text": "No game time set — set one with /set_rollcall_time."}
    now = datetime.now(timezone.utc)
    if when < now:
        return {"ok": False, "text": "That game time has already passed."}
    if when - now > timedelta(days=6):
        return {"ok": False, "text": "The game is more than 6 days away — forecasts aren't useful yet."}
    place = await resolve_location(rc.location, rc.timezone or "Asia/Kolkata")
    if not place:
        return {"ok": False, "text": (f"Couldn't find '{rc.location}' on the map. Add the area or city "
                                      "to /location, or paste coordinates like 12.97,77.59.")}
    result = await forecast(place["lat"], place["lon"], when)
    if not result:
        return {"ok": False, "text": "Weather services aren't answering right now — try again later."}
    local = when.astimezone(pytz.timezone(rc.timezone or "Asia/Kolkata"))
    text = (f"Forecast for {rc.title} — {local.strftime('%a %d %b, %I:%M %p')} at {place['label']}\n"
            + summary_line(result))
    return {"ok": True, "text": text, "result": result}


def remember(rc_id: int, text: str) -> None:
    """Latest forecast line for a rollcall, shown on the web page."""
    db.set_system_config(f"weather_last:{rc_id}", json.dumps({"text": text, "at": time.time()}))


def last_for(rc_id: int) -> str | None:
    raw = db.get_system_config(f"weather_last:{rc_id}")
    try:
        return json.loads(raw)["text"] if raw else None
    except (ValueError, KeyError):
        return None


def set_alerts(chat_id: int, enabled: bool, admin_user_id: int, admin_name: str) -> dict:
    db.update_chat_settings(chat_id, weather_alerts=1 if enabled else 0)
    db.log_admin_action(chat_id, admin_user_id, admin_name, "weather_alerts", details="on" if enabled else "off")
    return {"weather_alerts": enabled}


def alerts_enabled(chat_id: int) -> bool:
    return bool(int(db.get_or_create_chat(chat_id).get("weather_alerts") or 0))
