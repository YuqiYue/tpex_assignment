"""Domain knowledge about FacilityDesk that the rest of the code relies on."""

CHAT_TYPES = (
    "SUPPLIER_AND_TENANT",
    "INTERNAL_TEAM",
    "TECHNICAL_MANAGER_AND_SUPPLIER",
    "TECHNICAL_MANAGER_AND_TENANT",
    "ANNOUNCEMENT",
)

# A ticket is "open" unless its step is one of these. We deliberately define
# closed (not open) explicitly: an unknown step such as ON_HOLD (seen in the
# API examples but missing from the enum) is then treated as open and keeps
# being polled. Polling a closed ticket too often costs a few requests;
# missing changes on an open ticket breaks the freshness requirement.
CLOSED_STEPS = ("ISSUE_CLOSED", "REJECTED", "REJECTED_BY_SUPPLIER")

# FacilityDesk returns some timestamps without an offset (e.g. site_visit_at
# "2026-09-28 13:30:00"). All buildings in the examples are in the
# Netherlands/Germany, so we assume local time there.
DEFAULT_TIMEZONE = "Europe/Amsterdam"
