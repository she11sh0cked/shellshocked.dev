"""Feed Dedup: hide duplicate iptv-org feeds of the same channel.

Channels from iptv-org carry tvg-ids of the form ``Channel.cc@Feed``. Every
channel with more than one feed gets a dropdown in the plugin settings
("Automatic" or a specific feed). One feed per channel stays visible; the
others are hidden from all outputs (M3U, EPG, Xtream).
"""
import json
import logging
import os
import re
import time
import urllib.request
from collections import defaultdict

from celery import shared_task

FEEDS_URL = "https://iptv-org.github.io/api/feeds.json"
FEEDS_CACHE = "/data/feed_dedup_feeds.json"
STATE_FILE = "/data/feed_dedup_state.json"
TVG_RE = re.compile(r"^([^@\s]+)@(\S+)$")
AUTO = "auto"
DEFAULT_GRAVEYARD = "Graveyard"
DEFAULT_COUNTRY = "DE"
PLUGIN_KEY = "feed_dedup"
TASK_NAME = "feed_dedup_sync"
TASK_PATH = f"_dispatcharr_plugin_{PLUGIN_KEY}.plugin.feed_dedup_sync"
CRON = "25 * * * *"
FEEDS_MAX_AGE = 24 * 3600
LOGGER = logging.getLogger("plugins.feed_dedup")

_MANIFEST = json.load(open(os.path.join(os.path.dirname(__file__), "plugin.json"), encoding="utf-8"))


def _field_id(base):
    return "keep__" + base


def _read_feeds_cache():
    try:
        with open(FEEDS_CACHE, encoding="utf-8") as fh:
            return {(f["channel"], f["id"]): f for f in json.load(fh)}
    except Exception:
        return {}


def _load_feeds(logger):
    try:
        if time.time() - os.path.getmtime(FEEDS_CACHE) < FEEDS_MAX_AGE:
            return _read_feeds_cache()
    except OSError:
        pass
    try:
        with urllib.request.urlopen(FEEDS_URL, timeout=30) as r:
            data = r.read()
        json.loads(data)
        with open(FEEDS_CACHE + ".tmp", "wb") as fh:
            fh.write(data)
        os.replace(FEEDS_CACHE + ".tmp", FEEDS_CACHE)
    except Exception as exc:
        if logger:
            logger.warning("Feed Dedup: could not refresh feeds.json (%s), using cache", exc)
    return _read_feeds_cache()


def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return set(json.load(fh).get("hidden", []))
    except FileNotFoundError:
        return set()


def _held_by_others(own_file):
    """Channel ids another hiding plugin (Feed Dedup, Graveyard Hider) still wants hidden."""
    held = set()
    for f in ("/data/feed_dedup_state.json", "/data/graveyard_hider_state.json"):
        if f == own_file:
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                held |= set(json.load(fh).get("hidden", []))
        except (FileNotFoundError, ValueError):
            pass
    return held


def _save_state(ids):
    with open(STATE_FILE + ".tmp", "w", encoding="utf-8") as fh:
        json.dump({"hidden": sorted(ids)}, fh)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)


def _duplicate_groups():
    """{base_id: [(channel, feed_id), ...]} for every channel with 2+ feeds."""
    from apps.channels.models import Channel

    groups = defaultdict(list)
    for ch in Channel.objects.select_related("channel_group").order_by("channel_number"):
        m = TVG_RE.match(ch.tvg_id or "")
        if m:
            groups[m.group(1)].append((ch, m.group(2)))
    return {base: members for base, members in groups.items() if len(members) > 1}


def _is_dead(ch, graveyard):
    return bool(ch.channel_group and ch.channel_group.name == graveyard)


def _score(base, item, feeds, graveyard, country, pick):
    ch, feed_id = item
    feed = feeds.get((base, feed_id), {})
    return (
        not _is_dead(ch, graveyard),
        pick != AUTO and ch.tvg_id == pick,
        bool(feed.get("is_main")),
        country in (feed.get("broadcast_area") or []),
        -(ch.channel_number or 1e9),
    )


def _title(base, members, feeds):
    """Channel title without feed suffix or [tags], e.g. 'Rakuten TV Action Movies'."""
    names = []
    for ch, feed_id in members:
        n = re.sub(r"\s*\[[^\]]*\]", "", ch.name or "").strip()
        for suffix in (feeds.get((base, feed_id), {}).get("name"), feed_id):
            if suffix and n.casefold().endswith(" " + suffix.casefold()):
                n = n[: -len(suffix) - 1].strip()
                break
        names.append(n)
    return min(names, key=len) if names else base


def _num(ch):
    return f"{ch.channel_number:g}" if ch.channel_number is not None else "-"


def _arm_schedule(enabled=True):
    from core.scheduling import create_or_update_periodic_task, delete_periodic_task

    if enabled:
        create_or_update_periodic_task(TASK_NAME, TASK_PATH, cron_expression=CRON, enabled=True)
    else:
        delete_periodic_task(TASK_NAME)


@shared_task
def feed_dedup_sync():
    from django.db import close_old_connections
    from apps.plugins.models import PluginConfig

    try:
        close_old_connections()
        cfg = PluginConfig.objects.filter(key=PLUGIN_KEY).first()
        if not cfg or not cfg.enabled:
            return "disabled"
        return Plugin()._dedup(cfg.settings or {}, LOGGER, apply=True)["message"].splitlines()[0]
    finally:
        close_old_connections()


