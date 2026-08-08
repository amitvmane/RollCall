from exceptions import *
import logging
import Levenshtein
from datetime import datetime, timedelta
import pytz
import asyncio


def _bot():
    """Lazy import to avoid a circular dependency:
    bot_state → models → functions, so functions cannot import bot_state at module load."""
    from bot_state import bot
    return bot

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

def _ts():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

# FUNCTION TO RAISE RC ALREADY STARTED ERROR
# USELESS IN NEW FEATURE
def roll_call_already_started(message, manager):
    """Check if roll call already started - deprecated with multiple rollcalls"""
    try:
        rollcalls = manager.get_rollcalls(message.chat.id)
        if len(rollcalls) == 1:
            logging.info(f"[{_ts()}] Roll call with title {rollcalls[0].title} is still in progress")
            return False
        else:
            return True
    except Exception:
        logging.error(f"[{_ts()}] roll_call_already_started: unexpected error", exc_info=True)
        return True

# FUNCTION TO RAISE RC NOT STARTED ERROR
def roll_call_not_started(message, manager):
    """Check if any roll call is active"""
    try:
        rollcalls = manager.get_rollcalls(message.chat.id)
        if len(rollcalls) == 0:
            logging.info(f"[{_ts()}] Roll call is not active")
            return False
        else:
            return True
    except Exception:
        logging.error(f"[{_ts()}] roll_call_not_started: unexpected error", exc_info=True)
        return False

# FUNCTION TO RAISE NO ADMIN RIGHTS ERROR
async def admin_rights(message, manager):
    """Check if user has admin rights (if required)"""
    try:
        chat_id = message.chat.id

        if not manager.get_admin_rights(chat_id):
            return True

        if message.from_user is None:
            return False

        member = await _bot().get_chat_member(chat_id, message.from_user.id)
        if member.status not in ['administrator', 'creator']:
            logging.info(f"[{_ts()}] User {message.from_user.id} attempted admin-only command without permissions")
            return False

        return True

    except Exception as e:
        logging.error(f"[{_ts()}] Error checking admin rights: {e}")
        return False
    
# FUNCTION TO CHECK IF SHH/LOUDER IS ACTIVE
def send_list(message, manager):
    """Check if bot should send detailed lists (not in shh mode)"""
    chat_id = message.chat.id
    return not manager.get_shh_mode(chat_id)

# AUTOCOMPLETE TIMEZONE
def auto_complete_timezone(timezone):
    """Auto-complete timezone string using fuzzy matching"""
    try:
        # Parse input
        parts = timezone.split("/")
        if len(parts) < 2:
            return None
            
        continent = parts[0].lower()
        place = parts[-1].lower().replace(" ", "_")
        
        # Handle common aliases
        if place == 'india':
            place = 'calcutta'
        if place == 'argentina':
            place = 'buenos_aires'
        
        # Find best match
        best_match = None
        best_distance = float('inf')
        
        for tz in pytz.all_timezones:
            tz_parts = tz.split("/")
            
            # Check continent matches
            if tz_parts[0].lower() != continent:
                continue
            
            # Get the place part (could be 2nd or 3rd component)
            if len(tz_parts) == 2:
                tz_place = tz_parts[1].lower()
            elif len(tz_parts) == 3:
                tz_place = tz_parts[2].lower()
            else:
                continue
            
            # Calculate distance with threshold
            threshold = int(len(place) * 0.35)
            try:
                diff = Levenshtein.distance(place, tz_place, score_cutoff=threshold)
                
                if diff <= threshold and diff < best_distance:
                    best_distance = diff
                    best_match = tz
            except Exception:
                # Levenshtein.distance raises when distance exceeds the
                # score_cutoff. Use a typed catch — bare `except:` also
                # swallows KeyboardInterrupt and SystemExit, which we want
                # to propagate so the process can shut down cleanly.
                continue
        
        return best_match
    except Exception as e:
        logging.error(f"[{_ts()}] Error in auto_complete_timezone: {e}")
        return None


