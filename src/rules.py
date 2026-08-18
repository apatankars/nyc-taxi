"""Trip-quality rules, kept as data rather than code.

Each rule is a row in Taxi.TripQuality, so changing a threshold is an UPDATE plus
a re-run of stage 4, not a code edit.

Every rule carries:

  phase     'ingest' rules are decided while casting TripRaw -> Trip, where the
            original text still exists. 'rule' rules are SQL predicates over the
            typed Trip table and can be re-run without touching the load.
  severity  'invalid' means the value cannot describe a real trip; 'unusual' means
            suspicious but possible (e.g. negative fares are refunds/voids, real
            business events -- flag them, do not treat them as errors).
"""

# (rule_id, bit, rule_name, phase, severity, predicate, description)
#
# `predicate` is a SQL boolean expression over Taxi.Trip for phase='rule', None
# for phase='ingest' (the cast stage decides those directly).
#
# `bit` is the rule's position in Trip.flag_mask, declared explicitly rather than
# derived from list order so inserting a rule cannot renumber stored masks.
RULES = [
    # ---- ingest phase: evidence exists only during casting -------------------
    (1, 0, "cast_failed", "ingest", "invalid", None,
     "At least one field could not be coerced to its declared type."),
    (2, 1, "distance_reformatted", "ingest", "unusual", None,
     "trip_distance arrived with a thousands separator, e.g. '1,571.97'. Marks a "
     "distinct upstream provenance, and correlates with implausible magnitude."),
    (3, 2, "meter_metadata_missing", "ingest", "unusual", None,
     "passenger_count, RatecodeID, store_and_fwd_flag, payment_type and "
     "congestion_surcharge are all absent together -- a feed lacking meter data."),

    # ---- rule phase: SQL predicates over the typed table --------------------
    (10, 3, "distance_nonpositive", "rule", "invalid",
     "trip_distance <= 0",
     "A completed trip cannot cover zero or negative miles."),
    (11, 4, "distance_implausible", "rule", "unusual",
     "trip_distance > 100",
     "Over 100 miles. Possible for a long-haul run, but far outside the green-taxi norm."),
    (12, 5, "fare_negative", "rule", "unusual",
     "fare_amount < 0",
     "Negative fare. Almost certainly a refund or void, not corruption."),
    (13, 6, "total_negative", "rule", "unusual",
     "total_amount < 0",
     "Negative total. Same refund/void reading as fare_negative."),
    (14, 7, "fare_high", "rule", "unusual",
     "fare_amount > 500",
     "Fare above $500. Extraordinary but not impossible."),
    # YEAR(...) rather than a range comparison against date strings. A TIMESTAMP
    # column stores an encoded 64-bit integer, so `pickup_ts < '2023-01-01'`
    # compares '1154152267907846976' to '2023-01-01' *lexically* -- '1' sorts
    # before '2', so it matched all 787,060 rows instead of 7. Silent wrong
    # answer, no error. Wrap the column in a date function, or cast the literal.
    (15, 8, "pickup_outside_2023", "rule", "invalid",
     "pickup_ts IS NOT NULL AND YEAR(pickup_ts) <> 2023",
     "Pickup timestamp falls outside the 2023 reporting year."),
    (16, 9, "duration_nonpositive", "rule", "invalid",
     "duration_min IS NOT NULL AND duration_min <= 0",
     "Drop-off at or before pickup."),
    (17, 10, "duration_excessive", "rule", "unusual",
     "duration_min > 360",
     "Longer than six hours. Usually a meter left running."),
    (18, 11, "speed_implausible", "rule", "invalid",
     "implied_mph IS NOT NULL AND implied_mph > 80",
     "Implied average speed above 80 mph. Catches distance/time pairs that each "
     "look fine alone."),
    (19, 12, "speed_crawling", "rule", "unusual",
     "implied_mph IS NOT NULL AND implied_mph < 1 AND duration_min > 5",
     "Under 1 mph sustained for over five minutes."),
    (20, 13, "passenger_count_zero", "rule", "unusual",
     "passenger_count = 0",
     "A trip recorded with no passengers."),
    (21, 14, "tip_negative", "rule", "unusual",
     "tip_amount < 0",
     "Negative tip."),
    (22, 15, "fare_per_mile_extreme", "rule", "unusual",
     "trip_distance > 0.5 AND fare_amount > 0 AND (fare_amount / trip_distance) > 50",
     "Over $50 per mile. Compound distance/fare disagreement."),
]

INGEST_RULES = {r[2]: r[0] for r in RULES if r[3] == "ingest"}
BITS = {r[2]: r[1] for r in RULES}
IDS = {r[2]: r[0] for r in RULES}

# Which rules compromise which *measure*. Relevance is per-metric, not global: a
# trip flagged passenger_count_zero is still good evidence for a fare comparison.
# Filtering on flag_count = 0 for every question throws away usable rows -- and a
# *biased* subset, since flagged rows cluster. So each measure names only the
# rules that actually undermine it.
MEASURE_RULES = {
    # A trip existing is the fact being counted; a broken meter reading does not
    # mean nobody got in the cab. Only an out-of-year pickup disqualifies the row.
    "count": ["pickup_outside_2023"],

    # distance_reformatted is here on provenance grounds, not arithmetic: those
    # rows came through a different upstream path and carry the absurd magnitudes.
    "distance": ["cast_failed", "distance_nonpositive", "distance_implausible",
                 "distance_reformatted", "speed_implausible"],

    "fare": ["cast_failed", "fare_negative", "total_negative", "fare_high",
             "fare_per_mile_extreme"],

    "duration": ["cast_failed", "duration_nonpositive", "duration_excessive",
                 "speed_implausible", "speed_crawling"],

    "tip": ["cast_failed", "tip_negative"],
}

