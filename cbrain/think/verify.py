"""Truth gate: a claim survives only if its quote really appears in the transcript. Pure code, no LLM.

Matching happens on normalized word tokens (casefolded, punctuation removed, caption artifacts such as [Music]
removed) over the whole transcript, so a quote may span segment boundaries. Every token remembers its segment and
its original word, which gives `claim.t` (start of the segment where the match begins) and the real transcript span.

Allowed matches:
  1. exact token-sequence match (a normalized substring that starts and ends on word boundaries);
  2. the one tolerance: a same-length window where >= 90% of tokens match in order (one mis-heard word in a
     long quote). The stored quote is then replaced by the transcript's real words. A mismatch involving a number
     or a negation word is never tolerated (it could flip the meaning).
"""

from __future__ import annotations

import html
import math
import re
import unicodedata
from dataclasses import dataclass, replace

from ..models import Claim, Transcript

MIN_QUOTE_WORDS = 5
FUZZY_MIN_MATCH = 0.90

_BRACKETS = re.compile(r"\[[^\]]*\]")  # [Music] [Applause] [ __ ] ...
_PAREN_ARTIFACT = re.compile(
    r"\(\s*(?:music|applause|laughter|laughs|laughing|cheering|cheers|inaudible|silence|crosstalk|sighs|"
    r"coughs|clears throat)[^)]*\)", re.I)
_MUSIC_NOTES = re.compile(r"[♪♫♬♩]")
_TRANSLATE = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'", "ʼ": "'", "`": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
    "–": "-", "—": "-", "−": "-", "‐": "-", "‑": "-",
})
_NON_WORD = re.compile(r"[\W_]+")
_NEGATIONS = {"not", "no", "never", "dont", "doesnt", "didnt", "cant", "cannot", "wont", "wouldnt", "shouldnt",
              "couldnt", "isnt", "arent", "wasnt", "werent", "without", "none", "neither", "nor", "nothing"}


def clean_caption_text(text: str) -> str:
    """Remove auto-caption artifacts ([Music], (applause), music notes) and decode HTML entities."""
    text = html.unescape(text or "")
    text = _BRACKETS.sub(" ", text)
    text = _PAREN_ARTIFACT.sub(" ", text)
    return _MUSIC_NOTES.sub(" ", text)


def word_tokens(raw_word: str) -> list[str]:
    """Normalize one whitespace-delimited word into 0..n tokens ("high-intensity" -> 2, "don't" -> "dont")."""
    w = unicodedata.normalize("NFKC", raw_word).translate(_TRANSLATE).casefold().replace("'", "")
    return _NON_WORD.sub(" ", w).split()


def normalize(text: str) -> str:
    """lowercase, unify quotes/apostrophes, strip punctuation and caption artifacts, collapse whitespace."""
    return " ".join(t for w in clean_caption_text(text).split() for t in word_tokens(w))


@dataclass
class Match:
    start: int        # token index
    end: int          # token index, inclusive
    exact: bool
    mismatches: int = 0


class TranscriptIndex:
    """Normalized token stream of a transcript with token -> segment / original-word maps."""

    def __init__(self, transcript: Transcript):
        self.raw_words: list[str] = []
        self.tokens: list[str] = []
        self.tok_seg: list[int] = []
        self.tok_raw: list[int] = []
        self.seg_start: list[float] = [s.start for s in transcript.segments]
        self.positions: dict[str, list[int]] = {}
        for si, seg in enumerate(transcript.segments):
            for word in clean_caption_text(seg.text).split():
                raw_i = len(self.raw_words)
                self.raw_words.append(word)
                for tok in word_tokens(word):
                    self.positions.setdefault(tok, []).append(len(self.tokens))
                    self.tokens.append(tok)
                    self.tok_seg.append(si)
                    self.tok_raw.append(raw_i)

    def find(self, q: list[str], hint_t: float | None = None) -> Match | None:
        n = len(q)
        if n == 0 or n > len(self.tokens):
            return None
        exact = [p for p in self.positions.get(q[0], ()) if p + n <= len(self.tokens)
                 and self.tokens[p:p + n] == q]
        if exact:
            p = self._pick(exact, hint_t)
            return Match(p, p + n - 1, True)
        return self._fuzzy(q, hint_t)

    def _pick(self, starts: list[int], hint_t: float | None) -> int:
        if hint_t is None or len(starts) == 1:
            return starts[0]
        return min(starts, key=lambda p: (abs(self.seg_start[self.tok_seg[p]] - hint_t), p))

    def _fuzzy(self, q: list[str], hint_t: float | None) -> Match | None:
        n = len(q)
        allowed = n - math.ceil(FUZZY_MIN_MATCH * n - 1e-9)
        if allowed <= 0:
            return None
        toks, best = self.tokens, []
        for p in range(len(toks) - n + 1):
            bad, ok = 0, True
            for i in range(n):
                if toks[p + i] != q[i]:
                    if _unsafe_mismatch(toks[p + i], q[i]):
                        ok = False
                        break
                    bad += 1
                    if bad > allowed:
                        ok = False
                        break
            if ok and bad:
                best.append((bad, p))
        if not best:
            return None
        fewest = min(b for b, _ in best)
        p = self._pick([p for b, p in best if b == fewest], hint_t)
        return Match(p, p + n - 1, False, fewest)

    def segment_start(self, tok: int) -> float:
        return self.seg_start[self.tok_seg[tok]]

    def raw_span(self, m: Match) -> str:
        return " ".join(self.raw_words[self.tok_raw[m.start]:self.tok_raw[m.end] + 1])


def _unsafe_mismatch(a: str, b: str) -> bool:
    return (any(ch.isdigit() for ch in a) or any(ch.isdigit() for ch in b)
            or a in _NEGATIONS or b in _NEGATIONS)


def check_claim(index: TranscriptIndex, claim: Claim) -> tuple[Claim | None, str]:
    """Return (verified claim or None, reason). Reason is "ok", "exact", "fuzzy" or a rejection code."""
    if not (claim.claim or "").strip():
        return None, "empty_claim"
    q = normalize(claim.quote or "").split()
    if len(q) < MIN_QUOTE_WORDS:
        return None, "short_quote"
    m = index.find(q, claim.t if isinstance(claim.t, (int, float)) else None)
    if m is None:
        return None, "not_found"
    quote = claim.quote.strip() if m.exact else index.raw_span(m)
    verified = replace(claim, t=float(index.segment_start(m.start)), quote=quote, verified=True)
    return verified, "exact" if m.exact else "fuzzy"


def verify_claims(claims: list[Claim], transcript: Transcript) -> tuple[list[Claim], list[Claim]]:
    """Split claims into (kept, rejected). Kept claims are copies with verified=True and t = match segment start."""
    index = TranscriptIndex(transcript)
    kept: list[Claim] = []
    rejected: list[Claim] = []
    for c in claims:
        ok, _ = check_claim(index, c)
        (kept if ok else rejected).append(ok or c)
    return kept, rejected
