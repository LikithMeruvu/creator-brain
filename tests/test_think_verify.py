from cbrain.models import Claim, Segment, Transcript
from cbrain.think.verify import normalize, verify_claims


def tr(*texts, step=10.0):
    segs = [Segment(start=i * step, end=(i + 1) * step, text=t) for i, t in enumerate(texts)]
    return Transcript(video_id="vid", segments=segs, source="auto_caption")


def claim(quote, text="Eat enough protein.", t=0.0):
    return Claim(creator="c", video_id="vid", t=t, claim=text, quote=quote, topic="Protein Intake")


def one(transcript, c):
    kept, rejected = verify_claims([c], transcript)
    return (kept[0] if kept else None), rejected


# ---- normalize -------------------------------------------------------------------------------------------------

def test_normalize_basics():
    assert normalize("  Hello,   WORLD!!  ") == "hello world"
    assert normalize("Don’t stop — it’s “great”") == "dont stop its great"
    assert normalize("high-intensity training") == "high intensity training"
    assert normalize("it&#39;s fine") == "its fine"


def test_normalize_caption_artifacts():
    assert normalize("so [Music] we start [Applause and cheering] now") == "so we start now"
    assert normalize("♪ la la ♪ ok (laughter) go") == "la la ok go"


# ---- exact matches ---------------------------------------------------------------------------------------------

def test_exact_match_sets_t_to_segment_start_and_verified():
    t = tr("welcome back everyone", "you should eat about one gram of protein per pound", "of body weight daily")
    k, _ = one(t, claim("eat about one gram of protein per pound"))
    assert k.verified is True
    assert k.t == 10.0
    assert k.quote == "eat about one gram of protein per pound"


def test_quote_spanning_segments_uses_start_segment():
    t = tr("intro words here", "you should eat about one gram", "of protein per pound of body weight", "outro")
    k, _ = one(t, claim("eat about one gram of protein per pound"))
    assert k is not None and k.t == 10.0


def test_quote_spanning_three_segments():
    t = tr("aaa bbb ccc", "ddd eee", "fff ggg hhh")
    k, _ = one(t, claim("ccc ddd eee fff ggg"))
    assert k.t == 0.0


def test_curly_quotes_and_apostrophes():
    t = tr("and honestly you don't need to count every single calorie to lose fat")
    k, _ = one(t, claim("you don’t need to count every single calorie"))
    assert k is not None


def test_transcript_with_curly_quote_llm_with_straight():
    t = tr("and honestly you don’t need to count every single calorie to lose fat")
    k, _ = one(t, claim("you don't need to count every single calorie"))
    assert k is not None


def test_auto_caption_without_punctuation_or_case():
    t = tr("so the first thing is protein you want about", "point eight grams per pound every day okay")
    k, _ = one(t, claim("The first thing is protein: you want about point eight grams per pound"))
    assert k is not None and k.t == 0.0


def test_quote_with_punctuation_matches_unpunctuated_transcript():
    t = tr("if you train hard and eat enough protein you will build muscle")
    k, _ = one(t, claim("If you train hard, and eat enough protein, you will build muscle."))
    assert k is not None


def test_caption_artifact_inside_transcript_is_ignored():
    t = tr("you need to eat more protein", "[Music]", "than you think to build muscle")
    k, _ = one(t, claim("you need to eat more protein than you think to build muscle"))
    assert k is not None and k.t == 0.0


def test_html_entities_in_transcript():
    t = tr("you don&#39;t need supplements to make progress in the gym")
    k, _ = one(t, claim("you don't need supplements to make progress"))
    assert k is not None


def test_word_boundaries_respected():
    t = tr("breathe in the said that thing now")
    k, rej = one(t, claim("he said that thing now ok"))
    assert k is None and len(rej) == 1


def test_repeated_phrase_picks_match_nearest_claimed_time():
    phrase = "make sure you get enough sleep every night"
    t = tr(phrase, "filler filler filler", "more filler words", phrase)
    k, _ = one(t, claim(phrase, t=29.0))
    assert k.t == 30.0
    k, _ = one(t, claim(phrase, t=1.0))
    assert k.t == 0.0


