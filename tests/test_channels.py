"""Channel watch: URL parsing, filtering of a canned yt-dlp listing, seen tracking."""

from __future__ import annotations

import pytest
from clipstudio import channels as ch
from clipstudio.jobs import batch as bq

LISTING = {
    "channel": "TED",
    "entries": [
        {
            "id": "long1",
            "title": "A great talk",
            "duration": 900,
            "url": "https://www.youtube.com/watch?v=long1",
        },
        {
            "id": "short1",
            "title": "Quick",
            "duration": 30,
            "url": "https://www.youtube.com/shorts/short1",
        },
        {"id": "live1", "title": "Live now", "duration": None, "live_status": "is_live"},
        {"id": "long2", "title": "Sponsored: ad", "duration": 600},
        {"id": "huge", "title": "Marathon", "duration": 9000},
        {"title": "no id"},
    ],
}


def test_normalize():
    assert ch.normalize_channel_url("@TED") == "https://www.youtube.com/@TED/videos"
    assert ch.normalize_channel_url("https://www.youtube.com/@TED") == (
        "https://www.youtube.com/@TED/videos"
    )
    assert ch.normalize_channel_url("https://www.youtube.com/@TED/videos") == (
        "https://www.youtube.com/@TED/videos"
    )
    assert ch.normalize_channel_url("https://www.youtube.com/channel/UC123/featured") == (
        "https://www.youtube.com/channel/UC123/videos"
    )
    for bad in ("", "https://example.com/@x", "https://www.youtube.com/watch?v=abc"):
        with pytest.raises(ch.ChannelError):
            ch.normalize_channel_url(bad)


def test_parse_listing_filters():
    keep, skipped = ch.parse_listing(LISTING, ch.Filters())
    assert [c.id for c in keep] == ["long1", "long2", "huge"]
    assert set(skipped) == {"short1", "live1"}
    assert keep[1].url.endswith("watch?v=long2")

    keep, skipped = ch.parse_listing(
        LISTING,
        ch.Filters(max_duration_s=1800, exclude_keywords=["sponsored"], include_keywords=["talk"]),
    )
    assert [c.id for c in keep] == ["long1"]


def test_add_baselines_then_enqueues_only_new(initialised_db):
    lister_data = {"v": LISTING}
    lister = lambda url: lister_data["v"]  # noqa: E731
    row = ch.add_channel("@TED", lister=lister, interval_min=1)
    assert row["interval_min"] == ch.MIN_INTERVAL_MIN  # clamped
    assert row["title"] == "TED"
    with pytest.raises(ch.ChannelError):
        ch.add_channel("@TED", lister=lister)

    assert ch.check_channel(row["id"], lister=lister) == []  # nothing new
    assert bq.list_items() == []

    new = {"id": "fresh", "title": "Brand new", "duration": 300}
    lister_data["v"] = {"entries": [new, *LISTING["entries"]]}
    got = ch.check_channel(row["id"], lister=lister)
    assert [c.id for c in got] == ["fresh"]
    items = bq.list_items()
    assert [i.url for i in items] == ["https://www.youtube.com/watch?v=fresh"]
    assert items[0].origin == f"channel:{row['id']}"
    # a second check does not re-enqueue
    assert ch.check_channel(row["id"], lister=lister) == []
    assert ch.get_channel(row["id"])["enqueued_count"] == 1


def test_due_and_delete(initialised_db):
    lister = lambda url: LISTING  # noqa: E731
    row = ch.add_channel("@TED", lister=lister, interval_min=60)
    assert ch.due_channels() == []
    assert ch.due_channels(now=row["last_checked"] + 3601) == [row["id"]]
    assert ch.update_channel(row["id"], enabled=False)["enabled"] is False
    assert ch.due_channels(now=row["last_checked"] + 99999) == []
    assert ch.delete_channel(row["id"])
    assert ch.list_channels() == []
