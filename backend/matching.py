from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from .db import json_load


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    return re.sub(r"[\u3000\s]+", " ", value).casefold().strip()


@dataclass
class MatchHit:
    keyword: str
    rule_type: str
    source_type: str
    source_file: str
    location: str
    snippet: str
    context_before: str
    context_after: str
    is_negative: bool = False


def _snippet(text: str, start: int, end: int, window: int = 56) -> tuple[str, str, str]:
    left, right = max(0, start - window), min(len(text), end + window)
    return text[left:right], text[left:start], text[end:right]


def occurrences(text: str, keyword: str, source_type: str, source_file: str, location: str, rule_type: str, negative: bool = False) -> list[MatchHit]:
    needle = normalize(keyword)
    if not needle:
        return []
    chars: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(text):
        for normalized in unicodedata.normalize("NFKC", char).casefold():
            normalized = " " if normalized.isspace() else normalized
            if normalized == " " and chars and chars[-1] == " ":
                continue
            chars.append(normalized)
            offsets.append(index)
    haystack = "".join(chars)
    hits: list[MatchHit] = []
    start = 0
    while len(hits) < 20:
        found = haystack.find(needle, start)
        if found < 0:
            break
        snippet, before, after = _snippet(text, offsets[found], offsets[found + len(needle) - 1] + 1)
        hits.append(MatchHit(keyword, rule_type, source_type, source_file, location, snippet, before, after, negative))
        start = found + len(needle)
    return hits


def expand_synonyms(group: dict[str, Any], words: list[str]) -> list[str]:
    synonyms = json_load(group.get("synonyms_json"), {})
    expanded = list(words)
    for word in words:
        expanded.extend(synonyms.get(word, []))
    return list(dict.fromkeys(word for word in expanded if normalize(word)))


def match_sources(group: dict[str, Any], sources: list[dict[str, str]]) -> list[MatchHit]:
    terms = {key: json_load(group.get(key + "_json"), []) for key in ("include_any", "include_all", "phrases", "exclude")}
    expanded = {key: expand_synonyms(group, words) for key, words in terms.items()}
    scopes = set(json_load(group.get("scopes_json"), ["title", "body", "attachment_name", "attachment_body"]))
    usable = [source for source in sources if source.get("source_type") in scopes]
    combined = normalize("\n".join(source.get("text", "") for source in usable))
    if not any(expanded[key] for key in ("include_any", "include_all", "phrases")):
        return []
    if expanded["include_any"] and not any(normalize(word) in combined for word in expanded["include_any"]):
        return []
    for key in ("include_all", "phrases"):
        for term in terms[key]:
            if not any(normalize(word) in combined for word in expand_synonyms(group, [term])):
                return []
    hits: list[MatchHit] = []
    for source in usable:
        for key, words in expanded.items():
            # Exclusions describe the subject of the announcement.  Technical
            # attachments routinely contain incidental words such as
            # "培训要求" and must not veto an otherwise valid procurement.
            if key == "exclude" and source.get("source_type") not in {"title", "body"}:
                continue
            for word in words:
                hits.extend(occurrences(source.get("text", ""), word, source["source_type"], source.get("source_file", ""), source.get("location", ""), "phrase" if key == "phrases" else key, key == "exclude"))
    return hits
