# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Record of the screens this device has shown and what their elements carry.

``get_ui_hierarchy`` compacts a UI dump by dropping attributes that never vary.
Which attributes those are is a property of the apps being driven, not of this
code, so it lives here as data: :func:`kept_attributes` answers it per package
and :func:`observe` keeps the record current. A screen whose fingerprint has not
been seen before is reported as new, and an attribute/value pair that no screen
of that package has shown before is reported as novel -- the signal that the
projection may be dropping something that matters on a page nobody has looked at
yet.
"""

from collections.abc import Iterable, Sequence
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)

# Attributes whose non-default value changes what a caller would do: a switch that
# is on, the tab you are already on, a button that cannot be pressed, the field
# that will receive typing. Seeded for every package ('*'); a row naming a single
# package overrides it for that app alone.
_SEED_ATTRIBUTES: tuple[tuple[str, str], ...] = (
    ("clickable", "true"),
    ("long-clickable", "true"),
    ("scrollable", "true"),
    ("selected", "true"),
    ("focused", "true"),
    ("password", "true"),
    ("enabled", "false"),
)

# Attributes that say what is on a screen rather than what kind of screen it is,
# so they are excluded from novelty: every new message or contact name would
# otherwise read as a discovery.
_CONTENT_ATTRIBUTES = frozenset(
    {
        "text",
        "content-desc",
        "accessibilityText",
        "hint",
        "bounds",
        "parsed_bounds",
        "index",
        "drawing-order",
        "resource-id",
    }
)

# Attributes that vary freely without changing what a caller would do. Reporting
# them as unexamined would bury the ones that matter on the first screen of every
# app.
_IGNORED_ATTRIBUTES = frozenset(
    {"package", "class", "NAF", "display-id", "visible-to-user", "focusable"}
)

# Handled by the projection even though no kept_attributes row names them: a
# checkable element always reports 'checked', because both of its values matter.
_PAIRED_ATTRIBUTES = frozenset({"checkable", "checked"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS screens (
    fingerprint   TEXT PRIMARY KEY,
    package       TEXT NOT NULL,
    activity      TEXT NOT NULL,
    first_seen    TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen     TEXT NOT NULL DEFAULT (datetime('now')),
    seen_count    INTEGER NOT NULL DEFAULT 1,
    element_count INTEGER NOT NULL,
    raw_chars     INTEGER NOT NULL,
    compact_chars INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS attribute_values (
    package    TEXT NOT NULL,
    attribute  TEXT NOT NULL,
    value      TEXT NOT NULL,
    count      INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (package, attribute, value)
);

CREATE TABLE IF NOT EXISTS kept_attributes (
    package   TEXT NOT NULL,
    attribute TEXT NOT NULL,
    trigger   TEXT NOT NULL,
    note      TEXT,
    PRIMARY KEY (package, attribute)
);

CREATE INDEX IF NOT EXISTS screens_by_package ON screens (package, activity);
"""


