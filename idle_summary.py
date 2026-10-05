#!/usr/bin/env python3
"""
Weekly idle-alert summary for a Telegram group.

Reads last week's idle notifications from a Telegram group, for example:
    09/24 10:58: Asset: T117 was idling for 480 seconds in Geofence(s): 2B
works out the weekly stats and posts an "A look at last week" message.

Modes
  Live (default)  Reads each client's channel through a Telegram user account
                  (Telethon) and posts one summary per client. Settings come
                  from environment variables (GitHub secrets). See README.md.
  Export          python idle_summary.py --from-export result.json
                  Reads a Telegram Desktop JSON export instead and only prints
                  the summary. Good for testing with no Telegram login at all.

Useful flags
  --week-start YYYY-MM-DD   Summarise the 7 production days starting on this date
                            (each day runs DAY_START to DAY_START, default 07:30)
  --csv alerts.csv          Also write the parsed alerts to a CSV for checking in Excel
  --dry-run                 Print the summary without posting it
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Matches: "09/24 10:58: Asset: T117 was idling for 480 seconds in Geofence(s): 2B"
# finditer is used, so a message holding several alerts is also handled.
ALERT_RE = re.compile(
    r"(?P<month>\d{1,2})/(?P<day>\d{1,2})\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*:\s*"
    r"Asset:\s*(?P<asset>\S+)\s+was\s+idling\s+for\s+(?P<seconds>\d+)\s+seconds?"
    r"\s+in\s+Geofence\(s\):[ \t]*(?P<geofences>[^\n]*)",
    re.IGNORECASE,
)

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@dataclass
class Alert:
    when: datetime          # local time of the alert
    asset: str
    seconds: int
    geofences: tuple[str, ...]


# --------------------------------------------------------------------------- settings

def setting(name: str, default: str | None = None, required: bool = False) -> str | None:
    """Read a setting from the environment. Empty values count as missing."""
    value = os.environ.get(name, "").strip()
    if not value:
        if required:
            raise SystemExit(f"Missing required setting: {name} (add it as a GitHub secret)")
        return default
    return value


def truthy(value: str | None) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def parse_chat(value: str) -> int | str:
    """Chat IDs like -1001234567890 become ints; @usernames and links stay strings."""
    value = value.strip()
    return int(value) if re.fullmatch(r"-?\d+", value) else value


def clock_minutes(value) -> int:
    """'07:30' -> 450 minutes after midnight. A plain number is hours ('6' -> 06:00)."""
    v = str(value).strip()
    if ":" in v:
        h, m = v.split(":", 1)
        return (int(h) * 60 + int(m)) % 1440
    return int(float(v) * 60) % 1440


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def time_settings(item: dict | None = None) -> dict:
    """Day boundary and shift times, from a client entry or the global settings.

    DAY_START        when a production day begins (default 07:30), so e.g. "Wednesday"
                     means Wednesday 07:30 to Thursday 07:30.
    DAY_SHIFT_START  default: same as DAY_START
    NIGHT_SHIFT_START default: 12 hours after the day shift starts
    """
    item = item or {}
    day_start = clock_minutes(item.get("day_start") or setting("DAY_START", "07:30"))
    day_shift = clock_minutes(item.get("day_shift_start") or setting("DAY_SHIFT_START", hhmm(day_start)))
    night_shift = clock_minutes(item.get("night_shift_start")
                                or setting("NIGHT_SHIFT_START", hhmm((day_shift + 720) % 1440)))
    return {"day_offset": day_start, "day_start": day_shift, "night_start": night_shift}


def production_date(when: datetime, offset: int) -> date:
    """The production day an alert belongs to (an alert at 03:00 Thursday is Wednesday's)."""
    return (when - timedelta(minutes=offset)).date()


def is_day_shift(when: datetime, day_start: int, night_start: int) -> bool:
    t = when.hour * 60 + when.minute
    if day_start < night_start:
        return day_start <= t < night_start
    return t >= day_start or t < night_start


# --------------------------------------------------------------------------- parsing

def parse_message(text: str, sent_at: datetime, tz: ZoneInfo) -> list[Alert]:
    sent_local = sent_at.astimezone(tz)
    alerts = []
    for m in ALERT_RE.finditer(text):
        month, day = int(m["month"]), int(m["day"])
        # The message has no year, so borrow it from the send date (handles New Year).
        year = sent_local.year
        if month == 12 and sent_local.month == 1:
            year -= 1
        try:
            when = datetime(year, month, day, int(m["hour"]), int(m["minute"]), tzinfo=tz)
        except ValueError:
            when = sent_local
        if abs(when - sent_local) > timedelta(days=2):
            when = sent_local  # the text timestamp looks wrong; trust Telegram's send time

        # The feed ends each list with ")" and sometimes sends an empty list: "Geofence(s): )"
        raw = m["geofences"].strip().rstrip(".").rstrip(")").strip()
        fences = tuple(sorted({g.strip() for g in re.split(r"[,;]", raw) if g.strip()})) or ("No geofence",)
        alerts.append(Alert(when, m["asset"].rstrip(".,"), int(m["seconds"]), fences))
    return alerts


def parse_rows(rows: list[tuple[datetime, str]], tz: ZoneInfo) -> tuple[list[Alert], list[str]]:
    alerts, unparsed, seen = [], [], set()
    for sent_at, text in rows:
        found = parse_message(text, sent_at, tz)
        if not found and text.strip():
            unparsed.append(text)
        for a in found:
            key = (a.asset, a.when, a.seconds, a.geofences)
            if key in seen:  # the feed occasionally posts the same alert twice
                continue
            seen.add(key)
            alerts.append(a)
    return alerts, unparsed


# --------------------------------------------------------------------------- data sources

def load_export(path: str) -> list[tuple[datetime, str]]:
    """Read a Telegram Desktop export (Export chat history -> JSON -> result.json)."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    rows = []
    for m in data.get("messages", []):
        if m.get("type") != "message":
            continue
        text = m.get("text", "")
        if isinstance(text, list):  # formatted messages are stored as a list of parts
            text = "".join(p if isinstance(p, str) else p.get("text", "") for p in text)
        if "date_unixtime" in m:
            sent = datetime.fromtimestamp(int(m["date_unixtime"]), tz=timezone.utc)
        else:
            sent = datetime.fromisoformat(m["date"]).astimezone(timezone.utc)
        rows.append((sent, text))
    return rows


async def connect_client():
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(
        StringSession(setting("TG_SESSION", required=True)),
        int(setting("TG_API_ID", required=True)),
        setting("TG_API_HASH", required=True),
    )
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise SystemExit(
            "The Telegram session is not logged in (expired or ended). "
            "Run generate_session.py again and update the TG_SESSION secret."
        )
    await client.get_dialogs()  # fills Telethon's cache so numeric group IDs resolve
    return client


async def fetch_messages(client, chat, since_utc: datetime, until_utc: datetime):
    """Messages sent between since_utc and until_utc, plus whether the channel has
    history going back that far (so a week-on-week comparison is fair)."""
    rows, reached_start = [], False
    async for msg in client.iter_messages(chat, offset_date=until_utc):  # newest first
        if msg.date < since_utc:
            reached_start = True
            break
        if msg.message:
            rows.append((msg.date, msg.message))
    return rows, reached_start


# --------------------------------------------------------------------------- summary

def merged_idle_seconds(alerts: list[Alert]) -> dict[str, int]:
    """Idle seconds per asset, merging overlapping alerts so time isn't counted twice.

    Each alert covers (alert time - reported seconds) up to the alert time. When a
    device reconnects after being offline, several overlapping alerts for the same
    stop can arrive at once; merging stops those from inflating the total.
    """
    by_asset: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    for a in alerts:
        by_asset[a.asset].append((a.when - timedelta(seconds=a.seconds), a.when))
    result = {}
    for asset, spans in by_asset.items():
        spans.sort()
        total = 0.0
        start, end = spans[0]
        for s, e in spans[1:]:
            if s <= end:
                end = max(end, e)
            else:
                total += (end - start).total_seconds()
                start, end = s, e
        total += (end - start).total_seconds()
        result[asset] = round(total)
    return result


def location(a: Alert) -> str:
    return ", ".join(a.geofences)


def esc(value: str) -> str:
    return html.escape(value, quote=False)


def duration(seconds: int) -> str:
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


def pct(part: int, total: int) -> str:
    return f"{round(100 * part / total)}%" if total else "0%"


def trend(current: int, previous: int) -> str:
    if previous == 0:
        return ""
    change = (current - previous) / previous * 100
    if round(change) == 0:
        return " (no change vs previous week)"
    arrow = "▲" if change > 0 else "▼"
    return f" ({arrow} {abs(change):.0f}% vs previous week)"


def change_pct(current: float, previous: float) -> float:
    return (current - previous) / previous * 100


def comparison_section(this_secs: int, this_count: int, prev_alerts: list[Alert]) -> list[str]:
    """'Has your idling gone down?' block, comparing idle time and alerts with the previous 7 days.

    Left out when there's no previous-week data to compare against.
    """
    if not prev_alerts:
        return []
    prev_secs = sum(merged_idle_seconds(prev_alerts).values())
    prev_count = len(prev_alerts)
    time_change = change_pct(this_secs, prev_secs)
    count_change = change_pct(this_count, prev_count)

    if round(time_change) < 0:
        verdict = f"✅ <b>Yes</b> – idle time is down <b>{abs(time_change):.0f}%</b>"
    elif round(time_change) > 0:
        verdict = f"⚠️ <b>No</b> – idle time is up <b>{time_change:.0f}%</b>"
    else:
        verdict = "➖ <b>About the same</b> – idle time is unchanged"

    def arrow(change: float) -> str:
        if round(change) == 0:
            return "no change"
        return f"{'▼' if change < 0 else '▲'} {abs(change):.0f}%"

    return [
        "",
        "<b>Has your idling gone down?</b>",
        f"{verdict} compared with the previous week.",
        f"⏱ Idle time: {duration(this_secs)} vs {duration(prev_secs)} ({arrow(time_change)})",
        f"🔔 Idle alerts: {this_count} vs {prev_count} ({arrow(count_change)})",
    ]


def period_label(start: datetime, end: datetime) -> str:
    """e.g. 'Fri 18 Sep 07:30 – Fri 25 Sep 07:30'"""
    def fmt(d: datetime) -> str:
        return f"{d:%a} {d.day} {d:%b} {d:%H:%M}"
    return f"{fmt(start)} – {fmt(end)}"


def build_message(alerts: list[Alert], prev_alerts: list[Alert], week_start: datetime,
                  site: str, times: dict) -> str:
    day_start, night_start, offset = times["day_start"], times["night_start"], times["day_offset"]

    period = period_label(week_start, week_start + timedelta(days=7))
    subtitle = f"{esc(site)} · {period}" if site else period
    lines = ["📊 <b>A look at last week</b>", f"<i>{subtitle}</i>", ""]

    if not alerts:
        lines.append("✅ No idle alerts were raised last week.")
        return "\n".join(lines)

    total = len(alerts)
    secs_by_asset = merged_idle_seconds(alerts)
    total_secs = sum(secs_by_asset.values())

    by_asset = Counter(a.asset for a in alerts)
    by_fence = Counter(location(a) for a in alerts)
    by_hour = Counter(a.when.hour for a in alerts)
    by_day = Counter(production_date(a.when, offset).weekday() for a in alerts)
    day_shift = sum(1 for a in alerts if is_day_shift(a.when, day_start, night_start))

    top_asset, top_asset_n = by_asset.most_common(1)[0]
    top_fence, top_fence_n = by_fence.most_common(1)[0]
    peak_hour, peak_hour_n = by_hour.most_common(1)[0]
    peak_day, peak_day_n = by_day.most_common(1)[0]

    lines.append(f"🔔 Idle alerts: <b>{total}</b>")
    # Alerts report idle time up to the moment they fire, not the full stop, so this is a minimum.
    lines.append(f"⏱ Idle time: <b>at least {duration(total_secs)}</b>")
    lines.append(f"🚜 Assets with alerts: <b>{len(by_asset)}</b>")
    lines.append(
        f"🥇 Most alerts: <b>{esc(top_asset)}</b> – {top_asset_n} alerts "
        f"({duration(secs_by_asset[top_asset])})"
    )
    lines.append(f"📍 Top location: <b>{esc(top_fence)}</b> – {pct(top_fence_n, total)} of alerts")
    lines.append(f"🕐 Busiest hour: {peak_hour:02d}:00–{(peak_hour + 1) % 24:02d}:00 ({peak_hour_n} alerts)")
    lines.append(f"📅 Busiest day: {DAYS[peak_day]} ({peak_day_n} alerts)")
    lines.append(f"☀️ Day shift {pct(day_shift, total)} · 🌙 Night shift {pct(total - day_shift, total)}")

    lines += comparison_section(total_secs, total, prev_alerts)

    lines += ["", "<b>Top 5 assets</b>"]
    for i, (asset, n) in enumerate(by_asset.most_common(5), 1):
        lines.append(f"{i}. {esc(asset)} – {n} alert{'s' if n != 1 else ''} · {duration(secs_by_asset[asset])}")

    if len(by_fence) > 1:
        lines += ["", "<b>Top locations</b>"]
        for i, (fence, n) in enumerate(by_fence.most_common(3), 1):
            lines.append(f"{i}. {esc(fence)} – {n} ({pct(n, total)})")

    return "\n".join(lines)


def write_csv(path: str, alerts: list[Alert], times: dict) -> None:
    delimiter = setting("CSV_DELIMITER", ";")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=delimiter)
        w.writerow(["Date", "Time", "Production day", "Weekday", "Asset", "Seconds", "Geofences", "Shift"])
        for a in sorted(alerts, key=lambda a: a.when):
            shift = "Day" if is_day_shift(a.when, times["day_start"], times["night_start"]) else "Night"
            pday = production_date(a.when, times["day_offset"])
            w.writerow([a.when.strftime("%Y-%m-%d"), a.when.strftime("%H:%M"), pday.isoformat(),
                        DAYS[pday.weekday()], a.asset, a.seconds, ", ".join(a.geofences), shift])
    print(f"Wrote {len(alerts)} alerts to {path}")


# --------------------------------------------------------------------------- posting

def send_via_bot(token: str, chat_id: int | str, text: str) -> None:
    payload = json.dumps({
        "chat_id": chat_id, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload, headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.load(resp)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Bot could not post: {e.code} {e.read().decode(errors='replace')}")
    if not result.get("ok"):
        raise SystemExit(f"Bot could not post: {result}")


async def post(text: str, client, target: int | str) -> None:
    token = setting("BOT_TOKEN")
    if token:
        send_via_bot(token, target, text)
    else:
        entity = await client.get_entity(target)
        await client.send_message(entity, text, parse_mode="html", link_preview=False)


# --------------------------------------------------------------------------- main

def resolve_week_start(value: str | None, tz: ZoneInfo, offset: int) -> datetime:
    """Start of the 7 production days to summarise (each day starts at DAY_START).

    Default: the 7 complete production days before the current one. Run on Friday
    at 07:30 or later, that is Friday 07:30 a week ago up to this Friday 07:30.
    With a date given, the period starts on that date at DAY_START.
    """
    if value:
        d = date.fromisoformat(value)
    else:
        d = production_date(datetime.now(tz), offset) - timedelta(days=7)
    return datetime(d.year, d.month, d.day, tzinfo=tz) + timedelta(minutes=offset)


def load_clients() -> list[dict]:
    """Client list from the CLIENTS secret (JSON), or a single client from
    SOURCE_CHAT / TARGET_CHAT / SITE_NAME if CLIENTS isn't set."""
    raw = setting("CLIENTS")
    if raw:
        try:
            items = json.loads(raw)
        except json.JSONDecodeError as e:
            raise SystemExit(f"The CLIENTS secret isn't valid JSON ({e.msg}, line {e.lineno}). "
                             "Check quotes, commas and brackets against the README example.")
        if isinstance(items, dict):
            items = [items]
    else:
        items = [{"name": setting("SITE_NAME", ""),
                  "source": setting("SOURCE_CHAT", required=True),
                  "target": setting("TARGET_CHAT", required=True)}]
    clients = []
    for i, item in enumerate(items, 1):
        missing = [k for k in ("source", "target") if not str(item.get(k, "")).strip()]
        if missing:
            raise SystemExit(f"Client {i} in CLIENTS is missing: {', '.join(missing)}")
        clients.append({
            "name": str(item.get("name", "")).strip(),
            "source": parse_chat(str(item["source"])),
            "target": parse_chat(str(item["target"])),
            "times": time_settings(item),
        })
    return clients


def summarise(rows, cfg: dict, tz: ZoneInfo, week_start: datetime, csv_path: str | None,
              history_complete: bool) -> str:
    week_end = week_start + timedelta(days=7)
    prev_start = week_start - timedelta(days=7)
    alerts, unparsed = parse_rows(rows, tz)
    week = [a for a in alerts if week_start <= a.when < week_end]
    prev = [a for a in alerts if prev_start <= a.when < week_start]
    if not history_complete:
        print("Note: the data doesn't cover the whole previous week, so the comparison is left out.")
        prev = []

    print(f"Messages read: {len(rows)} | alerts parsed: {len(alerts)} | unparsed messages: {len(unparsed)}")
    print(f"Alerts this week: {len(week)} | previous week: {len(prev)}")
    if week:
        print("Most common reported durations (seconds, count):",
              Counter(a.seconds for a in week).most_common(5))
    for text in unparsed[:3]:
        print("  Unparsed example:", text[:120].replace("\n", " "))

    if not rows:
        raise SystemExit("No messages found. Check the source channel ID and that the account has joined it.")
    if not alerts:
        raise SystemExit("Messages were found but none matched the idle-alert format. "
                         "The notification wording may have changed; see the unparsed examples above.")
    if csv_path:
        write_csv(csv_path, week, cfg["times"])

    text = build_message(week, prev, week_start, cfg["name"], cfg["times"])
    print("\n----- SUMMARY -----\n" + text + "\n-------------------\n")
    return text


async def run(args) -> None:
    tz = ZoneInfo(setting("TIMEZONE", "Africa/Johannesburg"))
    offset = time_settings()["day_offset"]
    week_start = resolve_week_start(args.week_start or setting("WEEK_START"), tz, offset)
    week_end = week_start + timedelta(days=7)
    prev_start = week_start - timedelta(days=7)
    print(f"Period: {week_start:%a %Y-%m-%d %H:%M} to {week_end:%a %Y-%m-%d %H:%M} ({tz.key})")

    # Export mode: one file, one client, print only.
    if args.from_export:
        cfg = {"name": args.site or setting("SITE_NAME", ""),
               "times": time_settings()}
        rows = load_export(args.from_export)
        history_complete = any(d < prev_start.astimezone(timezone.utc) for d, _ in rows)
        summarise(rows, cfg, tz, week_start, args.csv, history_complete)
        print("Export mode: nothing was posted.")
        return

    dry_run = args.dry_run or truthy(setting("DRY_RUN", "false"))
    clients = load_clients()
    failures = []
    tg = await connect_client()
    try:
        for cfg in clients:
            label = cfg["name"] or str(cfg["source"])
            print(f"\n========== {label} ==========")
            try:
                source = await tg.get_entity(cfg["source"])
                # a day of margin either side, since alert times and send times can differ slightly
                rows, history_complete = await fetch_messages(
                    tg, source,
                    (prev_start - timedelta(days=1)).astimezone(timezone.utc),
                    (week_end + timedelta(days=1)).astimezone(timezone.utc),
                )
                text = summarise(rows, cfg, tz, week_start, None, history_complete)
                if dry_run:
                    print("Dry run: nothing was posted.")
                else:
                    await post(text, tg, cfg["target"])
                    print("Summary posted.")
            except (Exception, SystemExit) as e:  # keep going so one client can't block the others
                print(f"FAILED for {label}: {e}")
                failures.append(label)
    finally:
        await tg.disconnect()

    if failures:
        raise SystemExit(f"Summary failed for: {', '.join(failures)} (see the log above). "
                         "The other clients were processed normally.")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # emoji on Windows consoles
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Weekly idle-alert summary for Telegram")
    parser.add_argument("--from-export", help="Path to a Telegram Desktop result.json export (prints only)")
    parser.add_argument("--week-start", help="First production day of the 7 to summarise, YYYY-MM-DD (default: the 7 complete days before now)")
    parser.add_argument("--csv", help="Export mode: also write this week's parsed alerts to a CSV file")
    parser.add_argument("--site", help="Export mode: client name to show in the summary")
    parser.add_argument("--dry-run", action="store_true", help="Print the summary without posting")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
