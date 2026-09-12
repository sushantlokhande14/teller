"""
Two deployments of the same vendor product.

`MERIDIAN_VARIANT=summit` runs the same application configured and branded the way
a second institution would have it: its own name and colours, a newer point release,
a content frame the integrator renamed, a welcome banner after sign-in, and one menu
item relabelled. Nothing about the workflow changes, which is the point. It is the
stand-in for two tenants running the same core vendor product.
"""
from __future__ import annotations

import os

VARIANTS: dict[str, dict[str, str | None]] = {
    "meridian": {
        "brand": "MERIDIAN CORE SERVICING",
        "org": "Meridian Financial Systems",
        "version": "4.2.117",
        "content_frame": "main",
        "lookup_label": "Member Lookup",
        "search_label": "Search",
        "welcome": None,
        "accent": "#d9dce8",
    },
    "summit": {
        "brand": "SUMMIT CREDIT UNION",
        "org": "Summit Credit Union",
        "version": "4.4.02",
        "content_frame": "content",          # the integrator renamed the frame
        "lookup_label": "Member Search",     # and relabelled one menu item
        "search_label": "Find Member",
        "welcome": "Welcome to Summit Credit Union",
        "accent": "#cfe0d4",
    },
}


def current() -> dict[str, str | None]:
    return VARIANTS.get(os.environ.get("MERIDIAN_VARIANT", "meridian"), VARIANTS["meridian"])
