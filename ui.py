# name: (fallback emoji, premium custom emoji id)
# Paste your premium emoji IDs below (get them with /emojiid). Empty id = normal emoji is used.
EMOJI = {
    "rocket": ("🚀", ""),
    "link": ("🔗", ""),
    "bolt": ("⚡", ""),
    "search": ("🔍", ""),
    "download": ("⬇️", ""),
    "upload": ("⬆️", ""),
    "video": ("🎬", ""),
    "save": ("💾", ""),
    "check": ("✅", ""),
    "cross": ("❌", ""),
    "clock": ("⏳", ""),
    "folder": ("📁", ""),
    "star": ("⭐", ""),
}


def e(name: str) -> str:
    """Premium emoji (HTML) if an id is set, else the plain fallback emoji."""
    fallback, cid = EMOJI[name]
    return f'<tg-emoji emoji-id="{cid}">{fallback}</tg-emoji>' if cid else fallback


def eid(name: str) -> str:
    return EMOJI[name][1]


def human(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def bar(done, total, width=12):
    pct = (done / total) if total else 0
    filled = int(pct * width)
    return "█" * filled + "░" * (width - filled), pct * 100


def trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"
