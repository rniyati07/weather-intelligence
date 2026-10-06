"""ChatOrchestrator — the conversational travel assistant use case.

Orchestrates the full chat flow:
1. Load/create conversation
2. Gemini reads conversation history + TripContext + the new message and
   extracts a structured request in one call (destination, dates, duration,
   interests, travel style, pace, search area, intent) — a deterministic
   regex heuristic runs only if that call itself fails (see `EntityExtractor`)
3. Backend validates/normalizes the extraction (date parsing, the existing
   date-range rules, geocoding, destination-ambiguity detection) and updates
   TripContext
4. If incomplete: generate clarifying question
5. If complete: resolve intent (Gemini's classification, with a keyword
   fallback), fetch only the backend capabilities that intent needs
6. Generate conversational response via Gemini, grounded in only that data
   (structured fallback on failure)
7. Save conversation state

This keeps one orchestrator and two Gemini calls per turn — understanding,
then response generation — never an agent loop or tool-calling framework
(Bible ADR-006/011). Gemini never touches a coordinate, a weather number, a
score, or a place name directly: it reads raw text out of a message and
writes raw text back in the response; every fact in between is fetched or
computed by the backend and merely restated.
"""

import asyncio
import contextlib
import json
import logging
import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from app.application.use_cases.cached_intelligence import IntelligenceUseCase
from app.domain.entities.attractions import (
    Attraction,
    AttractionRecommendation,
    AttractionType,
)
from app.domain.entities.conversation import Conversation, Message
from app.domain.entities.trip import (
    MISSING_DESTINATION,
    MISSING_END_DATE,
    MISSING_START_DATE,
    GeocodedPlace,
    TripContext,
    parse_iso_date,
)
from app.domain.entities.weather_intelligence import WeatherIntelligence
from app.domain.ports.attractions import AttractionPort
from app.domain.ports.conversation import ConversationRepository
from app.domain.ports.geocoding import GeocodingPort
from app.domain.rules.date_range import (
    ForecastCoverage,
    InvalidDateRangeError,
    forecast_coverage,
    validate_trip_dates,
)
from app.domain.rules.intent import ChatIntent, classify_intent
from app.infrastructure.ai.llm_client import LlmClient, LlmClientError, LlmTimeoutError

logger = logging.getLogger(__name__)


class _MalformedExtractionError(Exception):
    """Gemini's extraction call returned syntactically valid JSON that
    wasn't a JSON object — treated the same as a call failure, never as
    "nothing extracted"."""


def _format_history(messages: Sequence[Message]) -> str:
    """Render a bounded window of turns as `Role: text` lines.

    Shared by the extraction prompt and the response-generation prompt so
    the two Gemini calls see conversation history in exactly the same
    shape — the "context understanding" the extraction call does and the
    "final response" the second call writes are reading the same story.
    """
    lines = [f"{'User' if msg.role == 'user' else 'Assistant'}: {msg.content}" for msg in messages]
    return "\n".join(lines) if lines else "(start of conversation)"


def _uv_category(uv_index: float) -> str:
    """WHO UV Index categories — a public, standard scale, not invented."""
    if uv_index >= 11:
        return "extreme"
    if uv_index >= 8:
        return "very high"
    if uv_index >= 6:
        return "high"
    if uv_index >= 3:
        return "moderate"
    return "low"


def _local_clock_time(iso_utc: str | None, timezone_name: str | None) -> str | None:
    """A UTC ISO datetime (Open-Meteo's `sunrise`/`sunset` shape) rendered in
    the destination's own local time — a bare UTC timestamp would mislead
    rather than help ("sunset at 12:53" means nothing to a traveler unless
    it's already in their destination's clock). Falls back to UTC if the
    destination's timezone wasn't resolved, rather than guessing one."""
    if not iso_utc:
        return None
    try:
        moment = datetime.fromisoformat(iso_utc)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    if timezone_name:
        with contextlib.suppress(KeyError, ValueError):
            moment = moment.astimezone(ZoneInfo(timezone_name))
    return moment.strftime("%I:%M %p").lstrip("0")


#: Turns kept in the prompt's conversation-history section. Bounded so a long
#: conversation doesn't grow the prompt (and therefore cost/latency)
#: unboundedly — recent context matters far more than early context once a
#: trip's essentials are already captured in `TripContext`.
_HISTORY_TURNS_IN_PROMPT = 6

#: Structured extraction is a read-and-report task, not a creative one — a
#: low temperature makes it far less likely to drop an explicitly-stated
#: destination or date on an unlucky sampling draw. Narrative generation
#: keeps the provider's own default (unset), since that response wants
#: natural variation; this constant is for the extraction call only.
_EXTRACTION_TEMPERATURE = 0.1

#: Places referenced per day when building the LLM prompt AND when
#: populating the structured `places` field on the response — one shared
#: cap so the two can never drift apart: the reply can't mention a place
#: absent from the structured list, and vice versa.
_PLACES_PER_DAY_IN_RESPONSE = 3

#: Same shared cap, raised for a turn that explicitly asked for detail —
#: matches `attraction_matching._MAX_PLACES_PER_DAY`, the most the engine
#: ever ranks for one day, so "raised" still means "every real place we
#: actually have", never an invented one.
_MAX_PLACES_PER_DAY_IN_RESPONSE = 6

#: A user who explicitly asks for a checklist, a full itinerary, or "every
#: day" wants a complete structured answer, not the terse 2-3 sentences
#: every other question gets. Live testing found the fixed terse/no-list
#: prompt rules actively fighting these requests: a packing "checklist" ask
#: got an outright refusal (the model had real data but no way to present
#: it within "no bullet lists, 3 sentences"), and "3 restaurants each day"
#: still got one restaurant per day because "name at most two or three
#: places" is a *whole-response* cap, not per-day.
_DETAIL_REQUEST_KEYWORDS = (
    "checklist", "itinerary", "detailed", "in detail", "guidelines",
    "each day", "every day", "day by day", "day-by-day", "full plan",
    "breakdown", "all the", "everything",
)


def _wants_detailed_response(message: str) -> bool:
    lowered = message.lower()
    return any(keyword in lowered for keyword in _DETAIL_REQUEST_KEYWORDS)


#: Live-observed regression: once any specific interest (e.g. "food") gets
#: recorded on the trip, it never narrows back — `TripContext.interests`
#: only ever accumulates — so a later, broader ask ("proper itinerary with
#: viewpoints, cafes, beaches, monuments, things to do") stayed scoped to
#: just that one earlier category forever, and every place search kept
#: returning restaurants only. A message that explicitly asks "what's here"
#: in general must widen the search back out, not stay crowded out by
#: whatever narrow thing was mentioned first.
#: "itinerary" itself belongs here too, not just its synonyms — live-observed:
#: the turn that *establishes* a trip is always forced to `TRIP_PLANNING`
#: intent regardless of wording (so the trip-establishing turn always gets
#: full treatment; see `process_message`'s `was_complete_before_this_turn`
#: branch), which means "plan a trip to X, love food, give me a detailed
#: itinerary" never reaches the `ITINERARY_REQUEST` intent check below on
#: that first turn — only the phrase match here does.
_BROAD_ITINERARY_PHRASES = (
    "things to do", "places to visit", "places to see", "what to see",
    "what to do", "sightseeing", "what all is there", "itinerary",
    "day by day", "day-by-day", "full plan",
)
_BROAD_ITINERARY_SPREAD = (
    AttractionType.LANDMARK,
    AttractionType.VIEWPOINT,
    AttractionType.BEACH,
    AttractionType.MUSEUM,
    AttractionType.CULTURAL_SITE,
    AttractionType.RESTAURANT,
    AttractionType.SPORTS_FACILITY,
)


def _wants_broad_itinerary(message: str) -> bool:
    lowered = message.lower()
    return any(phrase in lowered for phrase in _BROAD_ITINERARY_PHRASES)


#: How many of the most recent user turns `_recent_user_text` joins.
_RECENT_USER_TURNS_FOR_BREADTH_CHECK = 3


def _recent_user_text(conversation: Conversation) -> str:
    """The last few user turns, joined — live-observed: a from-scratch
    itinerary ask that also names an ambiguous destination ("Udaipur,
    Rajasthan, give me a detailed itinerary") gets its category breadth
    computed on the *next* turn, once the destination is actually resolved —
    but that turn's own text is just the disambiguation reply ("1"), which
    carries none of the original wording. Checking `_wants_broad_itinerary`
    against the last few turns rather than only the current one means the
    itinerary ask one turn back still counts."""
    user_messages = [m.content for m in conversation.messages if m.role == "user"]
    return " ".join(user_messages[-_RECENT_USER_TURNS_FOR_BREADTH_CHECK:])

#: Destinations offered when the user asks for help choosing one — few
#: enough to compare at a glance.
_MAX_DESTINATION_SUGGESTIONS = 4

#: Some variety is wanted when suggesting destinations (unlike extraction),
#: but not so much that the picks drift from what the user asked for.
_SUGGESTION_TEMPERATURE = 0.5

#: Also how tests tell this JSON-mode call apart from the extraction call.
_SUGGESTION_USER_CONTENT = "Suggest destinations now."

#: When no verified suggestion could be produced — honest about why, and
#: still gives the user a way forward. Never a hardcoded list of places.
_SUGGESTIONS_UNAVAILABLE = (
    "I'd be happy to suggest a few destinations, but I couldn't put a list together "
    "just now. Try asking again in a moment — or, if you already have a place in mind, "
    "just tell me where."
)

#: How many geocoding candidates a destination-clarification turn offers.
#: Mirrors `GeocodingPort.DEFAULT_SEARCH_LIMIT` — enough to disambiguate
#: without turning the question into a wall of options.
_MAX_CLARIFICATION_CANDIDATES = 5

#: Words a clarification reply might use instead of a bare digit ("the
#: first one"). Small and closed on purpose — anything not recognized here
#: falls through to re-asking rather than guessing which one was meant.
_ORDINAL_WORDS: dict[str, int] = {
    "first": 0, "1st": 0,
    "second": 1, "2nd": 1,
    "third": 2, "3rd": 2,
    "fourth": 3, "4th": 3,
    "fifth": 4, "5th": 4,
}


def _is_ambiguous_candidates(candidates: list[GeocodedPlace], query: str) -> bool:
    """True when the top geocoding candidates plausibly name different places
    the user could actually mean.

    Country-diversity alone isn't enough: a fuzzy, population-ranked
    geocoder returns loosely similar-*sounding* places from anywhere in the
    world alongside a genuine match — live-observed: "varkala kerala"
    returning both "Varkala, India" (the obvious match) and
    "Varkalabiškės, Lithuania" (an unrelated village that happens to fold
    close enough to rank). Asking the user to disambiguate against a place
    they were never talking about is not the same failure mode as the
    intended case: a bare "Paris" resolving to Paris, France vs. Paris,
    Texas, where BOTH candidates are literally named "Paris". Restricting
    the country-diversity check to candidates whose name actually appears in
    what the user typed keeps the Paris case working while dropping the
    Varkala one.

    Country alone under-triggers: live-observed, "Manali" returns a 35k-
    population Chennai suburb (Tamil Nadu) ranked above the 8k-population
    Himalayan hill station (Himachal Pradesh) tourists actually mean —
    Open-Meteo's population ranking has no notion of tourism relevance, and
    both candidates share a country, so a country-only check silently
    accepts the wrong one. Comparing (country, region) instead catches any
    same-country candidates that are genuinely different places, while two
    rows for the same real place (identical country and region) still
    collapse to one identity and stay non-ambiguous.
    """
    if len(candidates) < 2:
        return False
    folded_query = query.strip().casefold()
    contenders = [c for c in candidates if c.name.strip().casefold() in folded_query]
    if len(contenders) < 2:
        return False
    identities = {(c.country_code or c.country, c.admin1) for c in contenders}
    return len(identities) > 1


#: A `searchArea` this far or further from the established trip destination
#: is treated as a bad extraction, not a genuine "near X" recentering — see
#: `ChatOrchestrator._resolve_search_area`. Generous enough to cover a whole
#: metro area or a nearby town (a state-sized radius), tight enough to catch
#: a fuzzy geocoder match landing in the wrong country.
_MAX_SEARCH_AREA_DISTANCE_KM = 150.0
_EARTH_RADIUS_KM = 6371.0


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in kilometers."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def _clarification_label(candidate: GeocodedPlace) -> str:
    """A disambiguation-list line for one candidate — unlike
    `GeocodedPlace.display_name` (name + country only, fine for the normal
    single-result case), this always includes the region too. Live-observed:
    same-country candidates (the "Manali" case `_is_ambiguous_candidates`
    above now also catches) rendered as identical lines ("1. Manali, India"
    through "5. Manali, India") with `display_name` alone — nothing let the
    user actually tell them apart.
    """
    parts = [candidate.name, candidate.admin1, candidate.country]
    return ", ".join(part for part in parts if part)


