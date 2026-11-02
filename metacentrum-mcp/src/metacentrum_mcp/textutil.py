"""Helpers that keep remote output small and readable for a model."""

from __future__ import annotations

import re

# CSI, OSC (terminated by BEL or ST), and lone two-byte escapes.
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_terminal(text: str) -> str:
    """Strip escape sequences and collapse carriage-return overwrites (progress bars)."""
    text = _ANSI_RE.sub("", text)
    text = text.replace("\r\n", "\n")
    lines = []
    for line in text.split("\n"):
        if "\r" in line:
            parts = [p for p in line.split("\r") if p]
            line = parts[-1] if parts else ""
        lines.append(_CTRL_RE.sub("", line))
    return "\n".join(lines)


def truncate_lines(text: str, head: int, tail: int, max_line_chars: int = 2000) -> tuple[str, bool]:
    """Keep the first `head` and last `tail` lines; clip very long lines."""
    lines = text.split("\n")
    clipped = False
    out = []
    for line in lines:
        if len(line) > max_line_chars:
            line = line[:max_line_chars] + f" …[{len(line) - max_line_chars} chars clipped]"
            clipped = True
        out.append(line)
    if len(out) > head + tail:
        omitted = len(out) - head - tail
        out = out[:head] + [f"…[{omitted} lines omitted]…"] + (out[-tail:] if tail else [])
        clipped = True
    return "\n".join(out), clipped


def looks_binary(data: bytes) -> bool:
    if not data:
        return False
    if b"\x00" in data[:8192]:
        return True
    try:
        data[:8192].decode("utf-8")
    except UnicodeDecodeError as e:
        # A multi-byte char cut at the sample boundary is not binary.
        return e.start < len(data[:8192]) - 4
    return False


def parse_walltime(value: str | int) -> int:
    """'HH:MM:SS', 'MM:SS', 'D:HH:MM:SS', '90m', '2h', '1d' or seconds -> seconds."""
    if isinstance(value, int):
        if value <= 0:
            raise ValueError("walltime must be positive")
        return value
    s = str(value).strip().lower()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhd])", s)
    if m:
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
        secs = int(float(m.group(1)) * mult)
    elif re.fullmatch(r"\d+", s):
        secs = int(s)
    elif re.fullmatch(r"\d+(:\d{1,2}){1,3}", s):
        parts = [int(p) for p in s.split(":")]
        while len(parts) < 4:
            parts.insert(0, 0)
        d, h, mi, se = parts
        secs = ((d * 24 + h) * 60 + mi) * 60 + se
    else:
        raise ValueError(f"cannot parse walltime {value!r}; use HH:MM:SS")
    if secs <= 0:
        raise ValueError("walltime must be positive")
    return secs


def format_walltime(secs: int) -> str:
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


_SIZE_UNITS = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4}


def parse_size(value: str | int | float) -> int:
    """'4gb', '4G', '500mb', '1.5GB', bare number = GB -> bytes."""
    if isinstance(value, (int, float)):
        return int(value * 1024**3)
    s = str(value).strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kmgt]?b?|[kmgt]i?b)?", s)
    if not m:
        raise ValueError(f"cannot parse size {value!r}; use e.g. 4gb or 500mb")
    num, unit = float(m.group(1)), (m.group(2) or "gb").replace("i", "")
    if unit in ("k", "m", "g", "t"):
        unit += "b"
    if unit not in _SIZE_UNITS:
        raise ValueError(f"unknown size unit in {value!r}")
    return int(num * _SIZE_UNITS[unit])


def format_size_pbs(nbytes: int) -> str:
    """PBS wants integers: prefer gb, then mb."""
    if nbytes % 1024**3 == 0:
        return f"{nbytes // 1024**3}gb"
    return f"{max(1, -(-nbytes // 1024**2))}mb"


def human_size(nbytes: int | None) -> str:
    if nbytes is None:
        return "?"
    n = float(nbytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n}"
