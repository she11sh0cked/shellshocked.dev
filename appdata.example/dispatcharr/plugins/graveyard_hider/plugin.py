"""Graveyard Hider: keep IPTV Checker's dead-channel group out of all outputs."""
import json
import logging
import os

from celery import shared_task

PLUGIN_KEY = "graveyard_hider"
STATE_FILE = "/data/graveyard_hider_state.json"
TASK_NAME = "graveyard_hider_sync"
TASK_PATH = f"_dispatcharr_plugin_{PLUGIN_KEY}.plugin.graveyard_hider_sync"
CRON = "20 * * * *"
DEFAULT_GROUP = "Graveyard"
LOGGER = logging.getLogger("plugins.graveyard_hider")

_MANIFEST = json.load(open(os.path.join(os.path.dirname(__file__), "plugin.json"), encoding="utf-8"))


def _load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return set(json.load(fh).get("hidden", []))
    except FileNotFoundError:
        return set()


def _save_state(ids):
    with open(STATE_FILE + ".tmp", "w", encoding="utf-8") as fh:
        json.dump({"hidden": sorted(ids)}, fh)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)


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


def _sync(settings, apply):
    from apps.channels.models import Channel

    group = (settings.get("group_name") or DEFAULT_GROUP).strip()
    owned = _load_state()
    in_group = Channel.objects.filter(channel_group__name=group)
    # Already hidden channels belong to whoever hid them; leave them alone.
    to_hide = list(in_group.filter(hidden_from_output=False).order_by("channel_number"))
    in_group_ids = set(in_group.values_list("id", flat=True))
    held = _held_by_others(STATE_FILE)
    to_unhide = list(Channel.objects.filter(id__in=owned - in_group_ids - held, hidden_from_output=True).order_by("channel_number"))

    if apply:
        Channel.objects.filter(id__in=[c.id for c in to_hide]).update(hidden_from_output=True)
        Channel.objects.filter(id__in=[c.id for c in to_unhide]).update(hidden_from_output=False)
        # Claim every channel in the group, also ones another plugin hid first.
        keep = (owned - {c.id for c in to_unhide} - (owned - in_group_ids)) | in_group_ids
        _save_state(set(Channel.objects.filter(id__in=keep, hidden_from_output=True).values_list("id", flat=True)))
        if to_hide or to_unhide:
            LOGGER.info("Graveyard Hider: hid %d, unhid %d", len(to_hide), len(to_unhide))

    lines = [f"hide: {c.channel_number:g} {c.name}" for c in to_hide]
    lines += [f"unhide: {c.channel_number:g} {c.name}" for c in to_unhide]
    verb = "Applied" if apply else "Preview"
    return f"{verb}: {len(to_hide)} to hide, {len(to_unhide)} to unhide.\n" + "\n".join(lines)


def _arm_schedule(enabled=True):
    from core.scheduling import create_or_update_periodic_task, delete_periodic_task

    if enabled:
        create_or_update_periodic_task(TASK_NAME, TASK_PATH, cron_expression=CRON, enabled=True)
    else:
        delete_periodic_task(TASK_NAME)


@shared_task
def graveyard_hider_sync():
    from django.db import close_old_connections
    from apps.plugins.models import PluginConfig

    try:
        close_old_connections()
        cfg = PluginConfig.objects.filter(key=PLUGIN_KEY).first()
        if not cfg or not cfg.enabled:
            return "disabled"
        return _sync(cfg.settings or {}, apply=True).splitlines()[0]
    finally:
        close_old_connections()


class Plugin:
    name = _MANIFEST["name"]
    version = _MANIFEST["version"]
    description = _MANIFEST["description"]
    fields = _MANIFEST["fields"]
    actions = _MANIFEST["actions"]

    def run(self, action, params, context):
        settings = context.get("settings") or {}
        if action == "preview":
            return {"status": "ok", "message": _sync(settings, apply=False)}
        if action == "apply":
            msg = _sync(settings, apply=True)
            _arm_schedule(True)
            return {"status": "ok", "message": msg + f"\nSchedule armed ({CRON})."}
        if action == "undo":
            from apps.channels.models import Channel

            n = Channel.objects.filter(id__in=_load_state() - _held_by_others(STATE_FILE), hidden_from_output=True).update(hidden_from_output=False)
            _save_state(set())
            _arm_schedule(False)
            return {"status": "ok", "message": f"Unhid {n} channels, schedule stopped."}
        return {"status": "error", "message": f"Unknown action {action}"}