def db_path() -> Path:
    """Returns the corpus location, overridable with ``ARTEMIS_UI_CORPUS_DB``."""
    override = os.environ.get("ARTEMIS_UI_CORPUS_DB")
    if override:
        return Path(override)
    try:
        from artemis.config import settings

        base = Path(settings.TRACES_PATH)
    except Exception:  # pylint: disable=broad-exception-caught
        base = Path("traces")
    return base / "ui_corpus.db"


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Opens the corpus, creating the schema and seeding defaults on first use."""
    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT OR IGNORE INTO kept_attributes (package, attribute, trigger, note)"
        " VALUES ('*', ?, ?, 'seeded default')",
        _SEED_ATTRIBUTES,
    )
    conn.commit()
    return conn


def kept_attributes(
    package: str | None, conn: sqlite3.Connection | None = None
) -> tuple[tuple[str, str], ...]:
    """Returns the (attribute, trigger) pairs to report for ``package``.

    A row naming the package wins over the '*' row for the same attribute, so an
    app that encodes something unusual can be taught without touching the code.
    """
    owned = conn is None
    try:
        conn = conn or connect()
        rows = conn.execute(
            "SELECT package, attribute, trigger FROM kept_attributes WHERE package IN ('*', ?)",
            (package or "*",),
        ).fetchall()
    except Exception:  # pylint: disable=broad-exception-caught
        logger.debug("ui_corpus unreadable; falling back to seeded attributes", exc_info=True)
        return _SEED_ATTRIBUTES
    finally:
        if owned and conn is not None:
            conn.close()

    merged: dict[str, str] = {}
    for row in sorted(rows, key=lambda r: r["package"] == "*", reverse=True):
        merged[row["attribute"]] = row["trigger"]
    return tuple(merged.items()) or _SEED_ATTRIBUTES


def fingerprint(package: str, activity: str, elements: Sequence[dict[str, Any]]) -> str:
    """Identifies a screen by its layout rather than by the content shown on it.

    Built from the resource ids and widget classes present, so the same chat list
    fingerprints alike whichever conversations it happens to be showing, while a
    settings page or a composer fingerprints differently.
    """
    ids = sorted({(e.get("resource-id") or "").rsplit("/", 1)[-1] for e in elements} - {""})
    # Apps that lean on RecyclerView or Compose expose almost no resource ids, so
    # ids alone collapse every one of their screens together. How many of each
    # widget a screen uses separates them; the count is bucketed by powers of two
    # so that a list gaining a row is still the same screen.
    histogram: dict[str, int] = {}
    for element in elements:
        name = (element.get("class") or "").rsplit(".", 1)[-1]
        if name:
            histogram[name] = histogram.get(name, 0) + 1
    shape = sorted((name, count.bit_length()) for name, count in histogram.items())
    payload = json.dumps([package, activity, ids, shape], sort_keys=True)
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


def dominant_package(elements: Iterable[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for element in elements:
        name = element.get("package") or ""
        if name and not name.startswith("com.android.systemui"):
            counts[name] = counts.get(name, 0) + 1
    return max(counts, key=counts.get) if counts else "unknown"


def observe(
    elements: Sequence[dict[str, Any]],
    *,
    activity: str = "",
    raw_chars: int = 0,
    compact_chars: int = 0,
    package: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Records one screen and reports whether anything about it is new.

    Returns the fingerprint, whether this layout had been seen before, how often
    it has been seen, and any attribute/value pair this package has never shown --
    which is the cue to check whether the projection still covers it.
    """
    package = package or dominant_package(elements)
    conn = None
    try:
        conn = connect(path)
        fp = fingerprint(package, activity, elements)
        existing = conn.execute(
            "SELECT seen_count FROM screens WHERE fingerprint = ?", (fp,)
        ).fetchone()
        is_new = existing is None

        if is_new:
            conn.execute(
                "INSERT INTO screens (fingerprint, package, activity, element_count,"
                " raw_chars, compact_chars) VALUES (?, ?, ?, ?, ?, ?)",
                (fp, package, activity, len(elements), raw_chars, compact_chars),
            )
            seen = 1
        else:
            conn.execute(
                "UPDATE screens SET last_seen = datetime('now'), seen_count = seen_count + 1,"
                " element_count = ?, raw_chars = ?, compact_chars = ?  WHERE fingerprint = ?",
                (len(elements), raw_chars, compact_chars, fp),
            )
            seen = existing["seen_count"] + 1

        known = {
            (r["attribute"], r["value"])
            for r in conn.execute(
                "SELECT attribute, value FROM attribute_values WHERE package = ?", (package,)
            )
        }
        kept = {name for name, _ in kept_attributes(package, conn)}
        seen_now: dict[tuple[str, str], int] = {}
        for element in elements:
            for attribute, value in element.items():
                if attribute in _CONTENT_ATTRIBUTES or attribute in _IGNORED_ATTRIBUTES:
                    continue
                if not isinstance(value, str):
                    continue
                seen_now[(attribute, value)] = seen_now.get((attribute, value), 0) + 1

        novel = sorted(
            {pair[0] for pair in seen_now if pair not in known}
            - _IGNORED_ATTRIBUTES
            - _PAIRED_ATTRIBUTES
        )
        conn.executemany(
            "INSERT INTO attribute_values (package, attribute, value, count) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (package, attribute, value) DO UPDATE SET count = count + excluded.count",
            [(package, a, v, c) for (a, v), c in seen_now.items()],
        )
        conn.commit()

        report: dict[str, Any] = {"fingerprint": fp, "package": package, "seen_count": seen}
        if is_new:
            report["new_screen"] = True
        # Only an unseen attribute the projection would silently drop is worth raising.
        unexamined = [a for a in novel if a not in kept]
        if unexamined:
            report["unexamined_attributes"] = unexamined
        return report
    except Exception:  # pylint: disable=broad-exception-caught
        logger.debug("ui_corpus observation skipped", exc_info=True)
        return {}
    finally:
        if conn is not None:
            conn.close()


def coverage(package: str | None = None, path: Path | None = None) -> dict[str, Any]:
    """Summarises what has been seen, per app."""
    conn = None
    try:
        conn = connect(path)
        where, args = ("WHERE package = ?", (package,)) if package else ("", ())
        rows = conn.execute(
            f"SELECT package, COUNT(*) AS screens, SUM(seen_count) AS visits,"
            f" SUM(raw_chars) AS raw, SUM(compact_chars) AS compact FROM screens {where}"
            " GROUP BY package ORDER BY screens DESC",
            args,
        ).fetchall()
        return {
            "db": str(path or db_path()),
            "apps": [
                {
                    "package": r["package"],
                    "screens": r["screens"],
                    "visits": r["visits"],
                    "raw_chars": r["raw"],
                    "compact_chars": r["compact"],
                    "saved_pct": round(100 - 100 * (r["compact"] or 0) / (r["raw"] or 1), 1),
                }
                for r in rows
            ],
        }
    except Exception:  # pylint: disable=broad-exception-caught
        logger.debug("ui_corpus coverage unavailable", exc_info=True)
        return {}
    finally:
        if conn is not None:
            conn.close()
