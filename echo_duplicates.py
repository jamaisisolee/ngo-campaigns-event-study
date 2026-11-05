"""Cheap, local flags for repeated exports, print/web copies and wire updates.

No LLM calls or network requests. Copy flags are inspectable and never delete
records. Similar-copy checks are restricted to one outlet family and two days.
"""

import hashlib
import html
import re
import unicodedata
from collections import defaultdict
from datetime import date


def normalize(text):
    text = unicodedata.normalize("NFKD", html.unescape(text or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def outlet_family(source):
    value = normalize(source)
    if "financial times deutschland" in value:
        return "Financial Times Deutschland"
    for matches, family in [
        (("financial times",), "Financial Times"),
        (("new york times", "nytimes", "nyt blogs", "international herald tribune"), "The New York Times"),
        (("les echos",), "Les Echos"),
        (("wirtschaftswoche",), "WirtschaftsWoche"),
        (("handelsblatt",), "Handelsblatt"),
        (("guardian",), "The Guardian"),
        (("reuters",), "Reuters"),
        (("el pais", "elpais"), "El Pais"),
    ]:
        if any(term in value for term in matches):
            return family
    return source


def headline_key(headline):
    value = headline.strip()
    value = re.sub(r"^(?:(?:UPDATE|WRAPUP|RPT|REFILE)(?:\s*\d+)?\s*[-: ]\s*)+", "", value, flags=re.I)
    return normalize(value)


class UnionFind:
    def __init__(self, ids):
        self.parent = {key: key for key in ids}

    def find(self, key):
        while self.parent[key] != key:
            self.parent[key] = self.parent[self.parent[key]]
            key = self.parent[key]
        return key

    def join(self, a, b):
        self.parent[self.find(b)] = self.find(a)


def flag_duplicates(articles):
    ids = list(articles)
    copies, stories = UnionFind(ids), UnionFind(ids)
    exact, families, texts, shingles = {}, defaultdict(list), {}, {}
    ordered = sorted(articles.values(), key=lambda a: (a["article_date"], a["article_id"]))
    edges = []
    for article in ordered:
        key = article["article_id"]
        text = normalize(article["body"])
        texts[key] = text
        digest = hashlib.sha256(text.encode()).hexdigest()
        if text and digest in exact:
            copies.join(exact[digest], key)
            stories.join(exact[digest], key)
            edges.append((exact[digest], key, "exact_normalized_text", 1.0))
        elif text:
            exact[digest] = key
        family = outlet_family(article["source"])
        article["outlet_family"] = family
        day = date.fromisoformat(article["article_date"])
        words = text.split()
        for previous in reversed(families[family]):
            prev_day = date.fromisoformat(previous["article_date"])
            if (day - prev_day).days > 2:
                break
            other = previous["article_id"]
            if copies.find(key) == copies.find(other):
                continue
            # Shingle overlap is cheap and does not require an edit-distance model.
            ratio = len(text) / max(1, len(texts[other]))
            if len(words) >= 80 and 0.85 <= ratio <= 1.18:
                if key not in shingles:
                    shingles[key] = set(zip(words, words[1:], words[2:]))
                if other not in shingles:
                    w = texts[other].split()
                    shingles[other] = set(zip(w, w[1:], w[2:]))
                left, right = shingles[key], shingles[other]
                similarity = len(left & right) / max(1, len(left | right))
                if similarity >= 0.90:
                    copies.join(other, key)
                    stories.join(other, key)
                    edges.append((other, key, "same_outlet_near_text", round(similarity, 4)))
            if family == "Reuters" and headline_key(previous["headline"]) == headline_key(article["headline"]):
                stories.join(other, key)
        families[family].append(article)
    copy_groups, story_groups = defaultdict(list), defaultdict(list)
    for key in ids:
        copy_groups[copies.find(key)].append(key)
        story_groups[stories.find(key)].append(key)
    for members in copy_groups.values():
        canonical = min(members, key=lambda k: (articles[k]["article_date"], k))
        group_id = hashlib.sha256(("copy|" + canonical).encode()).hexdigest()[:16]
        for key in members:
            a = articles[key]
            a.update(duplicate_group_id=group_id, duplicate_group_size=len(members),
                     is_duplicate=int(key != canonical), duplicate_of=canonical if key != canonical else "",
                     duplicate_kind="unique" if len(members) == 1 else "representative" if key == canonical else "exact_text_copy" if texts[key] == texts[canonical] else "probable_channel_copy",
                     export_occurrences=len(a.get("input_files", a["input_file"]).split("; ")),
                     repeated_in_exports=int("; " in a.get("input_files", "")))
    for members in story_groups.values():
        canonical = min(members, key=lambda k: (articles[k]["article_date"], k))
        group_id = hashlib.sha256(("story|" + canonical).encode()).hexdigest()[:16]
        copy_ids = {articles[k]["duplicate_group_id"] for k in members}
        for key in members:
            articles[key].update(count_story_id=group_id,
                                 related_story_versions=len(members),
                                 has_updated_versions=int(len(copy_ids) > 1),
                                 deduplicated_first_date=articles[canonical]["article_date"])
    return edges