# --------------------------------------------------------------------------
# what each rule is an argument about
# --------------------------------------------------------------------------
# A flag is a claim about one number, so the only way to show what a flag means is
# to put the flagged rows' values for that number next to the values the unflagged
# rows report for the same number. FIELDS names the column behind each claim and
# the buckets to count it into; RULE_FIELD maps each rule onto one of them.
#
# `edges` are bucket boundaries, half-open [lo, hi), with an implicit '< first'
# bucket below and a '>= last' bucket above -- so the range is always covered and
# a flagged population that sits entirely outside the sensible range still lands
# somewhere. They are chosen to bracket *both* populations: unflagged rows fill
# the middle buckets, flagged rows pile into one tail, and that contrast is the
# whole point of the panel. Buckets empty in both populations are dropped before
# drawing, so one set of edges per column can afford to cover more range than any
# single rule needs.
#
# `mode` is 'range' (the default, bucket a continuous value) or 'value' (count
# distinct values -- for a small integer domain like a year, where a bucket would
# blur exactly the thing being shown).
FIELDS = {
    "trip_distance": {
        "label": "trip_distance", "unit": "mi", "expr": "t.trip_distance",
        # 0.01 splits the exact-zero spike (39,494 trips report 0 miles) off the
        # genuinely short trips; without it the two are one bar.
        "edges": [0, 0.01, 0.5, 1, 2, 3, 5, 10, 20, 50, 100, 1000],
    },
    "duration_min": {
        "label": "duration_min", "unit": "min", "expr": "t.duration_min",
        "edges": [0, 1, 5, 10, 20, 30, 45, 60, 120, 360],
    },
    "implied_mph": {
        "label": "implied_mph", "unit": "mph", "expr": "t.implied_mph",
        "edges": [0, 1, 5, 10, 15, 20, 25, 30, 50, 80],
    },
    "fare_amount": {
        "label": "fare_amount", "unit": "$", "expr": "t.fare_amount",
        "edges": [0, 0.01, 5, 10, 15, 20, 30, 50, 100, 250, 500],
    },
    "total_amount": {
        "label": "total_amount", "unit": "$", "expr": "t.total_amount",
        "edges": [0, 0.01, 5, 10, 15, 20, 30, 50, 100, 250, 500],
    },
    "tip_amount": {
        "label": "tip_amount", "unit": "$", "expr": "t.tip_amount",
        "edges": [0, 0.01, 1, 2, 3, 5, 10, 20],
    },
    "passenger_count": {
        "label": "passenger_count", "unit": "count", "integer": True,
        "expr": "t.passenger_count", "edges": [0, 1, 2, 3, 4, 5, 6],
    },
    # The compound rule's own quantity: neither fare nor distance alone is what it
    # objects to. Guarded, because the denominator is zero on 39,494 rows.
    "fare_per_mile": {
        "label": "fare_amount / trip_distance", "unit": "$/mi",
        "expr": ("CASE WHEN t.trip_distance > 0 "
                 "THEN t.fare_amount / t.trip_distance END"),
        "edges": [0, 1, 2, 3, 5, 10, 20, 50, 100],
    },
    # A year is a label, not a quantity: 'ratio': False stops the panel reporting
    # that out-of-year pickups average 0.99 times the in-year ones.
    "pickup_year": {
        "label": "YEAR(pickup_ts)", "unit": "year", "mode": "value",
        "expr": "YEAR(t.pickup_ts)", "ratio": False,
    },
}

# Rules with no single number behind them (cast_failed, meter_metadata_missing)
# point at fare_amount, which is the question actually being asked about them: the
# 55,613-trip meter-metadata block is only worth keeping if its fares look like
# everyone else's, and that comparison is the answer.
RULE_FIELD = {
    "cast_failed": "fare_amount",
    "distance_reformatted": "trip_distance",
    "meter_metadata_missing": "fare_amount",
    "distance_nonpositive": "trip_distance",
    "distance_implausible": "trip_distance",
    "fare_negative": "fare_amount",
    "total_negative": "total_amount",
    "fare_high": "fare_amount",
    "pickup_outside_2023": "pickup_year",
    "duration_nonpositive": "duration_min",
    "duration_excessive": "duration_min",
    "speed_implausible": "implied_mph",
    "speed_crawling": "implied_mph",
    "passenger_count_zero": "passenger_count",
    "tip_negative": "tip_amount",
    "fare_per_mile_extreme": "fare_per_mile",
}

# Lookup zones that resolve but carry no location: 264 is 'Unknown'/'N/A', 265 is
# 'N/A'/'Outside of NYC'. They pass every join and produce a named-but-unusable
# row -- worse than a NULL -- so zone rollups must exclude them deliberately.
UNKNOWN_ZONES = (264, 265)

# Cash fares record a tip of 0.00 in 100% of cases because a cash tip never
# reaches the meter: unobserved, not zero. Averaging over all payment types halves
# the answer, so tip analysis is restricted to card payments.
CARD_PAYMENT = 1


def seed(exec_dml):
    """Replace the rules table contents with the definitions above."""
    exec_dml("DELETE FROM Taxi.TripQuality")
    for rule_id, bit, name, phase, severity, predicate, description in RULES:
        exec_dml(
            "INSERT INTO Taxi.TripQuality "
            "(rule_id, bit_position, rule_name, phase, severity, predicate, "
            " description, enabled) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            rule_id, bit, name, phase, severity,
            "" if predicate is None else predicate, description,
        )
    return len(RULES)