class Plugin:
    name = _MANIFEST["name"]
    version = _MANIFEST["version"]
    description = _MANIFEST["description"]
    actions = _MANIFEST["actions"]

    @property
    def fields(self):
        """Static fields plus one dropdown per channel that has duplicate feeds.

        Dispatcharr reads this when it loads the plugin, so new duplicates show
        up after a plugin reload or container restart.
        """
        fields = list(_MANIFEST["fields"])
        try:
            feeds = _read_feeds_cache()
            groups = _duplicate_groups()
        except Exception:
            return fields
        if not groups:
            return fields
        fields.append({
            "id": "_section_picks",
            "label": "Which feed stays",
            "type": "info",
            "description": "One dropdown per channel with several feeds. Automatic shows the feed it currently keeps: the first alive feed, preferring the iptv-org main feed, then a feed broadcast in the preferred country. A picked feed that is dead loses to an alive one until it works again. Save, then Preview and Apply.",
        })
        for base, members in sorted(groups.items(), key=lambda kv: kv[1][0][0].channel_number or 0):
            auto = max(members, key=lambda it: _score(base, it, feeds, DEFAULT_GRAVEYARD, "c/" + DEFAULT_COUNTRY, AUTO))[0]
            options = [{"value": AUTO, "label": f"Automatic → {_num(auto)} · {auto.name}"}]
            for ch, feed_id in members:
                tags = []
                if feeds.get((base, feed_id), {}).get("is_main"):
                    tags.append("main")
                if _is_dead(ch, DEFAULT_GRAVEYARD):
                    tags.append("dead")
                suffix = f" ({', '.join(tags)})" if tags else ""
                options.append({"value": ch.tvg_id, "label": f"{_num(ch)} · {ch.name}{suffix}"})
            fields.append({
                "id": _field_id(base),
                "label": _title(base, members, feeds),
                "type": "select",
                "default": AUTO,
                "options": options,
                "help_text": f"{len(members)} feeds of {base}",
            })
        return fields

    def run(self, action, params, context):
        logger = context.get("logger")
        settings = context.get("settings") or {}
        if action == "undo":
            _arm_schedule(False)
            return self._undo()
        if action == "preview":
            return self._dedup(settings, logger, apply=False)
        if action == "apply":
            result = self._dedup(settings, logger, apply=True)
            if (params or {}).get("event") is None:
                _arm_schedule(True)
                result["message"] += f"\nSchedule armed ({CRON})."
            return result
        return {"status": "error", "message": f"Unknown action {action}"}

    def _plan(self, settings, logger):
        graveyard = (settings.get("graveyard_group") or DEFAULT_GRAVEYARD).strip()
        country = "c/" + (settings.get("preferred_country") or DEFAULT_COUNTRY).strip().upper()
        feeds = _load_feeds(logger)

        should_hide = {}
        for base, members in _duplicate_groups().items():
            pick = settings.get(_field_id(base)) or AUTO

            keep = max(members, key=lambda it: _score(base, it, feeds, graveyard, country, pick))[0]
            for ch, _ in members:
                if ch.id != keep.id:
                    should_hide[ch.id] = (ch, keep)
        return should_hide

    def _dedup(self, settings, logger, apply):
        from apps.channels.models import Channel

        should_hide = self._plan(settings, logger)
        owned = _load_state()
        to_hide = [cid for cid, (ch, _) in should_hide.items() if not ch.hidden_from_output]
        held = _held_by_others(STATE_FILE)
        to_unhide = [cid for cid in owned if cid not in should_hide and cid not in held]

        lines = []
        for cid, (ch, keep) in sorted(should_hide.items(), key=lambda kv: kv[1][0].channel_number or 0):
            mark = "hide" if cid in to_hide else "hidden"
            lines.append(f"{mark}: {_num(ch)} {ch.name} (keeps {_num(keep)} {keep.name})")
        for ch in Channel.objects.filter(id__in=to_unhide):
            lines.append(f"unhide: {_num(ch)} {ch.name}")

        if apply:
            Channel.objects.filter(id__in=to_hide).update(hidden_from_output=True)
            Channel.objects.filter(id__in=to_unhide, hidden_from_output=True).update(hidden_from_output=False)
            # Claim every duplicate, also ones another plugin hid first, so they stay
            # hidden here when that plugin lets go (e.g. a feed leaves Graveyard).
            owned = (owned - set(to_unhide)) | set(should_hide)
            # Forget channels that were deleted or unhidden by someone else.
            still = set(Channel.objects.filter(id__in=owned, hidden_from_output=True).values_list("id", flat=True))
            _save_state(still)
            if logger:
                logger.info("Feed Dedup: hid %d, unhid %d, %d hidden by plugin", len(to_hide), len(to_unhide), len(still))

        verb = "Applied" if apply else "Preview"
        return {
            "status": "ok",
            "message": f"{verb}: {len(to_hide)} to hide, {len(to_unhide)} to unhide, {len(should_hide)} duplicates total.\n" + "\n".join(lines),
        }

    def _undo(self):
        from apps.channels.models import Channel

        owned = _load_state()
        n = Channel.objects.filter(id__in=owned - _held_by_others(STATE_FILE), hidden_from_output=True).update(hidden_from_output=False)
        _save_state(set())
        return {"status": "ok", "message": f"Unhid {n} channels."}