WEEKDAY_MAP = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _localize_safe(tz, naive_dt):
    """Localize a naive datetime, handling DST cutovers the same way
    check_reminders._ensure_aware does — see that function's docstring for
    the full rationale. Ambiguous fall-back times resolve to the EARLIER
    occurrence (is_dst=True); non-existent spring-forward times resolve to
    the post-jump equivalent (is_dst=False). Kept as a separate function
    (not a call to _ensure_aware) to avoid a functions.py -> check_reminders
    import cycle; must stay behaviorally in sync with it."""
    try:
        return tz.localize(naive_dt, is_dst=None)
    except pytz.AmbiguousTimeError:
        return tz.localize(naive_dt, is_dst=True)
    except pytz.NonExistentTimeError:
        logging.warning(
            f"[_localize_safe] non-existent local time {naive_dt.isoformat()} in {tz}; "
            "interpreting as standard-time equivalent (post-DST-jump)"
        )
        return tz.localize(naive_dt, is_dst=False)
    except Exception:
        logging.exception(f"[_localize_safe] unexpected localize failure for {naive_dt} in {tz}")
        return tz.localize(naive_dt, is_dst=False)


def get_next_weekday_datetime(tz, target_day: str, target_time: str):
    """Return next datetime in tz with given weekday name and HH:MM time."""
    target_idx = WEEKDAY_MAP.get(target_day.lower())
    if target_idx is None:
        return None
    now = datetime.now(tz)
    try:
        hour, minute = map(int, target_time.split(":"))
    except ValueError:
        return None

    # Do the day-arithmetic on a NAIVE datetime and localize fresh for
    # whichever date we land on — adding a timedelta directly to an
    # already-localized pytz datetime keeps the UTC offset from the
    # original date, which is wrong whenever the addition crosses a DST
    # boundary (the classic pytz footgun: pytz.normalize() would only
    # relabel that stale offset, not fix which instant it points to).
    naive_today = datetime(now.year, now.month, now.day, hour, minute)
    days_ahead = (target_idx - naive_today.weekday()) % 7
    candidate = _localize_safe(tz, naive_today + timedelta(days=days_ahead))
    if candidate < now:
        candidate = _localize_safe(tz, naive_today + timedelta(days=days_ahead + 7))
    return candidate


def format_local_datetime(dt: datetime, tzname: str = "Asia/Kolkata", fmt: str = "%A, %d %b at %H:%M %Z") -> str:
    """Format a datetime into `tzname` with an explicit abbreviation label
    (e.g. "IST", "UTC", "EDT") baked into the string via %Z, so the same
    instant never displays ambiguously depending on which call site rendered
    it. `dt` may be naive (treated as UTC — matches how scheduled_at/finalize
    timestamps are stored) or already tz-aware (converted as-is).

    This is the one shared place to format a user-facing date/time — every
    call site used to do its own ad-hoc pytz dance, and one of them (the
    scheduled-rollcall announcement) skipped the conversion entirely and
    showed a bare UTC time with no label, which is exactly the kind of bug
    this helper exists to prevent."""
    try:
        tz = pytz.timezone(tzname)
    except Exception:
        tz = pytz.timezone("Asia/Kolkata")
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    return dt.astimezone(tz).strftime(fmt)


def format_iso_utc_local(iso_str: str, tzname: str = "Asia/Kolkata", fmt: str = "%A, %d %b at %H:%M %Z") -> str:
    """Same as format_local_datetime, but takes a UTC ISO 8601 string (the
    format the web frontend sends via JS `Date.toISOString()`, e.g.
    "2026-07-24T05:10:00.000Z") instead of a datetime object."""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return iso_str
    return format_local_datetime(dt, tzname, fmt)


def weekly_minutes(day: str, time_str: str):
    """Return minutes since Monday 00:00 for a weekday + HH:MM pair, or None if invalid."""
    day_idx = WEEKDAY_MAP.get(day.lower())
    if day_idx is None:
        return None
    try:
        h, m = map(int, time_str.split(":"))
        if not (0 <= h < 24 and 0 <= m < 60):
            return None
    except ValueError:
        return None
    return day_idx * 24 * 60 + h * 60 + m