def _destination_label(context: TripContext) -> str:
    if context.destination is not None:
        return context.destination.display_name
    return context.destination_query or "your destination"


def _days_phrase(days: int) -> str:
    if days == 1:
        return "a day"
    return f"{days} days"


def _day_label(day: date) -> str:
    """"October 10" — the year only when it isn't the current one."""
    label = f"{day:%B} {day.day}"
    return label if day.year == datetime.now(UTC).year else f"{label}, {day.year}"


def _date_span(start: date, end: date) -> str:
    """ "October 10–14", "October 30 – November 2", or a single day."""
    if start == end:
        return _day_label(start)
    if (start.year, start.month) == (end.year, end.month):
        return f"{_day_label(start)}–{end.day}"
    return f"{_day_label(start)} – {_day_label(end)}"


def _geocoded_place_to_dict(place: GeocodedPlace) -> dict[str, object]:
    """JSON-safe form of a candidate, for `Message.metadata` round-tripping.

    Mirrors the shape `persistence/repositories.py` already uses to store
    `TripContext.destination` — same fields, same names, so a future reader
    isn't left guessing why two near-identical serializations exist.
    """
    return {
        "name": place.name,
        "latitude": place.latitude,
        "longitude": place.longitude,
        "country": place.country,
        "country_code": place.country_code,
        "admin1": place.admin1,
        "timezone": place.timezone,
    }


#: What a good answer looks like, per intent.
#:
#: The same "2-5 paragraphs" instruction for every question is what produced
#: walls of prose that repeated the trip summary the interface already shows.
#: Each shape below says what to lead with and what to leave to the UI, so a
#: packing question gets a list and a day question gets a day — not the same
#: essay with different emphasis.
_INTENT_RESPONSE_SHAPES: dict[ChatIntent, str] = {
    ChatIntent.TRIP_PLANNING: (
        "give the headline verdict on the trip and what's driving it, like you're texting a "
        "friend the gist — the outlook, days and packing are already on screen, so add color "
        "rather than repeating the numbers"
    ),
    ChatIntent.ITINERARY_REQUEST: (
        "walk through the days like you're actually planning it with them — for each day, "
        "name the real spots and give each a little flavor: what it's known for, what's "
        "nearby, why it fits that day's weather. Easy to skim, not a dry list of names"
    ),
    ChatIntent.WEATHER_QUESTION: (
        "tell them what the day looks like and what that means for their plans, naturally"
    ),
    ChatIntent.RECOMMENDATION_REQUEST: (
        "recommend the real places above like you're tipping off a friend — what each is "
        "known for, why it's worth going, what's nearby. If none were provided, say so "
        "warmly and suggest what the weather makes sensible instead — never a bare 'None'"
    ),
    ChatIntent.PACKING_REQUEST: (
        "walk through what to pack and why, conversationally — a natural list is great here"
    ),
    ChatIntent.GENERAL_CHAT: "keep it short and friendly, like a quick reply",
}


def _attraction_to_dict(attraction: Attraction) -> dict[str, object]:
    """JSON-safe form of a place, for `Message.metadata` round-tripping.

    Field names match `PlaceSchema`'s wire shape rather than `Attraction`'s
    internals, so the metadata a restored conversation yields is the same
    shape the live `ChatResponse` carries and the frontend needs no second
    parser for it.

    Persisted because places are otherwise per-turn only: they are computed
    inside a chat turn and discarded, so reopening a conversation left the
    trip's places permanently empty even though the backend had found real
    ones. The trip workspace is trip-level state, not a property of whichever
    message happened to fetch it.
    """
    return {
        "name": attraction.name,
        "type": attraction.type.value,
        "latitude": attraction.latitude,
        "longitude": attraction.longitude,
        "address": attraction.address,
        "weatherSuitability": attraction.weather_suitability.value,
        "reason": attraction.weather_notes or "",
    }


@dataclass(frozen=True, slots=True)
class ChatResult:
    """Result of processing a chat message."""

    conversation: Conversation
    response: str
    context_complete: bool
    missing_essentials: tuple[str, ...]
    intent: ChatIntent
    llm_generated: bool
    #: Real places the reply is grounded in — always a subset of what a
    #: `PlacesPort` call actually returned, never LLM-invented. Empty when
    #: this turn fetched no attractions (clarification turns, general chat).
    places: tuple[Attraction, ...] = ()
    #: Set only when this turn's response is a destination disambiguation
    #: question — the structured form of the candidates listed in `response`.
    destination_candidates: tuple[GeocodedPlace, ...] = ()


#: What `EntityExtractor._llm_extract`'s JSON keys (camelCase, matching the
#: structured-request field names) map onto internally — the rest of this
#: module works in snake_case throughout, so this is the one place the
#: convention boundary is crossed, and the one place an unrecognized or
#: `null` key is dropped rather than propagated.
_EXTRACTION_KEY_MAP: dict[str, str] = {
    "destination": "destination",
    "searchArea": "search_area",
    "startDate": "start_date",
    "endDate": "end_date",
    "duration": "duration",
    "interests": "interests",
    "travelStyle": "travel_style",
    "pace": "pace",
    "intent": "intent",
    "wantsDestinationSuggestions": "wants_suggestions",
}


#: Phrases meaning "I haven't picked a destination — help me choose", for
#: the fallback path only (the LLM path reports `wantsDestinationSuggestions`
#: itself). Only consulted while no destination is established, so
#: "recommend restaurants" on an active trip never reads as one.
_SUGGESTION_REQUEST_PHRASES = (
    "not sure where", "don't know where", "dont know where", "no idea where",
    "help me pick", "help me choose", "help me decide", "where should i go",
    "where should we go", "suggest a", "suggest some", "suggest somewhere",
    "recommend a destination", "recommend somewhere", "any ideas", "somewhere",
    "anywhere",
)

_MONTHS: dict[str, int] = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_MONTH = "(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
_RANGE_JOIN = r"\s*(?:to|-|–|until|till|through|thru)\s*"
#: "10-14 october", "10th to 14th oct" — checked first so the start day is
#: not lost to the single-date pattern matching only "14 october".
_DAY_RANGE_THEN_MONTH_RE = re.compile(rf"\b{_DAY}{_RANGE_JOIN}{_DAY}\s+(?:of\s+)?{_MONTH}\b", re.I)
_MONTH_DAY_RE = re.compile(rf"\b{_MONTH}\s+{_DAY}\b(?:,?\s*(\d{{4}}))?", re.I)
_DAY_MONTH_RE = re.compile(rf"\b{_DAY}\s+(?:of\s+)?{_MONTH}\b(?:,?\s*(\d{{4}}))?", re.I)
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
#: The bare end day of "october 10 to 14" — never a trip length ("to 5 days").
_BARE_END_DAY_RE = re.compile(rf"^{_RANGE_JOIN}{_DAY}\b(?!\s*(?:days?|nights?|weeks?))", re.I)


def _calendar_date(year: int | None, month: int, day: int, *, not_before: date) -> date | None:
    """A yearless date resolves to its nearest occurrence on/after
    `not_before` — the same rule the extraction prompt gives the LLM."""
    try:
        if year is not None:
            return date(year, month, day)
        candidate = date(not_before.year, month, day)
        if candidate < not_before:
            candidate = date(not_before.year + 1, month, day)
        return candidate
    except ValueError:
        return None


def _fallback_dates(message: str, today: date) -> tuple[date | None, date | None]:
    """Start/end dates from plain phrasing — "october 10", "Oct 10 to 14",
    "10-14 october", "2026-10-10", "tomorrow". Fallback path only.

    Unrecognized phrasing yields `(None, None)` — never a guess.
    """
    found: list[tuple[int, date]] = []
    taken: list[tuple[int, int]] = []

    def claim(span: tuple[int, int]) -> bool:
        if any(span[0] < end and start < span[1] for start, end in taken):
            return False
        taken.append(span)
        return True

    for match in _DAY_RANGE_THEN_MONTH_RE.finditer(message):
        if not claim(match.span()):
            continue
        month = _MONTHS[match.group(3).lower()]
        start = _calendar_date(None, month, int(match.group(1)), not_before=today)
        if start is not None:
            end = _calendar_date(None, month, int(match.group(2)), not_before=start)
            found.append((match.start(), start))
            if end is not None:
                found.append((match.start() + 1, end))

    for match in _ISO_DATE_RE.finditer(message):
        if claim(match.span()):
            parsed = _calendar_date(
                int(match.group(1)), int(match.group(2)), int(match.group(3)), not_before=today
            )
            if parsed is not None:
                found.append((match.start(), parsed))

    for pattern, month_group, day_group in ((_MONTH_DAY_RE, 1, 2), (_DAY_MONTH_RE, 2, 1)):
        for match in pattern.finditer(message):
            if not claim(match.span()):
                continue
            month = _MONTHS[match.group(month_group).lower()]
            year = int(match.group(3)) if match.group(3) else None
            parsed = _calendar_date(year, month, int(match.group(day_group)), not_before=today)
            if parsed is None:
                continue
            found.append((match.start(), parsed))
            tail = _BARE_END_DAY_RE.match(message[match.end() :])
            if tail and claim((match.end(), match.end() + tail.end())):
                end = _calendar_date(None, month, int(tail.group(1)), not_before=parsed)
                if end is not None:
                    found.append((match.end(), end))

    if not found:
        lowered = message.lower()
        if "tomorrow" in lowered:
            return today + timedelta(days=1), None
        if re.search(r"\btoday\b", lowered):
            return today, None
        return None, None

    ordered = [parsed for _, parsed in sorted(found, key=lambda item: item[0])]
    start = ordered[0]
    end = ordered[1] if len(ordered) > 1 else None
    if end is not None and end < start and end.year == start.year:
        # "December 28 to January 3" — the end rolls into the next year.
        end = _calendar_date(end.year + 1, end.month, end.day, not_before=start)
    return start, end


def _last_turn_asked_for(history: Sequence[Message], essential: str) -> bool:
    """Whether the previous assistant turn was a clarification asking for
    `essential` first — read from its structured metadata, not its wording."""
    for message in reversed(history):
        if message.role == "assistant":
            missing = message.metadata.get("missing_essentials")
            return isinstance(missing, list) and bool(missing) and missing[0] == essential
    return False


#: A reply this short that resolved a pending destination choice carries no
#: other trip information worth extracting ("2", "the second one", "Paris
#: France"). Anything longer ("1, but make it the 12th to the 16th") still
#: goes through extraction so the extra detail is not lost.
_MAX_BARE_SELECTION_WORDS = 4


def _is_bare_selection(message: str) -> bool:
    return len(message.split()) <= _MAX_BARE_SELECTION_WORDS and not re.search(
        r"\b(?:to|from|until|till|day|days|night|nights|week|weekend)\b", message, re.I
    )


def _answer_to_end_date_question(
    extracted: dict[str, Any],
    message: str,
    context: TripContext,
    history: Sequence[Message],
) -> dict[str, Any]:
    """A lone date given right after "when does your trip end?" is the end
    date — live-observed: "october 14" in reply to that question was read
    as a new *start* date (by the heuristic, and sometimes by the LLM),
    replacing October 10 and asking for the end date again.

    Only applies when the previous turn asked for the end date, a start is
    already known, the new date falls on or after it, and the message doesn't
    explicitly talk about the start — so "actually start on the 12th" still
    moves the start.
    """
    new_start = parse_iso_date(extracted.get("start_date"))
    if (
        new_start is None
        or re.search(r"\bstart", message, re.I)
        or extracted.get("end_date")
        or context.start_date is None
        or context.end_date is not None
        or new_start < context.start_date
        or not _last_turn_asked_for(history, MISSING_END_DATE)
    ):
        return extracted
    remapped = {key: value for key, value in extracted.items() if key != "start_date"}
    remapped["end_date"] = new_start.isoformat()
    return remapped


_LEADING_REPLY_FILLER_RE = re.compile(
    r"^(?:i(?:'d| would)? (?:like|want) to (?:go|visit)(?: to)?|let'?s (?:go to|do)|"
    r"how about|maybe|probably|i think|to|in)\s+",
    re.I,
)


