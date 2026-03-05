import json
import os

SETTINGS_FILE = os.environ.get(
    "GUILD_SETTINGS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "guild_settings.json")
)


def load_settings() -> dict:
    if not os.path.isfile(SETTINGS_FILE):
        return {}
    with open(SETTINGS_FILE, "r") as f:
        return json.load(f)


def save_settings(data: dict):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_allowed_channel(guild_id: str) -> str | None:
    settings = load_settings()
    guild = settings.get(guild_id, {})
    return guild.get("allowed_channel")


def set_allowed_channel(guild_id: str, channel_id: str):
    settings = load_settings()
    if guild_id not in settings:
        settings[guild_id] = {}
    settings[guild_id]["allowed_channel"] = channel_id
    save_settings(settings)


def get_bitrate(guild_id: str) -> int | None:
    settings = load_settings()
    guild = settings.get(guild_id, {})
    return guild.get("bitrate")


def set_bitrate(guild_id: str, kbps: int):
    settings = load_settings()
    if guild_id not in settings:
        settings[guild_id] = {}
    settings[guild_id]["bitrate"] = kbps
    save_settings(settings)


def get_admins(guild_id: str) -> list[str]:
    settings = load_settings()
    guild = settings.get(guild_id, {})
    return guild.get("admins", [])


def add_admin(guild_id: str, user_id: str):
    settings = load_settings()
    if guild_id not in settings:
        settings[guild_id] = {}
    admins = settings[guild_id].get("admins", [])
    if user_id not in admins:
        admins.append(user_id)
    settings[guild_id]["admins"] = admins
    save_settings(settings)


def remove_admin(guild_id: str, user_id: str):
    settings = load_settings()
    if guild_id not in settings:
        return
    admins = settings[guild_id].get("admins", [])
    if user_id in admins:
        admins.remove(user_id)
        settings[guild_id]["admins"] = admins
        save_settings(settings)