def test_exact_keeps_llm_quote_text():
    t = tr("so you really should lift weights three times per week")
    k, _ = one(t, claim("You really should lift weights three times per week."))
    assert k.quote == "You really should lift weights three times per week."


# ---- the single tolerance --------------------------------------------------------------------------------------

LONG = "you should aim for about one gram of protein per pound of body weight each and every day"  # 18 words


def test_one_misheard_word_accepted_and_real_words_stored():
    t = tr("okay so", "You should aim for about one gram of protein per pond of body weight each and every day.", "bye")
    k, _ = one(t, claim(LONG))
    assert k is not None and k.verified
    assert k.quote == "You should aim for about one gram of protein per pond of body weight each and every day."
    assert "pond" in k.quote and "pound" not in k.quote
    assert k.t == 10.0


def test_fuzzy_works_across_segments_and_stores_real_span():
    t = tr("so you should aim for about one gram", "of protein per pond of body weight", "each and every day thanks")
    k, _ = one(t, claim(LONG))
    assert k is not None
    assert k.quote == "you should aim for about one gram of protein per pond of body weight each and every day"
    assert k.t == 0.0


def test_two_wrong_words_in_short_quote_rejected():
    t = tr("you should aim for about one gram of protein per pond of body wait each")
    k, _ = one(t, claim("you should aim for about one gram of protein per pound of body weight each"))
    assert k is None


def test_short_quote_cannot_use_tolerance():
    # 8 words, 1 wrong = 87.5% < 90%
    t = tr("eat plenty of green vegetables every single week")
    k, rej = one(t, claim("eat plenty of green vegetables every single day"))
    assert k is None and rej


def test_mismatch_on_number_is_never_tolerated():
    t = tr("you should aim for about three grams of protein per pound of body weight each and every day")
    k, _ = one(t, claim(LONG.replace("one", "five")))
    assert k is None


def test_mismatch_on_negation_is_never_tolerated():
    t = tr("you do not need about one gram of protein per pound of body weight each and every day ok")
    k, _ = one(t, claim("you do now need about one gram of protein per pound of body weight each and every day"))
    assert k is None


# ---- rejections ------------------------------------------------------------------------------------------------

def test_fabricated_quote_rejected():
    t = tr("we talked about training today and nothing else really matters here")
    k, rej = one(t, claim("creatine is the single most effective supplement ever"))
    assert k is None and rej[0].verified is False


def test_quote_under_five_words_rejected():
    t = tr("eat more protein every day please")
    k, _ = one(t, claim("eat more protein daily"))
    assert k is None
    k, _ = one(t, claim("eat more protein"))
    assert k is None


def test_exactly_five_words_accepted():
    t = tr("so eat more protein every day please")
    k, _ = one(t, claim("eat more protein every day"))
    assert k is not None


def test_empty_claim_rejected():
    t = tr("eat more protein every day please")
    for text in ("", "   "):
        k, _ = one(t, claim("eat more protein every day", text=text))
        assert k is None


def test_empty_quote_and_empty_transcript():
    t = tr("eat more protein every day please")
    assert one(t, claim(""))[0] is None
    assert one(tr(), claim("eat more protein every day"))[0] is None


def test_punctuation_only_difference_in_wording_is_not_enough_if_words_differ():
    t = tr("you must eat protein with every single meal today")
    k, _ = one(t, claim("you must eat protein in every single meal today"))
    assert k is None  # 9 words, one wrong -> 88.9%


def test_kept_and_rejected_partition_preserves_inputs():
    t = tr("eat more protein every day please", "sleep eight hours every night for recovery")
    good = claim("eat more protein every day please", text="Eat protein")
    bad = claim("totally made up sentence about nothing at all", text="Fake")
    kept, rejected = verify_claims([good, bad], t)
    assert [c.claim for c in kept] == ["Eat protein"] and [c.claim for c in rejected] == ["Fake"]
    assert good.verified is False and rejected[0] is bad  # originals untouched
