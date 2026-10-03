from __future__ import annotations

# The 28 states and 8 union territories of India (names as commonly written).
STATES: tuple[str, ...] = (
    "Andhra Pradesh",
    "Arunachal Pradesh",
    "Assam",
    "Bihar",
    "Chhattisgarh",
    "Goa",
    "Gujarat",
    "Haryana",
    "Himachal Pradesh",
    "Jharkhand",
    "Karnataka",
    "Kerala",
    "Madhya Pradesh",
    "Maharashtra",
    "Manipur",
    "Meghalaya",
    "Mizoram",
    "Nagaland",
    "Odisha",
    "Punjab",
    "Rajasthan",
    "Sikkim",
    "Tamil Nadu",
    "Telangana",
    "Tripura",
    "Uttar Pradesh",
    "Uttarakhand",
    "West Bengal",
    "Andaman and Nicobar Islands",
    "Chandigarh",
    "Dadra and Nagar Haveli and Daman and Diu",
    "Delhi",
    "Jammu and Kashmir",
    "Ladakh",
    "Lakshadweep",
    "Puducherry",
)

_BY_LOWER = {name.lower(): name for name in STATES}


def canonical_state(value: str) -> str:
    """Return the standard spelling of a state or union territory, or raise."""
    key = " ".join(value.split()).lower().replace("&", "and")
    if key in _BY_LOWER:
        return _BY_LOWER[key]
    raise ValueError("Choose a state or union territory from the list")
