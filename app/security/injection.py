"""Prompt-injection detector for UNTRUSTED content (uploaded documents, feedback).

Detection only flags; it never "cleans" or drops content. Flagged chunks are still
stored and shown as data with a warning. The real defence is architectural:
retrieval is RLS-scoped, so even an obeyed instruction cannot reach other tenants'
data, and the model's output is validated against retrieved evidence.
"""

import re

_RULES: list[tuple[str, re.Pattern]] = [
    (
        "override_instructions",
        re.compile(
            r"\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(instructions?|rules?|polic(y|ies)|guardrails?|prompts?)\b",  # noqa: E501
            re.I | re.S,
        ),
    ),
    (
        "role_hijack",
        re.compile(
            r"\b(you are now|act as|pretend to be|from now on you)\b.{0,40}\b(admin|administrator|developer|system|dan|unrestricted)",  # noqa: E501
            re.I | re.S,
        ),
    ),
    (
        "system_impersonation",
        re.compile(
            r"(\bsystem (instruction|prompt|message|override)\b|^\s*(system|assistant)\s*:|<\|im_start\|>|\[/?INST\]|###\s*instruction)",  # noqa: E501
            re.I | re.M,
        ),
    ),
    (
        "exfiltration",
        re.compile(
            r"\b(reveal|show|list|dump|export|print)\b.{0,60}\b(all|every|other)\b.{0,40}\b(tenants?|records?|employees?|data|passwords?|secrets?|keys?)\b",  # noqa: E501
            re.I | re.S,
        ),
    ),
    ("mode_switch", re.compile(r"\b(administrator|admin|developer|god|debug) mode\b", re.I)),
]


def detect(text: str) -> list[str]:
    """Names of the rules that matched (empty list = not suspicious)."""
    return [name for name, rx in _RULES if rx.search(text or "")]


def is_suspicious(text: str) -> bool:
    return bool(detect(text))