def _bare_place_reply(message: str) -> str | None:
    """A short answer to "where would you like to go?" — "ooty", "Andaman
    Nicobar island" — taken as the place name itself, case and all.

    Only ever used when the previous turn asked for a destination, so
    "thanks" or "ok" in any other position never reaches the geocoder.
    """
    text = _LEADING_REPLY_FILLER_RE.sub("", message.strip()).strip(" .!?,")
    words = text.split()
    if not words or len(words) > 5 or any(ch.isdigit() for ch in text):
        return None
    if all(word.lower() in EntityExtractor._NON_DESTINATION_WORDS for word in words):
        return None
    return text


def _fold(value: str) -> str:
    """Case- and diacritic-insensitive form ("Andamān" -> "andaman")."""
    decomposed = unicodedata.normalize("NFD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


_PLACE_FILLER_TOKENS = frozenset({"the", "and", "of", "in", "near", "trip", "to", "a"})


def _place_tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", _fold(text))
    return {
        word[:-1] if len(word) > 3 and word.endswith("s") else word
        for word in words
        if word not in _PLACE_FILLER_TOKENS
    }


def _best_candidate(candidates: Sequence[GeocodedPlace], query: str) -> GeocodedPlace:
    """The candidate whose name and region best cover what the user wrote.

    Live-observed: "Andaman Nicobar island" came back from the geocoder with
    "Andamān" — a village in Madurai, Tamil Nadu — ranked above "Andaman
    Islands, Andaman and Nicobar", and taking the first result planned the
    trip for the wrong place. Counting how many of the query's words each
    candidate's name/region/country accounts for picks the one the user
    described; ties keep the provider's own order, so a plain single-word
    query behaves exactly as before.
    """
    wanted = _place_tokens(query)
    best = candidates[0]
    if not wanted:
        return best
    best_score = -1
    for candidate in candidates:
        described = " ".join(p for p in (candidate.name, candidate.admin1, candidate.country) if p)
        score = len(wanted & _place_tokens(described))
        if score > best_score:
            best, best_score = candidate, score
    return best


class EntityExtractor:
    """Turns one user message, read in context, into a structured request.

    Gemini is the primary path: one JSON-mode call sees the recent
    conversation, the `TripContext` already established, and the latest
    message, and returns destination/dates/duration/interests/travel style/
    pace/search area/intent as JSON — genuine natural-language
    understanding, not a regex over capitalized words. A small keyword/regex
    heuristic exists only as the fallback used when that call itself fails
    (network, timeout, or an unparseable response) — never blended with a
    successful Gemini result, and never run first. Dates returned by either
    path are strings — the orchestrator runs them through `parse_iso_date`
    before they ever reach `TripContext.merge`, which is typed `date | None`
    and does not coerce.
    """

    #: Sentence position alone is not a signal a word is a proper noun —
    #: "We are visiting Bali" capitalizes "We" for grammar, not because it
    #: names a place. Filtering by word alone (not position) is deliberate:
    #: "Goa, August 20 to 23" opens *with* the real destination, so dropping
    #: whatever comes first would break the more common case to fix the
    #: rarer one. Month names are included because a date phrase sitting
    #: next to the destination is exactly what triggers this path.
    #: Used only by the fallback heuristic below — Gemini needs no stopword
    #: list, it reads the sentence.
    _NON_DESTINATION_WORDS = frozenset(
        {
            "i", "am", "im", "going", "to", "visit", "visiting", "travel", "travelling",
            "traveling", "trip", "planning", "plan", "want", "like", "love", "for",
            "days", "day", "week", "weekend", "month", "we", "are", "my", "our", "us",
            "they", "their", "he", "she", "it", "this", "that", "next", "family",
            "friends", "parents", "of", "a", "an", "the", "and", "with",
            "january", "february", "march", "april", "may", "june", "july",
            "august", "september", "october", "november", "december",
            # Question words and common request phrasing — without these, a
            # follow-up question ("Which day is best?", "Can you find me a
            # hotel?") had its leading capitalized word misread as a
            # destination by this heuristic. Only reachable when the LLM call
            # itself fails (this heuristic is the fallback path), but a
            # question is exactly the shape of message that path must not
            # get wrong, since a mid-conversation follow-up is exactly when
            # a destination should almost never be re-extracted.
            "which", "what", "who", "where", "when", "how", "why", "can", "could",
            "would", "should", "do", "does", "did", "is", "was", "were",
            "you", "your", "me", "find", "any", "some", "good", "nice", "best",
            "places", "place",
            # Requests for help choosing, and conversational fillers — a
            # sentence-initial "Help" was geocoded to "Helpt, Germany".
            "help", "pick", "choose", "suggest", "recommend", "destination",
            "somewhere", "anywhere", "not", "sure", "yet", "quiet", "warm",
            "hi", "hello", "hey", "thanks", "thank", "please", "ok", "okay",
            "yes", "no", "today", "tomorrow",
        }
    )

    def __init__(self, llm_client: LlmClient) -> None:
        self._llm_client = llm_client

    async def extract(
        self,
        message: str,
        current_context: TripContext,
        history: Sequence[Message],
    ) -> dict[str, Any]:
        """Extract a structured request from `message`, in context.

        Returns a dict with optional keys: `destination` (str, raw text),
        `search_area` (str, raw text), `start_date` / `end_date` (str, the
        caller parses), `duration` (int, days), `interests` (list[str]),
        `travel_style` (str), `pace` (str), `intent` (str). A key's absence
        means the message added or changed nothing for that field — never
        "clear it" (`TripContext.merge`'s own contract).

        Falls back to the deterministic heuristic only when the Gemini call
        itself fails — a *successful* call that extracts nothing (a plain
        "thanks!") is a valid, empty result and must not trigger the
        fallback, which would reintroduce exactly the fragile-regex-as-
        primary behaviour this architecture replaces.
        """
        try:
            return await self._llm_extract(current_context, history)
        except (
            LlmClientError,
            LlmTimeoutError,
            json.JSONDecodeError,
            _MalformedExtractionError,
        ) as exc:
            logger.warning("entity_extraction_failed_using_fallback", extra={"error": str(exc)})
            return self._fallback_heuristic_extract(message, current_context, history)

    def _build_extraction_prompt(
        self, current_context: TripContext, history: Sequence[Message], *, today: date
    ) -> str:
        """The structured-extraction prompt — history and current
        `TripContext` in, one JSON object out.

        Explicitly tells the model not to repeat an already-established
        destination and not to conflate `searchArea` with `destination` —
        the two ambiguities most likely to cause a wrong or wasted
        downstream geocoding call if left to chance.

        `today` is passed in (not read from the clock here) so a year-less
        date like "September 10" has something to resolve against — without
        it the model has no year to anchor to and silently drops the date
        rather than guess (see `_llm_extract`'s caller). The model's
        resolution is a best-effort read of the message, never trusted
        outright: whatever ISO date comes back still goes through the same
        `parse_iso_date` + `validate_trip_date_range` the rest of the
        pipeline already applies to every date, LLM-sourced or not.
        """
        history_text = _format_history(history)
        dest_display = (
            current_context.destination.display_name
            if current_context.destination
            else "not yet known"
        )
        dest_query = current_context.destination_query or "not yet known"
        start_date = current_context.start_date
        start = start_date.isoformat() if start_date else "not yet known"
        end = current_context.end_date.isoformat() if current_context.end_date else "not yet known"
        interests = (
            ", ".join(current_context.interests) if current_context.interests else "none yet"
        )
        travel_style = current_context.travel_style or "not yet known"
        pace = current_context.pace or "not yet known"
        # ISSUE-5 (E2E audit): the bare enum-name list alone left "which day
        # is best?" and "what places can I visit?" both landing on
        # `itinerary_request` — a label with no definition is ambiguous
        # between "asking about one day" and "asking for the whole trip's
        # schedule". Each value gets one disambiguating clause instead.
        intent_guide = (
            'trip_planning (establishing or changing the trip itself — '
            "destination, dates, interests, style, pace), "
            "itinerary_request (an explicit ask for a day-by-day plan or "
            "schedule across the WHOLE trip, e.g. \"plan out each day\"), "
            "weather_question (asking about conditions for a day or the whole "
            "trip, which day is best/worst, or safety/risk — e.g. \"which day "
            'is best?", "what if it rains?", "is it safe to travel", "any '
            'risks I should know about" — not a request for places), '
            "recommendation_request (asking what places, activities, food, "
            "or stays to visit or do), "
            "packing_request (what to bring or wear), "
            "general_chat (ONLY genuine small talk with no trip-planning "
            "content — thanks, greetings, chit-chat — never a real question "
            "about the trip's weather, safety, places, or plan, even a vague "
            'or broadly-phrased one)'
        )

        return (
            "You are the understanding layer of a conversational travel-planning "
            "assistant. Read the conversation and extract structured information "
            "from the LATEST user message as a single JSON object. Return ONLY the "
            "JSON object — no prose, no markdown fences.\n\n"
            f"Today's date is {today.isoformat()}.\n\n"
            f"CONVERSATION (the last line is the message to extract from):\n{history_text}\n\n"
            "TRIP CONTEXT ALREADY ESTABLISHED — do not repeat a value back unless "
            "the latest message actually changes it:\n"
            f'- destination: {dest_display} (as the user first wrote it: "{dest_query}")\n'
            f"- dates: {start} to {end}\n"
            f"- interests: {interests}\n"
            f"- travel style: {travel_style}\n"
            f"- pace: {pace}\n\n"
            "Use the conversation above to resolve references in the latest message "
            'such as "it", "there", "the second day", or an implied trip the message '
            "doesn't restate. Return a JSON object with any of these keys, omitting a "
            "key entirely when the latest message does not add or change that "
            "information:\n\n"
            "{\n"
            '  "destination": "place name, ONLY if this message introduces a NEW '
            'or CHANGED trip destination — never the one already established above. '
            "Write it as the place is named on a map: fix spelling and capitalization, "
            "and when the user also named its state, region or country, put that after "
            'a comma (e.g. \\"ooty tamilnadu\\" -> \\"Ooty, Tamil Nadu\\")",\n'
            '  "searchArea": "a specific place WITHIN or NEAR the trip destination the '
            "user is asking about for just this turn (e.g. \"more places near Kochi\") "
            '— never the main trip destination, never set together with destination",\n'
            '  "startDate": "YYYY-MM-DD — only an explicit or unambiguous calendar date",\n'
            '  "endDate": "YYYY-MM-DD — only an explicit or unambiguous calendar date",\n'
            '  "duration": integer number of days, whenever the message states a trip '
            "length (e.g. \"4-day trip\"), even alongside a startDate — give BOTH "
            "together when the message has a start date but not an end date (e.g. "
            "\"4-day trip starting Oct 1\" -> startDate AND duration, never just one); "
            "omit duration only if the message already gives an explicit endDate,\n"
            '  "interests": ["short words naming ONLY what the latest message itself '
            "asks for — a leisure theme (e.g. a place, activity, or food style) or a "
            "practical need (e.g. lodging, a sport) count equally, but every word here "
            'must trace to something the user actually wrote this turn"],\n'
            '  "travelStyle": "solo | couple | family | friends | business — inferred '
            'from who is travelling",\n'
            '  "pace": "short free-text pace preference, e.g. relaxed, packed, '
            'moderate",\n'
            f'  "intent": "exactly one of: {intent_guide}",\n'
            '  "wantsDestinationSuggestions": true ONLY when no destination is established '
            "and the latest message asks for help choosing where to go, or describes the "
            "kind of place wanted without naming one (e.g. \"not sure where yet\", \"help "
            'me pick a destination", "somewhere warm and quiet"); omit it otherwise\n'
            "}\n\n"
            "Rules:\n"
            "- destination must be a place the user actually named. A description of what "
            "they want (\"somewhere warm\", \"a beach\", \"a destination\") or a word like "
            "\"help\" is never a destination — use wantsDestinationSuggestions instead.\n"
            "- If the assistant's previous message asked where the user wants to go, a short "
            "reply that names a place (even lowercase or abbreviated, e.g. \"ooty\") IS the "
            "destination.\n"
            "- duration: a trip length stated on its own (\"a weekend\" = 2, \"5 days\" = 5) "
            "is still a duration, even with no dates yet.\n"
            "- Never invent a date, destination, or fact not present in the message or "
            "directly implied by it. If dates are not mentioned, omit startDate and "
            "endDate entirely — do not guess or default.\n"
            "- When the message gives a calendar date without a year (e.g. \"September "
            "10\" or \"the 10th to the 13th\"), resolve it against today's date above: "
            "use the current year, unless that month/day has already passed this year, "
            "in which case use next year instead — always the nearest such date on or "
            "after today. A date that already includes an explicit year is used as-is.\n"
            "- destination and searchArea never both appear on the same turn.\n"
            "- destination and searchArea must be an actual place name (a city, region, "
            "country, or landmark) — never a pronoun, article, or question word (\"can\", "
            "\"any\", \"what\", \"which\", \"there\", \"it\"). A question that happens to "
            "start with one of those words is not a destination mention; when in doubt, "
            "omit the field.\n"
            "- interests: only words the LATEST message itself introduces. A message "
            "that doesn't mention any interest (e.g. \"which day is best?\") must omit "
            "interests entirely — never repeat interests already established earlier in "
            "the conversation above; the caller keeps those, so repeating them back "
            "changes nothing and risks fabricating ones the latest message never said.\n"
            "- intent must be exactly one of the listed values, nothing else.\n"
            "- Output strictly the JSON object, nothing else."
        )

    async def _llm_extract(
        self,
        current_context: TripContext,
        history: Sequence[Message],
    ) -> dict[str, Any]:
        prompt = self._build_extraction_prompt(
            current_context, history, today=datetime.now(UTC).date()
        )
        raw_text = await self._llm_client.complete(
            system_prompt=prompt,
            user_content="Extract the structured request from the latest message above.",
            json_mode=True,
            temperature=_EXTRACTION_TEMPERATURE,
        )
        parsed = json.loads(raw_text.strip())
        if not isinstance(parsed, dict):
            raise _MalformedExtractionError(f"expected a JSON object, got {type(parsed).__name__}")

        return {
            _EXTRACTION_KEY_MAP[key]: value
            for key, value in parsed.items()
            if key in _EXTRACTION_KEY_MAP and value is not None
        }

    def _fallback_heuristic_extract(
        self,
        message: str,
        current_context: TripContext,
        history: Sequence[Message] = (),
    ) -> dict[str, Any]:
        """Deterministic, no-network extraction — used only when the Gemini
        extraction call itself failed. Never blended with a successful
        Gemini result.

        Live-observed: this path runs whenever the LLM provider rate-limits
        a turn, which on a small per-minute token budget is every few turns
        — so it has to get the common conversational replies right, not
        just the first-turn sentence. Three failures came from it: "Help me
        pick a destination" geocoded "Help" (→ "Helpt, Germany"), a bare
        "october 10" reply lost its date because this path never read
        dates, and a lowercase "ooty" answer to "where would you like to
        go?" was ignored because only capitalized words were candidates.
        """
        msg_lower = message.lower()
        result: dict[str, Any] = {}

        wants_suggestions = current_context.destination is None and any(
            phrase in msg_lower for phrase in _SUGGESTION_REQUEST_PHRASES
        )
        if wants_suggestions:
            result["wants_suggestions"] = True
        elif current_context.destination is None:
            destination = self._extract_destination(message)
            if destination is None and _last_turn_asked_for(history, MISSING_DESTINATION):
                destination = _bare_place_reply(message)
            if destination:
                result["destination"] = destination

        start, end = _fallback_dates(message, datetime.now(UTC).date())
        if start is not None:
            result["start_date"] = start.isoformat()
        if end is not None:
            result["end_date"] = end.isoformat()

        duration_match = re.search(r"(\d+)\s*(?:days?|nights?)\b", msg_lower)
        if duration_match:
            result["duration"] = int(duration_match.group(1))
        elif "weekend" in msg_lower:
            result["duration"] = 2

        search_area = self._extract_search_area_text(message)
        if search_area and "destination" not in result:
            result["search_area"] = search_area

        interest_keywords = {
            "beach": ["beach", "beaches", "coast", "shore", "sea", "ocean", "swim"],
            "food": ["food", "eat", "restaurant", "cuisine", "dining", "seafood", "local food"],
            "museum": ["museum", "museums", "art", "gallery", "history", "culture"],
            "nightlife": ["nightlife", "club", "bar", "party", "drink", "pub"],
            "hiking": ["hike", "hiking", "trail", "trek", "mountain", "walk"],
            "nature": ["nature", "park", "wildlife", "bird", "forest", "garden"],
            "shopping": ["shop", "shopping", "market", "mall", "boutique"],
            "photography": ["photo", "photography", "picture", "instagram", "shot"],
            "adventure": ["adventure", "thrill", "extreme", "zipline", "rafting"],
            "wellness": ["spa", "wellness", "yoga", "meditation", "relax", "massage"],
            "family": ["family", "kid", "children", "child", "family-friendly"],
            "luxury": ["luxury", "luxurious", "high-end", "premium", "5-star"],
            "budget": ["budget", "cheap", "affordable", "backpack", "hostel"],
        }
        found_interests = [
            interest
            for interest, keywords in interest_keywords.items()
            if any(kw in msg_lower for kw in keywords)
        ]
        if found_interests:
            result["interests"] = found_interests

        style_keywords = {
            "solo": ["solo", "alone", "by myself"],
            "couple": ["couple", "partner", "boyfriend", "girlfriend", "husband", "wife"],
            "family": ["family", "kids", "children"],
            "friends": ["friends", "group", "buddies"],
            "business": ["business", "work", "conference", "meeting"],
        }
        for style, keywords in style_keywords.items():
            if any(kw in msg_lower for kw in keywords):
                result["travel_style"] = style

        return result

    def _extract_destination(self, message: str) -> str | None:
        """Capitalized-word heuristic, filtered against `_NON_DESTINATION_WORDS`.

        Position alone is not a reliable signal: "We are visiting Bali"
        capitalizes "We" for grammar, but "Goa, August 20 to 23" opens with
        a genuine destination — dropping whichever word comes first would
        fix one at the cost of breaking the other. The stopword list is the
        actual fix; a word is excluded because of *what it is*, never
        *where it sits*.

        Only a *contiguous run* of surviving candidates is ever joined into
        one destination — not just the first three found anywhere in the
        message. "New York" is one place because the two words sit right
        next to each other; "Kerala" and "India" in "Kerala in India" are
        not, because "in" — a real word, just not a capitalized one — sits
        between them. The previous version joined the first three
        candidates regardless of what separated them, which silently turned
        "Kerala in India" into the search query "Kerala India": ungeocodable
        (real bug, found live). A comma is still treated as the same run —
        "Kerala, India" is a standard disambiguating format, not a sentence
        break, and the geocoder's own ranking already expects a query shaped
        that way.
        """
        candidates = [
            match
            for match in re.finditer(r"\b[A-Z][a-z]+\b", message)
            if match.group().lower() not in self._NON_DESTINATION_WORDS
        ]
        if not candidates:
            return None

        best_run = current_run = [candidates[0]]
        for previous, current in zip(candidates, candidates[1:], strict=False):
            between = message[previous.end() : current.start()]
            current_run = [*current_run, current] if re.fullmatch(r"[\s,]*", between) else [current]
            if len(current_run) > len(best_run):
                best_run = current_run

        return " ".join(match.group() for match in best_run[:3])

    #: "near Panjim", "places in Calangute", "around Baga" — a sub-location
    #: mentioned in a follow-up, distinct from the trip destination itself.
    #: Deliberately separate from `_extract_destination`'s pattern (no
    #: preposition there) so a first-turn message ("trip to Goa") is never
    #: mistaken for a local-area query.
    _SEARCH_AREA_PATTERN = re.compile(
        r"\b(?:near|in|around)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,2})"
    )

    def _extract_search_area_text(self, message: str) -> str | None:
        """Extract a local sub-location the user wants to recenter on.

        "Give me more places near Panjim" -> "Panjim". Filtered against the
        same stopword set as destination extraction so "in August" (a date
        phrase, not a place) never matches — `_NON_DESTINATION_WORDS`
        already excludes every month name for exactly this reason. Fallback
        path only — Gemini's own `searchArea` field is the primary source.
        """
        match = self._SEARCH_AREA_PATTERN.search(message)
        if not match:
            return None
        words = [w for w in match.group(1).split() if w.lower() not in self._NON_DESTINATION_WORDS]
        return " ".join(words) if words else None


class ChatOrchestrator:
    """Main chat orchestration use case."""

    def __init__(
        self,
        *,
        conversation_repo: ConversationRepository,
        geocoding: GeocodingPort,
        intelligence_use_case: IntelligenceUseCase,
        attraction_provider: AttractionPort,
        llm_client: LlmClient,
        max_forecast_horizon_days: int,
    ) -> None:
        """No `NarrationPort` here, deliberately.

        `NarrationPort.narrate(intelligence, language) -> Narrative` is
        shaped for one job: restate a finished `WeatherIntelligence` object.
        A chat turn needs conversational history, attraction data, and the
        current question in the same call — a different shape entirely, not
        a variation of narration's. Reusing the port would mean either
        breaking its contract for every other caller (`/narrative` stays
        strict-503, per the approved chat-degrades/narrative-stays-strict
        split) or building a second, parallel LLM implementation next to it,
        which duplicates the exact thing this fix is supposed to prevent.
        The existing `LlmClient` transport is reused instead — same class,
        same retry/timeout policy, no second implementation — just called
        directly with a chat-shaped prompt instead of through the narration
        port's narrower signature.
        """
        self._conversation_repo = conversation_repo
        self._geocoding = geocoding
        self._intelligence_use_case = intelligence_use_case
        self._attraction_provider = attraction_provider
        self._llm_client = llm_client
        self._max_forecast_horizon_days = max_forecast_horizon_days
        self._entity_extractor = EntityExtractor(llm_client)

    async def process_message(
        self,
        *,
        conversation_id: UUID | None,
        user_message: str,
        user_id: str | None = None,
    ) -> ChatResult:
        """Process a user message and return the assistant response."""
        if conversation_id:
            conversation = await self._conversation_repo.get(conversation_id)
            if conversation is None:
                raise ValueError(f"Conversation {conversation_id} not found")
        else:
            conversation = Conversation.start()

        previous_assistant = conversation.last_assistant_message
        previous_reply = previous_assistant.content if previous_assistant is not None else None
        conversation = conversation.add_user_message(user_message)
        was_complete_before_this_turn = conversation.trip_context.is_complete

        # A pending clarification takes priority over ordinary extraction:
        # if the previous turn asked "which Goa?", this message is most
        # likely an answer to that question, not a fresh trip description.
        pending_candidates = self._pending_candidates(conversation)
        selected_from_pending: GeocodedPlace | None = None
        carry_forward_candidates = False
        if pending_candidates and conversation.trip_context.destination is None:
            selected_from_pending = self._resolve_candidate_selection(
                user_message, pending_candidates
            )
            if selected_from_pending is None:
                # Unclear reply — keep the same options alive rather than
                # silently dropping them and re-asking a generic question.
                carry_forward_candidates = True

        history = conversation.messages[-_HISTORY_TURNS_IN_PROMPT:]
        if selected_from_pending is not None and _is_bare_selection(user_message):
            # "1" / "the first one" / "France" answers the clarification and
            # nothing else. Everything the original message established (dates,
            # duration, interests) is already in `trip_context`; re-extracting
            # the reply could only add noise on top of it — an LLM reading "1"
            # as a one-day trip, say — so the selection is merged as-is.
            extracted: dict[str, Any] = {}
        else:
            extracted = await self._entity_extractor.extract(
                user_message, conversation.trip_context, history
            )
        extracted = _answer_to_end_date_question(
            extracted, user_message, conversation.trip_context, history
        )

        destination = conversation.trip_context.destination
        destination_query = conversation.trip_context.destination_query
        destination_candidates: tuple[GeocodedPlace, ...] = ()

        extracted_destination = extracted.get("destination")
        if isinstance(extracted_destination, str):
            already_current = (
                conversation.trip_context.destination is not None
                and extracted_destination.strip().casefold()
                == (conversation.trip_context.destination_query or "").strip().casefold()
            )
            if already_current:
                # Gemini re-stated the destination that's already resolved
                # (models do this even when told not to) — treat as
                # "nothing new", not a fresh mention. Re-geocoding an
                # unchanged name risks a different candidate winning on a
                # second call and silently relocating an established trip.
                extracted_destination = None

        # "Help me pick a destination" / "somewhere warm" — the user wants
        # options, not to be asked "where?". Only while no destination is
        # set and this turn neither named one nor picked from a list.
        wants_suggestions = (
            extracted.get("wants_suggestions") is True
            and selected_from_pending is None
            and not extracted_destination
            and conversation.trip_context.destination is None
        )
        if wants_suggestions:
            carry_forward_candidates = False

        #: A named destination the geocoder could not find (or could not be
        #: reached for) — reported back to the user by name instead of the
        #: generic "where would you like to go?", which read as the message
        #: having been ignored.
        unresolved_destination: str | None = None
        geocoding_unavailable = False

        if selected_from_pending is not None:
            destination = selected_from_pending
            destination_query = selected_from_pending.name
        elif extracted_destination:
            carry_forward_candidates = False
            try:
                candidates = await self._geocode_destination(extracted_destination)
            except Exception:
                candidates = []
                geocoding_unavailable = True
                logger.warning("geocoding_failed", extra={"query": extracted_destination})
            if candidates:
                destination_query = extracted_destination
                if _is_ambiguous_candidates(candidates, destination_query):
                    # Genuinely different places sharing a name (the classic
                    # case: Paris, France vs. Paris, Texas) — resolving
                    # silently risks planning the wrong trip entirely. Leave
                    # `destination` as it was and ask instead of guessing.
                    destination_candidates = tuple(candidates[:_MAX_CLARIFICATION_CANDIDATES])
                else:
                    destination = _best_candidate(candidates, destination_query)
            else:
                # Nothing to resolve to: the established trip (if any) and
                # its query stay as they were rather than half-updating.
                unresolved_destination = extracted_destination
        elif carry_forward_candidates:
            destination_candidates = pending_candidates

        raw_duration = extracted.get("duration")
        duration_days: int | None = None
        if (
            isinstance(raw_duration, int)
            and not isinstance(raw_duration, bool)
            and raw_duration > 0
        ):
            duration_days = raw_duration

        new_context = conversation.trip_context.merge(
            destination=destination,
            destination_query=destination_query,
            start_date=parse_iso_date(extracted.get("start_date")),
            end_date=parse_iso_date(extracted.get("end_date")),
            interests=extracted.get("interests"),
            travel_style=extracted.get("travel_style"),
            pace=extracted.get("pace"),
            duration_days=duration_days,
        )
        if destination_candidates:
            # `merge(destination=None)` means "untouched", which is right
            # for every other `None` case but wrong here: a fresh ambiguous
            # mention must clear any *previously resolved* destination too —
            # the same override `dataclasses.replace` already applies below
            # for an invalid date range.
            new_context = replace(new_context, destination=None)

        # Backend normalization, not something Gemini is asked to compute:
        # a stated duration ("4-day trip") fills in the end date only when
        # a start date is known and no explicit end date was given. The
        # duration is read from the context, not just this turn, so "a quiet
        # weekend" said before any date still applies once "October 10"
        # arrives — rather than the user being asked how long they'll stay.
        duration = new_context.duration_days
        if (
            duration is not None
            and new_context.start_date is not None
            and new_context.end_date is None
        ):
            new_context = replace(
                new_context, end_date=new_context.start_date + timedelta(days=duration - 1)
            )

        # A bare single-day weather question ("will it rain in Mumbai
        # tomorrow?") gives a start date but never states a trip length —
        # unlike `trip_planning` wording, there's no "trip" being described
        # here to ask an end date *for*. Forcing the "when does your trip
        # end?" clarification onto a one-off forecast question is exactly
        # the kind of friction that makes the assistant feel like a form,
        # not a conversation, so a `weather_question` with only a start date
        # is read as asking about that single day, not opening a new trip.
        # `trip_planning` keeps asking, as it should: that wording usually
        # means more details (an end date, a duration) are still coming.
        if (
            extracted.get("intent") == "weather_question"
            and new_context.start_date is not None
            and new_context.end_date is None
        ):
            new_context = replace(new_context, end_date=new_context.start_date)

        # Once both dates are known they define the trip length, so the
        # stored duration can never disagree with them (the extraction prompt
        # omits `duration` when explicit dates are given, which otherwise left
        # "a 5 day trip from October 10 to 14" with no duration in its state).
        if (
            new_context.start_date is not None
            and new_context.end_date is not None
            and new_context.end_date >= new_context.start_date
        ):
            new_context = replace(
                new_context,
                duration_days=(new_context.end_date - new_context.start_date).days + 1,
            )

        # Is this a plannable trip at all (end after start, not entirely in
        # the past)? Length and distance ahead are deliberately *not* checked
        # here: a trip running past the forecast window is still a valid trip
        # — `_generate_travel_response` analyses the days the provider can
        # forecast and reports the rest, instead of refusing the trip.
        date_range_error: InvalidDateRangeError | None = None
        if not new_context.missing_essentials():
            assert new_context.start_date is not None
            assert new_context.end_date is not None
            try:
                validate_trip_dates(
                    new_context.start_date,
                    new_context.end_date,
                    today=datetime.now(UTC).date(),
                )
            except InvalidDateRangeError as exc:
                date_range_error = exc
                # An invalid range must not be persisted as "known" dates —
                # otherwise the *next* turn's `was_complete_before_this_turn`
                # check would see a structurally complete (but factually
                # rejected) trip and jump straight to keyword classification
                # instead of still treating the trip as being established.
                # Clearing them here is what makes "ok, next month instead"
                # on the following turn correctly re-trigger the full
                # weather+attraction fetch once real dates arrive.
                new_context = replace(new_context, start_date=None, end_date=None)

        conversation = conversation.update_context(new_context)
        missing = new_context.missing_essentials()
        context_complete = not missing
        response_places: tuple[Attraction, ...] = ()

        if destination_candidates:
            # Checked first, same reasoning as `date_range_error` below:
            # destination is necessarily among `missing` here, but the
            # generic "where would you like to go?" clarification would
            # waste the geocoding work already done and lose the specific
            # options the user needs to answer with.
            response = self._generate_destination_clarification(
                destination_candidates, destination_query
            )
            intent = ChatIntent.TRIP_PLANNING
            llm_generated = False
        elif wants_suggestions and new_context.destination is None:
            # Offered as numbered, geocoded candidates — the same pending-
            # selection mechanism a "which Paris?" question uses, so "2" or
            # a name on the next turn resolves straight to a real place.
            response, destination_candidates, llm_generated = await self._suggest_destinations(
                conversation, new_context
            )
            intent = ChatIntent.RECOMMENDATION_REQUEST
        elif unresolved_destination is not None:
            response = self._generate_unresolved_destination(
                unresolved_destination, new_context, lookup_failed=geocoding_unavailable
            )
            intent = ChatIntent.TRIP_PLANNING
            llm_generated = False
        elif date_range_error is not None:
            # Checked ahead of `not context_complete` deliberately: all three
            # essentials WERE present (that's the only way this gets set) —
            # dates were cleared afterward specifically so the *next* turn
            # doesn't misread this trip as already complete. That clearing
            # must not demote this response to the generic "where/when"
            # clarification; the specific rejection reason is more useful and
            # still accurate. No weather/attraction I/O happens either way.
            response = self._generate_date_range_rejection(date_range_error, new_context)
            intent = ChatIntent.TRIP_PLANNING
            llm_generated = False
        elif not context_complete:
            response = self._generate_clarification(missing, new_context)
            if previous_reply is not None and response == previous_reply:
                # The same question twice in a row reads as the answer having
                # been ignored — say what is still missing and show a format.
                response = self._rephrase_clarification(missing, new_context)
            intent = ChatIntent.TRIP_PLANNING
            llm_generated = False
        elif not was_complete_before_this_turn:
            # This turn is the one that completed the trip — always the full
            # answer, regardless of wording. Classifying by keyword here
            # would let "I'm planning a 4-day trip to Goa from August 20 to
            # August 23" land on GENERAL_CHAT (it contains none of the
            # recommendation/weather/itinerary keywords) and skip the very
            # weather+attraction fetch this message exists to trigger.
            # Keyword classification is for turns *after* a trip is already
            # established, not the turn that establishes it.
            intent = ChatIntent.TRIP_PLANNING
            search_area = await self._resolve_search_area(extracted, new_context.destination)
            response, llm_generated, response_places = await self._generate_travel_response(
                conversation,
                new_context,
                intent,
                user_message=user_message,
                search_area=search_area,
            )
        else:
            intent = self._resolve_intent(extracted.get("intent"), user_message, new_context)
            search_area = await self._resolve_search_area(extracted, new_context.destination)
            response, llm_generated, response_places = await self._generate_travel_response(
                conversation,
                new_context,
                intent,
                user_message=user_message,
                search_area=search_area,
            )

        conversation = conversation.add_assistant_message(
            response,
            metadata={
                "intent": intent.value,
                "llm_generated": llm_generated,
                # What this turn was still waiting for, in ask-order — lets
                # the next turn read a bare "ooty" as the answer to "where?"
                # even when it has to fall back to the heuristic extractor.
                "missing_essentials": list(missing),
                **(
                    {"places": [_attraction_to_dict(p) for p in response_places]}
                    if response_places
                    else {}
                ),
                **(
                    {
                        "destination_candidates": [
                            _geocoded_place_to_dict(c) for c in destination_candidates
                        ]
                    }
                    if destination_candidates
                    else {}
                ),
            },
        )
        conversation = await self._conversation_repo.save(conversation)

        return ChatResult(
            conversation=conversation,
            response=response,
            context_complete=context_complete,
            missing_essentials=missing,
            intent=intent,
            llm_generated=llm_generated,
            places=response_places,
            destination_candidates=destination_candidates,
        )

    def _generate_clarification(self, missing: tuple[str, ...], context: TripContext) -> str:
        """Generate a natural clarifying question."""
        if MISSING_DESTINATION in missing:
            return (
                "Where would you like to go? Tell me a city or region — or if you're not "
                "sure yet, tell me what you're in the mood for and I'll suggest a few places."
            )
        if MISSING_START_DATE in missing:
            dest = _destination_label(context)
            if context.duration_days:
                return (
                    f"Great — {dest} for {_days_phrase(context.duration_days)}. "
                    "What date does the trip start?"
                )
            return f"Great! When does your trip to {dest} start? Please give me the start date."
        if MISSING_END_DATE in missing:
            assert context.start_date is not None
            return (
                f"Got it — starting {_day_label(context.start_date)}. When does your trip end? "
                "(Or how many days will you stay?)"
            )
        return "I need a bit more information to help you plan. Could you tell me more?"

    def _rephrase_clarification(self, missing: tuple[str, ...], context: TripContext) -> str:
        """Second wording for a clarification the previous turn already asked
        verbatim — names what is still missing and shows an answer format."""
        if MISSING_DESTINATION in missing:
            return (
                "I still need a destination before I can check the weather. Give me a city or "
                'region (for example "Ooty" or "Port Blair"), or say "suggest some places".'
            )
        if MISSING_START_DATE in missing:
            return (
                f"I still don't have a start date for {_destination_label(context)}. "
                'You can reply with something like "October 10" or "October 10 to 14".'
            )
        return (
            "How long will you stay? Reply with an end date (for example "
            '"October 14") or a number of days (for example "5 days").'
        )

    def _generate_unresolved_destination(
        self, query: str, context: TripContext, *, lookup_failed: bool
    ) -> str:
        """A named place that could not be resolved — quoted back by name, and
        any already-established trip explicitly kept rather than replaced."""
        if lookup_failed:
            opening = f'I couldn\'t look up "{query}" just now — the place search is unavailable.'
            ask = "Could you try again in a moment?"
        else:
            opening = f'I couldn\'t find a place called "{query}".'
            ask = "Could you check the spelling, or add the state or country?"
        if context.destination is not None:
            return f"{opening} I've kept your trip to {context.destination.display_name}. {ask}"
        return f"{opening} {ask}"

    def _generate_date_range_rejection(
        self, error: InvalidDateRangeError, context: TripContext
    ) -> str:
        """Conversational phrasing of `validate_trip_dates` — the same reason
        codes REST uses, in a different-shaped sentence. Trip length and the
        forecast window are not rejections in chat (see
        `_generate_travel_response`)."""
        dest = (
            context.destination.display_name
            if context.destination
            else context.destination_query or "your destination"
        )
        if error.reason == "end_before_start":
            return (
                f"Your end date needs to be on or after your start date for the trip to "
                f"{dest}. Could you give me the correct dates?"
            )
        if error.reason == "historical_range":
            return (
                f"I can only forecast upcoming weather, not dates that have already passed. "
                f"Could you give me upcoming dates for your trip to {dest}?"
            )
        return "Those dates don't work for a forecast — could you give me a different range?"

    def _pending_candidates(self, conversation: Conversation) -> tuple[GeocodedPlace, ...]:
        """Destination candidates a previous turn asked about, if any.

        Read from the last assistant message's `metadata` — the same place
        `intent`/`llm_generated` already live — never from `TripContext`,
        which only ever holds a *resolved* destination. A malformed or
        missing entry is treated as "no pending clarification", never a
        crash: this is best-effort continuity, not a contract other code
        depends on.
        """
        last = conversation.last_assistant_message
        if last is None:
            return ()
        raw = last.metadata.get("destination_candidates")
        if not isinstance(raw, list):
            return ()
        candidates: list[GeocodedPlace] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                candidates.append(
                    GeocodedPlace(
                        name=item["name"],
                        latitude=item["latitude"],
                        longitude=item["longitude"],
                        country=item.get("country"),
                        country_code=item.get("country_code"),
                        admin1=item.get("admin1"),
                        timezone=item.get("timezone"),
                    )
                )
            except (KeyError, TypeError):
                continue
        return tuple(candidates)

    def _resolve_candidate_selection(
        self, message: str, candidates: tuple[GeocodedPlace, ...]
    ) -> GeocodedPlace | None:
        """Interpret a reply to a destination-clarification turn as a selection.

        Conservative on purpose: an unrecognized reply returns `None` and
        falls through to normal extraction (or a repeat of the same
        question) rather than guessing wrong — silently resolving to the
        wrong candidate is the one failure mode this feature exists to
        remove.
        """
        lowered = message.strip().lower()

        # A whole reply with one number ("2", "option 2"), or a reply that
        # *opens* with the option number and adds details ("1, from October
        # 10 to 14"). The second form skips an opening date or length ("1
        # October to 5 October", "1 day in ...") — those are not a choice.
        digit_match = re.fullmatch(r"\D*(\d+)\D*", lowered) or re.match(
            rf"\s*(\d)\b(?!\s*(?:st|nd|rd|th|days?|nights?|weeks?)\b)(?!\s*{_MONTH}\b)", lowered
        )
        if digit_match:
            index = int(digit_match.group(1)) - 1
            if 0 <= index < len(candidates):
                return candidates[index]

        for word, index in _ORDINAL_WORDS.items():
            if word in lowered and index < len(candidates):
                return candidates[index]

        text_matches = [
            candidate
            for candidate in candidates
            if (candidate.country and candidate.country.lower() in lowered)
            or (candidate.admin1 and candidate.admin1.lower() in lowered)
        ]
        if len(text_matches) == 1:
            return text_matches[0]

        # Suggested destinations carry distinct names, so "Puducherry" picks
        # that option. Same-named candidates ("which Paris?") all match and
        # fall through, exactly as before.
        folded = _fold(message)
        name_matches = [c for c in candidates if _fold(c.name) in folded]
        if len(name_matches) == 1:
            return name_matches[0]

        return None

    def _generate_destination_clarification(
        self, candidates: tuple[GeocodedPlace, ...], query: str | None
    ) -> str:
        """Deterministic prose for an ambiguous destination.

        The structured options travel separately, on
        `ChatResult.destination_candidates` — this text is a human-readable
        companion, never the data source a frontend should parse.
        """
        name = query or "that place"
        lines = [f'I found more than one location named "{name}". Which one do you mean?']
        for index, candidate in enumerate(candidates, start=1):
            lines.append(f"{index}. {_clarification_label(candidate)}")
        lines.append("Reply with the number, or tell me which one you meant.")
        return "\n".join(lines)

    async def _suggest_destinations(
        self, conversation: Conversation, context: TripContext
    ) -> tuple[str, tuple[GeocodedPlace, ...], bool]:
        """Recommend destinations to a user who hasn't chosen one.

        Choosing *candidate* destinations from preferences ("warm, quiet,
        next month") is world knowledge only the LLM has, so it proposes
        them — but each is then resolved through the same geocoder as a
        typed destination and dropped if it doesn't resolve to a real place
        in the stated country. What reaches the user is a numbered list of
        verified places, carried as `destination_candidates` so the next
        turn's "2" or "Puducherry" selects one through the existing pending-
        selection path. No weather is claimed here: the real forecast is
        fetched once a destination and dates are set.

        Returns `(response, candidates, llm_generated)`.
        """
        prompt = self._build_suggestion_prompt(
            conversation, context, today=datetime.now(UTC).date()
        )
        try:
            raw_text = await self._llm_client.complete(
                system_prompt=prompt,
                user_content=_SUGGESTION_USER_CONTENT,
                json_mode=True,
                temperature=_SUGGESTION_TEMPERATURE,
            )
            parsed = json.loads(raw_text.strip())
        except (LlmClientError, LlmTimeoutError, json.JSONDecodeError) as exc:
            logger.warning("destination_suggestions_failed", extra={"error": str(exc)})
            return _SUGGESTIONS_UNAVAILABLE, (), False

        raw_suggestions = parsed.get("suggestions") if isinstance(parsed, dict) else None
        if not isinstance(raw_suggestions, list):
            return _SUGGESTIONS_UNAVAILABLE, (), False
        suggestions = [
            item
            for item in raw_suggestions
            if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip()
        ][:_MAX_DESTINATION_SUGGESTIONS]

        resolved = await asyncio.gather(*(self._resolve_suggestion(item) for item in suggestions))
        verified: list[tuple[GeocodedPlace, str]] = []
        seen: set[str] = set()
        for item, place in zip(suggestions, resolved, strict=True):
            if place is None or place.location_id in seen:
                continue
            seen.add(place.location_id)
            reason = item.get("reason")
            verified.append((place, reason.strip() if isinstance(reason, str) else ""))

        if not verified:
            return _SUGGESTIONS_UNAVAILABLE, (), False

        intro = parsed.get("intro")
        lines = [
            intro.strip()
            if isinstance(intro, str) and intro.strip()
            else "Here are a few places that could suit you:",
            "",
        ]
        for index, (place, reason) in enumerate(verified, start=1):
            label = f"**{place.display_name}**"
            lines.append(f"{index}. {label} — {reason}" if reason else f"{index}. {label}")
        lines.extend(
            ["", "Reply with a number or a name, and tell me your dates if you know them."]
        )
        return "\n".join(lines), tuple(place for place, _ in verified), True

    async def _geocode_destination(self, query: str) -> list[GeocodedPlace]:
        """Geocode a destination as extracted, retrying a comma form as plain words.

        The extraction prompt asks for "Place, Region" when the user named a
        region, which the geocoder handles natively when it is a real
        place/region pair ("Ooty, Tamil Nadu"). But when the comma splits one
        name ("Andaman, Nicobar Islands"), the geocoder finds nothing and —
        by design — skips its word-dropping retry for comma queries.
        Retrying once without the comma recovers that case.
        """
        candidates = await self._geocoding.search(query)
        if candidates or "," not in query:
            return candidates
        return await self._geocoding.search(" ".join(query.replace(",", " ").split()))

    async def _resolve_suggestion(self, item: dict[str, Any]) -> GeocodedPlace | None:
        """Geocode one suggested destination; `None` unless it resolves to a
        place in the country the suggestion named.

        A place with exactly the suggested name wins. Failing that, the
        suggestion's region is tried for an exact match before settling for
        the best partial one — live-observed: "Pondicherry" has no town
        entry in the gazetteer, only "Pondicherry Airport", while its region
        "Puducherry" is the town itself.
        """
        name = str(item["name"]).strip()
        region = item.get("region") if isinstance(item.get("region"), str) else None
        country = item.get("country") if isinstance(item.get("country"), str) else None

        async def search_in_country(query: str) -> list[GeocodedPlace]:
            try:
                found = await self._geocoding.search(query)
            except Exception:
                logger.warning("suggestion_geocoding_failed", extra={"query": query})
                return []
            if country:
                found = [c for c in found if c.country and _fold(c.country) == _fold(country)]
            return found

        candidates = await search_in_country(name)
        exact = [c for c in candidates if _fold(c.name) == _fold(name)]
        if exact:
            return exact[0]
        if region and _fold(region) != _fold(name):
            exact_region = [
                c for c in await search_in_country(region) if _fold(c.name) == _fold(region)
            ]
            if exact_region:
                return exact_region[0]
        if not candidates:
            return None
        return _best_candidate(candidates, " ".join(part for part in (name, region) if part))

    def _build_suggestion_prompt(
        self, conversation: Conversation, context: TripContext, *, today: date
    ) -> str:
        history = _format_history(conversation.messages[-_HISTORY_TURNS_IN_PROMPT:])
        start = context.start_date.isoformat() if context.start_date else "not given"
        end = context.end_date.isoformat() if context.end_date else "not given"
        length = f"{context.duration_days} days" if context.duration_days else "not given"
        # Spelled out because the model, given only today's date, still
        # described "next month" as the current one in live testing.
        next_month = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        known = [
            f"- dates: {start} to {end}",
            f"- trip length: {length}",
            f"- interests and preferences: {', '.join(context.interests) or 'none stated'}",
            f"- travelling as: {context.travel_style or 'not stated'}",
        ]
        return (
            "You help a traveller who has not chosen a destination yet. Read the "
            "conversation and suggest destinations that fit what they asked for.\n\n"
            f"Today's date is {today.isoformat()} — this month is {today:%B %Y}, next "
            f"month is {next_month:%B %Y}.\n\n"
            f"CONVERSATION:\n{history}\n\n"
            "WHAT IS KNOWN SO FAR:\n" + "\n".join(known) + "\n\n"
            f"Suggest {_MAX_DESTINATION_SUGGESTIONS - 1} or {_MAX_DESTINATION_SUGGESTIONS} "
            "real, well-known travel destinations matching their stated preferences "
            "(climate, atmosphere, activities, timing).\n\n"
            "Rules:\n"
            "- Each must be a real city, town, island or hill station a map search can "
            "find. Give the name as it appears on maps.\n"
            "- If the conversation shows where the user is or which country or region "
            "they want, stay within it.\n"
            "- Work out relative timing (\"next month\", \"this weekend\") from today's date "
            "above, and describe the climate for THAT month, naming it correctly.\n"
            "- Talk only about typical seasonal climate (e.g. \"usually warm and dry in "
            "November\"), never a specific forecast — real forecasts are checked later.\n"
            "- One short sentence of reason per destination.\n\n"
            "Return ONLY a JSON object, no prose, no markdown fences:\n"
            '{"intro": "one short, friendly sentence", "suggestions": [{"name": "place '
            'name", "region": "state or province, or null", "country": "country\'s '
            'common English name", "reason": "why it fits"}]}'
        )

    def _resolve_intent(
        self, raw_intent: object, message: str, context: TripContext
    ) -> ChatIntent:
        """Gemini's own contextual classification, with a deterministic
        guard: an unrecognized or missing value falls back to the keyword
        classifier rather than propagating a string this codebase has no
        handling for ("backend may still use deterministic guards for
        clearly invalid/unsafe states")."""
        if isinstance(raw_intent, str):
            try:
                return ChatIntent(raw_intent)
            except ValueError:
                logger.warning("unrecognized_intent_from_extraction", extra={"intent": raw_intent})
        return classify_intent(message, context)

    async def _resolve_search_area(
        self, extracted: dict[str, Any], trip_destination: GeocodedPlace | None
    ) -> GeocodedPlace | None:
        """Geocode the sub-location this turn named ("near Panjim"), if any.

        `search_area` comes from the same structured extraction as
        everything else this turn (Gemini's `searchArea` field, or the
        regex fallback's own `search_area` key when Gemini's call failed) —
        no second pass over the raw message. Returns a standalone
        `TripContext`-shaped place, never persisted and never merged into
        the conversation's real `TripContext` — the trip destination stays
        whatever it already was. `None` means no sub-location was named,
        which is the common case.

        Live-observed: the extraction prompt asks for a place "within or
        near" the trip destination, but nothing enforces that Gemini's
        output actually is one — one call extracted `searchArea: "that
        restaurant"` from "can you book me a table at that restaurant for
        dinner", and the geocoder's fuzzy match for that phrase returned a
        real but utterly unrelated place across the world. The backend
        guard the architecture keeps room for: any candidate implausibly far
        from the established trip destination is discarded rather than
        silently recentering the whole attraction search on the wrong
        country.
        """
        query = extracted.get("search_area")
        if not isinstance(query, str) or not query.strip():
            return None
        try:
            candidates = await self._geocoding.search(query)
        except Exception:
            logger.warning("search_area_geocoding_failed", extra={"query": query})
            return None
        if not candidates:
            return None
        candidate = candidates[0]
        if trip_destination is not None:
            distance_km = _haversine_km(
                trip_destination.latitude,
                trip_destination.longitude,
                candidate.latitude,
                candidate.longitude,
            )
            if distance_km > _MAX_SEARCH_AREA_DISTANCE_KM:
                logger.warning(
                    "search_area_implausibly_far",
                    extra={"query": query, "distance_km": round(distance_km)},
                )
                return None
        return candidate

    async def _generate_travel_response(
        self,
        conversation: Conversation,
        context: TripContext,
        intent: ChatIntent,
        *,
        user_message: str,
        search_area: GeocodedPlace | None = None,
    ) -> tuple[str, bool, tuple[Attraction, ...]]:
        """Fetch what `intent` needs, then generate the response.

        Intelligence is always fetched — cheap and cached by the time this
        runs (see below) — but `general_chat` and `packing_request` skip the
        attraction-provider call: no place lookup applies to either, and it's
        the one real per-turn cost in this method. Every other intent,
        `weather_question` included, fetches places too:
        a "what if it rains?" question is implicitly asking what to do about
        it, and the weather-aware ranking already surfaces indoor/good-
        weather options first on a poor-weather day — the places call is
        what makes that answer concrete instead of generic.

        `search_area`, if given, recenters only the attraction search — a
        local `.merge()`'d copy of `context`, never the context itself, so
        the persisted trip destination is untouched by a follow-up asking
        about a specific neighbourhood within the trip.
        """
        assert context.destination is not None
        assert context.start_date is not None
        assert context.end_date is not None

        # The trip can be longer, or further ahead, than the provider's
        # forecast window. Weather (and the weather-ranked places) is fetched
        # only for the days inside it; the trip itself is never shortened.
        coverage = forecast_coverage(
            context.start_date,
            context.end_date,
            today=datetime.now(UTC).date(),
            window_days=self._max_forecast_horizon_days,
        )
        if not coverage.has_forecast:
            return self._beyond_forecast_window_response(context, coverage), False, ()
        forecast_context = replace(
            context, start_date=coverage.covered_start, end_date=coverage.covered_end
        )
        assert forecast_context.start_date is not None
        assert forecast_context.end_date is not None

        # Fetched unconditionally, `general_chat` included: this method only
        # ever runs once the trip is already fully established (the asserts
        # above), so the fetch is already cheap and cached
        # (`CachedIntelligenceUseCase`) by the time any turn reaches here —
        # not the same cost tradeoff as the attraction-provider call below,
        # which does real per-turn work. Skipping it used to mean an
        # intent-classification miss (a real question the model mislabeled
        # general_chat — live-observed with "is it safe to travel each
        # day?") left the model with zero data and nothing to answer with,
        # producing the bare fallback stub instead of a real answer.
        intelligence_result = await self._intelligence_use_case.execute(
            latitude=context.destination.latitude,
            longitude=context.destination.longitude,
            start=forecast_context.start_date,
            end=forecast_context.end_date,
            name=context.destination.display_name,
        )
        intelligence = intelligence_result.intelligence
        attractions: AttractionRecommendation | None = None

        if intent not in (ChatIntent.GENERAL_CHAT, ChatIntent.PACKING_REQUEST):
            assert intelligence is not None
            preferred_types = self._interests_to_attraction_types(
                context.interests,
                message=user_message,
                intent=intent,
                broad_check_text=_recent_user_text(conversation),
            )
            attraction_context = (
                forecast_context.merge(destination=search_area)
                if search_area is not None
                else forecast_context
            )
            attractions = await self._attraction_provider.get_recommendations(
                trip_context=attraction_context,
                weather_intelligence=intelligence,
                preferred_types=preferred_types,
            )

        # An itinerary or recommendation ask is inherently a "give me the
        # spread" question — it shouldn't need the user to also say
        # "checklist" or "every day" to get more than a token place or two.
        wants_detail = _wants_detailed_response(user_message) or intent in (
            ChatIntent.ITINERARY_REQUEST,
            ChatIntent.RECOMMENDATION_REQUEST,
        )
        places_per_day = (
            _MAX_PLACES_PER_DAY_IN_RESPONSE if wants_detail else _PLACES_PER_DAY_IN_RESPONSE
        )

        response_text, llm_generated = await self._generate_conversational_narrative(
            conversation=conversation,
            context=context,
            intent=intent,
            intelligence=intelligence,
            attractions=attractions,
            search_area=search_area,
            wants_detail=wants_detail,
            places_per_day=places_per_day,
            coverage=coverage,
        )
        if not coverage.is_complete:
            # Appended deterministically rather than left to the model, so
            # the uncovered dates are always stated, and stated exactly.
            response_text = f"{response_text}\n\n{self._partial_forecast_note(coverage)}"
        places = self._select_response_places(attractions, places_per_day=places_per_day)
        return response_text, llm_generated, places

    def _partial_forecast_note(self, coverage: ForecastCoverage) -> str:
        """Which trip days the analysis covers, and which it cannot yet."""
        assert coverage.covered_start is not None
        assert coverage.covered_end is not None
        first_uncovered = coverage.first_uncovered_day
        assert first_uncovered is not None
        uncovered_days = (coverage.trip_end - first_uncovered).days + 1
        covered = _date_span(coverage.covered_start, coverage.covered_end)
        uncovered = _date_span(first_uncovered, coverage.trip_end)
        fully_covered_from = _day_label(
            coverage.fully_covered_from(self._max_forecast_horizon_days)
        )
        single = uncovered_days == 1
        return (
            f"_Forecast note: weather data currently reaches {_day_label(coverage.window_end)}, "
            f"so the weather above covers {covered}. {uncovered} "
            f"({_days_phrase(uncovered_days)}) {'is' if single else 'are'} beyond the "
            "available forecast range. The forecast moves forward a day at a time — ask "
            f"again from {fully_covered_from} and your whole trip will be covered._"
        )

    def _beyond_forecast_window_response(
        self, context: TripContext, coverage: ForecastCoverage
    ) -> str:
        """The whole trip is past the forecast window: keep the trip, say
        plainly why there is no weather analysis yet, and when there will be."""
        days = (coverage.trip_end - coverage.trip_start).days + 1
        available_from = _day_label(
            coverage.forecast_available_from(self._max_forecast_horizon_days)
        )
        return (
            f"Your {days}-day trip to {_destination_label(context)} "
            f"({_date_span(coverage.trip_start, coverage.trip_end)}) is saved. "
            f"Weather forecasts currently reach {_day_label(coverage.window_end)}, so I can't "
            "analyse these dates yet — forecasts for your first day start appearing from "
            f"{available_from}. Ask me again then and I'll check every day that's in range."
        )

    def _select_response_places(
        self, attractions: AttractionRecommendation | None, *, places_per_day: int
    ) -> tuple[Attraction, ...]:
        """The exact places this turn's reply is allowed to mention.

        Same per-day cap the prompt itself uses, flattened across days and
        deduplicated by id, in day order — so the structured `places` field
        returned to the caller can never contain a place absent from what
        the LLM was actually shown, or vice versa.
        """
        if attractions is None:
            return ()
        seen: set[str] = set()
        selected: list[Attraction] = []
        for day in attractions.daily:
            for attraction in day.attractions[:places_per_day]:
                if attraction.id not in seen:
                    seen.add(attraction.id)
                    selected.append(attraction)
        return tuple(selected)

    def _interests_to_attraction_types(
        self,
        interests: tuple[str, ...],
        *,
        message: str = "",
        intent: ChatIntent | None = None,
        broad_check_text: str = "",
    ) -> tuple[AttractionType, ...]:
        """Map user interests to attraction types.

        Also scans the raw turn `message` directly, not just the LLM's own
        `interests` extraction — live testing found the extraction step
        doesn't reliably echo back every interest word verbatim (observed:
        identical "find me a nice hotel" turns returned `interests: ['hotel']`
        on roughly 1 in 3 calls, ordinary LLM sampling variance, not a bug in
        the extraction prompt). A category the user explicitly named in this
        turn must not be missed just because that one call's extraction
        didn't surface it.

        Live-observed regression, a second instance of the same class of bug
        `_BROAD_ITINERARY_SPREAD` already exists for: `TripContext.interests`
        only ever accumulates, so a trip where the user mentioned "food" on
        turn one stayed food-only forever after — asking for "a detailed
        itinerary" later still only ever fetched restaurants, no viewpoints,
        monuments, or anything else, because `intent` never widened the
        category set on its own, only specific trigger phrases
        (`_wants_broad_itinerary`) did, and "itinerary" itself was never one
        of them. `itinerary_request` is inherently a "give me the whole day"
        ask, not a request for one category, so it now widens unconditionally
        — on top of, never instead of, whatever specific interests were
        actually named, so "an itinerary heavy on sports" still gets sports
        specifically as well as the broad spread.

        `broad_check_text`, when given, is checked instead of `message` for
        the broad-itinerary-phrase trigger specifically (`_recent_user_text`)
        — the exact-keyword interest scan below always stays scoped to this
        turn's own `message`, never blurred across turns.
        """
        mapping = {
            "beach": AttractionType.BEACH,
            "food": AttractionType.FOOD,
            "cafe": AttractionType.FOOD,
            "restaurant": AttractionType.RESTAURANT,
            "museum": AttractionType.MUSEUM,
            "nightlife": AttractionType.NIGHTLIFE,
            "hiking": AttractionType.HIKING,
            "nature": AttractionType.NATURE,
            "shopping": AttractionType.SHOPPING,
            "market": AttractionType.SHOPPING,
            "photography": AttractionType.PHOTOGRAPHY,
            "adventure": AttractionType.ADVENTURE,
            "wellness": AttractionType.WELLNESS,
            "family": AttractionType.FAMILY,
            "culture": AttractionType.CULTURAL_SITE,
            "monument": AttractionType.CULTURAL_SITE,
            "temple": AttractionType.CULTURAL_SITE,
            "gallery": AttractionType.CULTURAL_SITE,
            "landmark": AttractionType.LANDMARK,
            "attraction": AttractionType.LANDMARK,
            "sightseeing": AttractionType.LANDMARK,
            "viewpoint": AttractionType.VIEWPOINT,
            "view point": AttractionType.VIEWPOINT,
            "wildlife": AttractionType.WILDLIFE,
            "zoo": AttractionType.WILDLIFE,
            "water sports": AttractionType.WATER_SPORTS,
            "outdoor": AttractionType.OUTDOOR_ACTIVITY,
            "indoor": AttractionType.INDOOR_ACTIVITY,
            "hotel": AttractionType.HOTEL,
            "stay": AttractionType.HOTEL,
            "accommodation": AttractionType.HOTEL,
            "lodging": AttractionType.HOTEL,
            "guest house": AttractionType.GUEST_HOUSE,
            "hostel": AttractionType.GUEST_HOUSE,
            "sports": AttractionType.SPORTS_FACILITY,
            "tennis": AttractionType.SPORTS_FACILITY,
            "golf": AttractionType.SPORTS_FACILITY,
            "football": AttractionType.SPORTS_FACILITY,
            "cricket": AttractionType.SPORTS_FACILITY,
        }
        types = []
        haystacks = [interest.lower() for interest in interests]
        if message:
            haystacks.append(message.lower())
        for haystack in haystacks:
            for key, atype in mapping.items():
                if key in haystack:
                    types.append(atype)

        broad_text = broad_check_text or message
        wants_broad = bool(broad_text) and _wants_broad_itinerary(broad_text)
        if intent is ChatIntent.ITINERARY_REQUEST or wants_broad:
            types.extend(_BROAD_ITINERARY_SPREAD)

        seen: set[AttractionType] = set()
        deduped = []
        for atype in types:
            if atype not in seen:
                seen.add(atype)
                deduped.append(atype)
        return tuple(deduped)

    async def _generate_conversational_narrative(
        self,
        *,
        conversation: Conversation,
        context: TripContext,
        intent: ChatIntent,
        intelligence: WeatherIntelligence | None,
        attractions: AttractionRecommendation | None,
        search_area: GeocodedPlace | None = None,
        wants_detail: bool = False,
        places_per_day: int = _PLACES_PER_DAY_IN_RESPONSE,
        coverage: ForecastCoverage | None = None,
    ) -> tuple[str, bool]:
        """Generate the response via the LLM; fall back to a structured
        summary only if the call itself fails (approved decision: chat
        degrades gracefully, unlike `/narrative`, which stays strict-503).

        Returns `(response_text, llm_generated)` so the caller — and tests —
        can tell the two paths apart without parsing prose.
        """
        prompt = self._build_conversational_prompt(
            conversation=conversation,
            context=context,
            intent=intent,
            intelligence=intelligence,
            wants_detail=wants_detail,
            places_per_day=places_per_day,
            attractions=attractions,
            search_area=search_area,
            coverage=coverage,
        )
        try:
            raw_text = await self._llm_client.complete(
                system_prompt=prompt, user_content="Respond now."
            )
        except (LlmClientError, LlmTimeoutError) as exc:
            logger.warning("chat_response_generation_failed", extra={"error": str(exc)})
            return self._fallback_response(context, intelligence, attractions), False

        text = raw_text.strip()
        if not text:
            logger.warning("chat_response_empty")
            return self._fallback_response(context, intelligence, attractions), False
        return text, True

    def _build_conversational_prompt(
        self,
        *,
        conversation: Conversation,
        context: TripContext,
        intent: ChatIntent,
        intelligence: WeatherIntelligence | None,
        attractions: AttractionRecommendation | None,
        search_area: GeocodedPlace | None = None,
        wants_detail: bool = False,
        places_per_day: int = _PLACES_PER_DAY_IN_RESPONSE,
        coverage: ForecastCoverage | None = None,
    ) -> str:
        """Build the full prompt for the conversational response.

        Every fact in this prompt — weather numbers, place names, dates —
        came from a deterministic engine or a real provider before this
        function ever ran. The model is asked to narrate and never given
        room to introduce a fact that isn't already here (API Spec-style
        grounding, mirroring `narration.j2`'s discipline).
        """
        history_text = _format_history(conversation.messages[-_HISTORY_TURNS_IN_PROMPT:])

        dest = (
            context.destination.display_name
            if context.destination
            else context.destination_query or "your destination"
        )
        days = "?"
        if context.start_date and context.end_date:
            days = str((context.end_date - context.start_date).days + 1)
        interests_str = ", ".join(context.interests) if context.interests else "not specified"
        travel_style = context.travel_style or "not specified"
        pace = context.pace or "not specified"

        sections = [
            "You are a friendly, knowledgeable travel planner. "
            "Write a natural, conversational response.",
            "",
            f"CONVERSATION HISTORY:\n{history_text}",
            "",
            "TRIP CONTEXT:\n"
            f"- Destination: {dest}\n"
            f"- Dates: {context.start_date} to {context.end_date} ({days} days)\n"
            f"- Interests: {interests_str}\n"
            f"- Travel style: {travel_style}\n"
            f"- Preferred pace: {pace}",
        ]
        if coverage is not None and not coverage.is_complete:
            sections += [
                "",
                "FORECAST COVERAGE:\n"
                f"- The weather data below covers only {coverage.covered_start} to "
                f"{coverage.covered_end}; {coverage.first_uncovered_day} to "
                f"{coverage.trip_end} are beyond the weather provider's forecast range.\n"
                "- Never describe or guess weather for those later days. You may still "
                "suggest how to use them; a note listing the exact dates is added after "
                "your reply, so don't repeat it.",
            ]

        if intelligence is not None:
            trip_summary = intelligence.trip_summary
            best_days = ", ".join(d.isoformat() for d in trip_summary.best_days[:2])
            worst_days = ", ".join(d.isoformat() for d in trip_summary.worst_days[:2])
            packing_count = 10 if wants_detail else 5
            packing = ", ".join(trip_summary.overall_packing_list[:packing_count])
            timezone_name = context.destination.timezone if context.destination else None

            # Real per-day detail the deterministic engine already computes —
            # previously never reached this prompt at all, only the
            # trip-level rollup did. Without it, a live response reused the
            # single trip-level suitability score for every day in a table,
            # mislabeling it as if it varied day to day. UV/feels-like/
            # sunrise/sunset are the newly-fetched Open-Meteo fields; a
            # provider that lacks one just omits that clause, never guesses.
            daily_lines = []
            for day in intelligence.daily_intelligence:
                summary = day.summary
                parts = [
                    f"{day.date.strftime('%a %b %d')}: {summary.condition.value}, "
                    f"{summary.temp_min_c:.0f}-{summary.temp_max_c:.0f}°C"
                ]
                if summary.feels_like_max_c is not None:
                    parts.append(f"(feels up to {summary.feels_like_max_c:.0f}°C)")
                parts.append(f"{summary.precipitation_probability:.0%} rain")
                wind = f"wind {summary.wind_speed_kph:.0f} km/h"
                if summary.wind_gust_kph is not None:
                    wind += f" (gusts {summary.wind_gust_kph:.0f})"
                parts.append(wind)
                if summary.humidity is not None:
                    parts.append(f"humidity {summary.humidity:.0%}")
                if summary.uv_index_max is not None:
                    parts.append(
                        f"UV {summary.uv_index_max:.0f} ({_uv_category(summary.uv_index_max)})"
                    )
                sunrise = _local_clock_time(summary.sunrise, timezone_name)
                sunset = _local_clock_time(summary.sunset, timezone_name)
                if sunrise and sunset:
                    parts.append(f"sun {sunrise}–{sunset}")
                top_activity = max(
                    day.activity_suitability, key=lambda entry: entry.score, default=None
                )
                verdict = f"{day.risk_assessment.overall_risk_level} risk, {day.travel_advisory}"
                if top_activity is not None:
                    verdict += f", best for {top_activity.activity} ({top_activity.score}/100)"
                daily_lines.append(f"  {', '.join(parts)} — {verdict}")

            sections.append(
                "WEATHER INTELLIGENCE (already computed, real data — restate only, never "
                "alter or invent a number not shown here):\n"
                f"- Trip suitability score: {trip_summary.trip_suitability_score}/100\n"
                f"- Travel confidence: {trip_summary.travel_confidence:.0%}\n"
                f"- Best days: {best_days}\n"
                f"- Watch-out days: {worst_days}\n"
                f"- Overall risk: {trip_summary.overall_risk_level}\n"
                f"- Packing: {packing}\n"
                "- Day by day:\n" + "\n".join(daily_lines)
            )

        if attractions is not None:
            attr_lines = []
            for day_attr in attractions.daily:
                day_str = day_attr.date.strftime("%a %b %d")
                day_places = day_attr.attractions[:places_per_day]
                named = ", ".join(f"{a.name} ({a.type.value})" for a in day_places)
                attr_lines.append(f"  {day_str}: {named}")
            area_note = (
                f" — the user asked specifically about {search_area.display_name}, "
                f"so these are centered there rather than on {dest} generally"
                if search_area is not None
                else ""
            )
            sections.append(
                f"DAILY ATTRACTIONS (real places from the places provider, with their "
                f"type in parentheses{area_note} — never invent a name not listed here, "
                "and only slot a place into breakfast/lunch/dinner if its type is "
                "restaurant, cafe, or food — a museum, landmark, or other non-food type "
                "is a thing to do, never a meal stop):\n" + "\n".join(attr_lines)
            )

        shape = _INTENT_RESPONSE_SHAPES.get(
            intent, _INTENT_RESPONSE_SHAPES[ChatIntent.GENERAL_CHAT]
        )
        always_rules = (
            "- Talk like a well-traveled friend giving advice over text, not a formal "
            "report — warm and casual, the way you'd naturally reply to someone. "
            "Contractions are fine, a little personality is fine, an exclamation point here "
            "and there is fine. Skip pure filler that adds nothing ('Great question!', "
            "'I'd be happy to help!'), but a natural, friendly opening is welcome — you "
            "don't need to open with the bare fact like a database dump.\n"
            "- Places, packing items, and weather/trip numbers must only ever be ones that "
            "actually appear above — never invent or add one of your own, even a small, "
            "plausible-sounding extra like a spare packing item. If something specific "
            "wasn't provided, say so plainly in passing — but never refuse or apologize "
            "when the data above already answers the question; declining when the answer "
            "is right there is worse than a short one. General travel knowledge about the "
            "destination itself (well-known local dishes, what a region is famous for) is "
            "fine to mention as color, since that's common knowledge about the place, not "
            "a claim about specific computed data — but keep it general to the destination, "
            "never attributed to one of the specific real venues above. You have no menu, "
            "hours, or review data for any listed place, so never say what a named "
            "restaurant serves, claim a specific dish is 'their specialty' or 'a must-try "
            "there', or invent why a specific place is good beyond its type and what's "
            "generally nearby.\n"
            "- When you mention a place, give it a little life — what its type suggests "
            "about the experience (a museum is browsing exhibits, a cafe is a relaxed "
            "sit-down), how it fits the day's weather, roughly where it falls in the day "
            "— the way you'd tip off a friend, not just recite a name from a list. This is "
            "color about the kind of place it is, never invented specifics about that one "
            "venue (no menu items, no 'their famous X', no made-up backstory).\n"
            f"- Match the answer to what was asked: {shape}"
        )
        if wants_detail:
            # The user explicitly asked for a checklist, a full itinerary, or
            # "every day" (or the intent itself already implies wanting a
            # full spread — itinerary/recommendation asks, not just a quick
            # answer). Live-observed: forcing a packing "checklist" ask into
            # 3 sentences with no lists produced an outright refusal, and a
            # "3 restaurants a day" ask stayed capped at one place total.
            sections.append(
                "RESPONSE GUIDELINES:\n" + always_rules + "\n"
                "- Give the full picture — every real place above that's relevant, with a "
                "bit of color on each (what it's known for, what's nearby, why it fits). "
                "Length should match what a genuinely useful answer needs, not be padded "
                "or clipped short.\n"
                "- A markdown list (one item, or one day, per line) works well for a "
                "checklist or a day-by-day plan — use it when it makes the answer easier "
                "to follow. For an actual packing checklist specifically, use task-list "
                "syntax (`- [ ] item`) instead of a plain bullet — it renders as a real "
                "checkbox. A markdown table is the clearest shape for a day-by-day "
                "itinerary with more than one column of information (e.g. time, place, "
                "why it fits). A short `> ` blockquote line works well for one standout "
                "tip or watch-out you want to set apart from the rest of the answer — use "
                "it sparingly, not for every sentence.\n"
                "- Still never invent an item, place or fact not listed above."
            )
        else:
            sections.append(
                "RESPONSE GUIDELINES:\n" + always_rules + "\n"
                "- Write however long feels natural for a real answer — often a short "
                "paragraph, sometimes just a sentence or two if that's genuinely all it "
                "takes. Don't pad it out with a closing well-wish or an offer to help "
                "further, but don't clip it into a robotic fragment either.\n"
                "- Do not restate the trip's score, confidence, best/watch-out days, full "
                "packing list or day-by-day breakdown unless the user asked for that "
                "specific thing — the interface already shows all of it beside your reply, "
                "and repeating it is noise.\n"
                "- A couple of places is usually plenty here — save the full rundown for "
                "when they ask for one."
            )

        return "\n\n".join(sections)

    def _fallback_response(
        self,
        context: TripContext,
        intelligence: WeatherIntelligence | None,
        attractions: AttractionRecommendation | None,
    ) -> str:
        """Structured fallback when the LLM call fails or returns nothing."""
        dest = (
            context.destination.display_name
            if context.destination
            else context.destination_query or "your destination"
        )
        lines = [f"Your trip to {dest} is taking shape!", ""]

        if intelligence:
            ts = intelligence.trip_summary
            lines.append(
                f"Trip suitability: {ts.trip_suitability_score}/100 "
                f"({ts.overall_risk_level} risk)"
            )
            if ts.best_days:
                best = ", ".join(d.isoformat() for d in ts.best_days[:2])
                lines.append(f"Best days: {best}")
            if ts.worst_days:
                worst = ", ".join(d.isoformat() for d in ts.worst_days[:2])
                lines.append(f"Watch out for: {worst}")
            if ts.overall_packing_list:
                lines.append(f"Packing: {', '.join(ts.overall_packing_list[:5])}")
            lines.append("")

        if attractions:
            for day_attr in attractions.daily[:3]:
                day_str = day_attr.date.strftime("%a %b %d")
                day_places = day_attr.attractions[:_PLACES_PER_DAY_IN_RESPONSE]
                names = ", ".join(a.name for a in day_places)
                if names:
                    lines.append(f"{day_str}: {names}")
            lines.append("")

        lines.append("Let me know if you'd like more details!")
        return "\n".join(lines).strip()
