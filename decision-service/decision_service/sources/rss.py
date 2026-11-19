"""Tolerant RSS parsing: ElementTree first, a regex fallback for malformed XML."""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET

_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)
_FIELD_RE = re.compile(r"<([A-Za-z][\w:.-]*)>(.*?)</\1>", re.S)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_rss(body: bytes) -> tuple[dict[str, str], list[dict[str, str]]]:
    """Return (channel fields, list of item fields). Only leaf elements are kept."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return _parse_with_regex(body.decode("utf-8", errors="replace"))

    channel = root.find("channel")
    if channel is None:
        channel = root
    meta = {
        _local(child.tag): (child.text or "").strip()
        for child in channel
        if child.tag != "item" and len(child) == 0
    }
    items = [
        {_local(field.tag): (field.text or "").strip() for field in item}
        for item in channel.iter("item")
    ]
    return meta, items


def _parse_with_regex(text: str) -> tuple[dict[str, str], list[dict[str, str]]]:
    head = text.split("<item>", 1)[0]
    meta = {
        match.group(1): html.unescape(match.group(2).strip())
        for match in _FIELD_RE.finditer(head)
        if "<" not in match.group(2)
    }
    items = [
        {match.group(1): html.unescape(match.group(2).strip()) for match in _FIELD_RE.finditer(block)}
        for block in _ITEM_RE.findall(text)
    ]
    return meta, items
