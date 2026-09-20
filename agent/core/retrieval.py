"""Finding a saved job by describing it, not by remembering its name.

The library is only useful if you can get at it. Today you must know a job is
called "bist30-morning-pull"; what you actually have in your head is "get me
the index numbers". This closes that gap.

**Why not embeddings.** The obvious answer is a sentence-transformer over
descriptions and step intents. It was rejected for now, deliberately:

  * it means a ~90MB model download on first use, and this project has already
    been bitten once by a tier that silently pulled 1.2GB the first time
    somebody touched it;
  * a job library is tens to hundreds of entries, not millions — exhaustive
    scoring is microseconds, so there is nothing for a vector index to buy;
  * the queries are short and share vocabulary with the jobs ("excel",
    "invoice", "bist30"), which is the case where lexical matching is
    strongest and dense retrieval adds least.

What lexical matching genuinely cannot do is match "stock market" to "borsa".
`suggest()` therefore returns candidates *and their scores*, and the caller —
Claude, which does understand that the two mean the same thing — makes the
final call. That is a better division of labour than a similarity threshold.

If the library ever grows past a few hundred jobs, `EMBEDDINGS` below is where
that tier goes; the interface will not need to change.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

#: Reserved for a future dense tier. Off, and not imported unless enabled, so
#: nothing downloads a model behind the user's back.
EMBEDDINGS = False

#: Words that carry no signal about *which* job is meant. Still deliberately
#: conservative — over-stemming a small corpus loses more than it gains — but
#: it must cover the ordinary connective tissue of a sentence. It did not, and
#: the omission was not harmless: a lesson whose text contained the word "is"
#: was recalled for the question "what is currently on my screen", because
#: "is" was scored as evidence like any other word.
STOP = {
    "a", "an", "the", "and", "or", "of", "to", "for", "in", "on", "at", "by",
    "with", "from", "into", "my", "me", "i", "it", "this", "that", "then",
    "please", "can", "you", "do", "does", "make", "get", "go",
    # Copulas and auxiliaries: present in almost every sentence, evidence of
    # nothing.
    "is", "are", "was", "were", "be", "am", "been", "being",
    "has", "have", "had", "will", "would", "should", "could", "may", "might",
    # Question words and connectives.
    "what", "which", "who", "when", "where", "why", "how",
    "but", "if", "so", "than", "as", "not", "no",
    # Positional and quantifying filler.
    "there", "here", "about", "after", "before", "again", "now", "still",
    "just", "only", "also", "very", "some", "any", "all", "each", "other",
    "such", "same", "too", "out", "off", "over", "under", "up", "down",
    "currently", "want", "need", "like",
    # Turkish — the user works in both languages, and a job description may be
    # written in either.
    "ve", "ile", "bir", "bu", "su", "icin", "de", "da", "bana", "beni",
    "ne", "nasil", "nerede", "ise", "olan", "gibi", "daha", "cok", "var",
}


def _fold(text: str) -> str:
    """Lowercase and strip accents so a Turkish query meets a Turkish job.

    Turkish needs handling before the generic pass: dotted capital I and
    dotless lowercase i do not round-trip through str.lower() the way the rest
    of Latin-1 does, so "İŞLEM" and "islem" would otherwise never meet.
    """
    t = (text or "").replace("İ", "i").replace("I", "ı").lower()
    for a, b in (("ı", "i"), ("ş", "s"), ("ğ", "g"),
                 ("ü", "u"), ("ö", "o"), ("ç", "c")):
        t = t.replace(a, b)
    t = unicodedata.normalize("NFKD", t)
    return "".join(c for c in t if not unicodedata.combining(c))


def tokens(text: str) -> list[str]:
    """Words worth matching on. Digits are kept — "bist30" is the whole point."""
    out: list[str] = []
    for w in re.findall(r"[a-z0-9]+", _fold(text)):
        if len(w) < 2 or w in STOP:
            continue
        out.append(w)
        # Split a trailing number off too, so "bist30" also matches "bist 30".
        m = re.match(r"^([a-z]+)(\d+)$", w)
        if m:
            out.extend(m.groups())
    return out


def _similar(a: str, b: str) -> float:
    """Cheap similarity for near-misses: typos, plurals, drifted labels.

    Bigram overlap was tried first and is too harsh on short words — it scores
    excel/exel at 0.57, below any threshold that also excludes excel/export.
    SequenceMatcher separates them properly: real slips and plurals land in
    0.80-0.92, unrelated words in 0.36-0.50, with nothing in between.
    """
    if a == b:
        return 1.0
    if len(a) < 4 or len(b) < 4:
        return 0.0                      # too short to tell a typo from a word
    if a.startswith(b) or b.startswith(a):
        # A shared prefix only means a shared word when the shorter one is
        # most of the longer one. Without that ratio, "what" scored 0.9
        # against "whatsapp", so the question "what is on my screen" pulled up
        # every WhatsApp lesson. Plurals and drifted labels (excel/excels,
        # islem/islemler) stay well above the bar.
        shorter, longer = sorted((len(a), len(b)))
        if shorter / max(longer, 1) >= 0.6:
            return 0.9
    return SequenceMatcher(None, a, b).ratio()


#: How much each part of a job says about what it is for. A name and a
#: description are written to be read; step intents are incidental detail, and
#: weighting them equally lets a long job outrank a well-named short one.
FIELD_WEIGHT = {"name": 3.0, "description": 3.0, "tags": 2.0,
                "apps": 1.5, "intents": 1.0}

FUZZY_FLOOR = 0.78          # measured: typos land above, unrelated words below


@dataclass
class Candidate:
    id: str
    name: str
    description: str
    score: float = 0.0
    matched: list[str] = field(default_factory=list)
    trust: str = "draft"
    runs: int = 0
    success_rate: float = 0.0

    def why(self) -> str:
        if not self.matched:
            return "no overlapping terms"
        return "matched " + ", ".join(sorted(set(self.matched))[:6])


class Index:
    """Scores a query against the job library. Rebuilt on demand — it is cheap.

    Term weighting is IDF over the library itself, so a word appearing in every
    job ("excel", if they are all Excel jobs) stops being evidence, while a
    word appearing in one is decisive.
    """

    def __init__(self, rows: list[dict] | None = None):
        self.rows: list[dict] = []
        self.docs: list[dict[str, list[str]]] = []
        self.df: dict[str, int] = {}
        if rows:
            self.build(rows)

    # -- building --------------------------------------------------------
    def build(self, rows: list[dict]) -> "Index":
        self.rows, self.docs, self.df = list(rows), [], {}
        for r in self.rows:
            doc = {
                "name": tokens(r.get("name", "")),
                "description": tokens(r.get("description", "")),
                "tags": tokens(" ".join(r.get("tags") or [])),
                "apps": tokens(" ".join(r.get("requires") or [])),
                "intents": tokens(" ".join(r.get("intents") or [])),
            }
            self.docs.append(doc)
            for t in {t for toks in doc.values() for t in toks}:
                self.df[t] = self.df.get(t, 0) + 1
        return self

    def idf(self, term: str) -> float:
        n = max(len(self.rows), 1)
        # +1 smoothing, with a floor so a term present in every job still
        # counts for a little rather than for exactly nothing.
        return max(math.log((n + 1) / (self.df.get(term, 0) + 1)) + 0.25, 0.05)

    # -- querying --------------------------------------------------------
    def search(self, query: str, limit: int = 5,
               min_score: float = 0.5) -> list[Candidate]:
        q = tokens(query)
        if not q or not self.rows:
            return []

        out: list[Candidate] = []
        for row, doc in zip(self.rows, self.docs):
            score, matched = 0.0, []
            for term in set(q):
                weight = self.idf(term)
                best, label = 0.0, ""
                for field_name, toks in doc.items():
                    if not toks:
                        continue
                    fw = FIELD_WEIGHT.get(field_name, 1.0)
                    if term in toks:
                        hit, tag = fw, term
                    else:
                        sim = max((_similar(term, t) for t in toks), default=0.0)
                        if sim < FUZZY_FLOOR:
                            continue
                        # A fuzzy hit must be worth strictly less than an exact
                        # one, or a typo outranks a real match in another field.
                        hit, tag = fw * sim * 0.7, f"{term}~"
                    if hit > best:
                        best, label = hit, tag
                if best:
                    score += weight * best
                    matched.append(label)

            if score <= 0:
                continue
            # Normalise by query length, so a long question is not inherently
            # scored higher than a short one.
            score /= max(len(set(q)), 1)

            # A job that has actually worked here is the better suggestion — a
            # nudge, never enough to reorder a clearly better textual match.
            if row.get("runs"):
                score *= 1.0 + 0.12 * float(row.get("success_rate") or 0.0)
            trust = row.get("trust", "draft")
            if trust == "trusted":
                score *= 1.08
            elif trust == "quarantined":
                score *= 0.4

            out.append(Candidate(
                id=row.get("id", ""), name=row.get("name", ""),
                description=row.get("description", ""),
                score=round(score, 3), matched=matched, trust=trust,
                runs=int(row.get("runs") or 0),
                success_rate=float(row.get("success_rate") or 0.0)))

        out.sort(key=lambda c: (-c.score, c.name))
        return [c for c in out if c.score >= min_score][:limit]


def rows_from_store(store) -> list[dict]:
    """Library rows plus the step intents, which store.list() omits.

    Intents matter for retrieval — "click the export button" is often the only
    place the word "export" appears — but they are not worth loading for a
    plain listing, so they are gathered only here.
    """
    rows = []
    for r in store.list():
        row = dict(r)
        try:
            macro, _ = store.load(r["id"])
            row["intents"] = [s.intent for s in macro.steps if s.intent]
        except Exception:
            row["intents"] = []
        rows.append(row)
    return rows


def suggest(store, query: str, limit: int = 5) -> list[Candidate]:
    """Jobs that might already do what was asked for."""
    return Index(rows_from_store(store)).search(query, limit=limit)
