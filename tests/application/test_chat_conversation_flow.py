"""Conversation-flow regressions from live UI testing.

Each class reproduces one transcript that went wrong in the running app:

- "Help me pick a destination for next month" resolved to "Helpt, Germany"
- "I'm not sure where yet" / "somewhere warm" got "where would you like to
  go?" instead of suggestions
- "Andaman Nicobar island" -> "october 10" asked for the start date again
- a lowercase "ooty" answer to "where?" was ignored
- "Andaman Nicobar island" resolved to a same-named village in Tamil Nadu
- a trip length stated before any date was forgotten

Most of these only occurred when the extraction LLM call failed (a Groq
rate limit) and the heuristic fallback ran, so both paths are covered.
"""

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from app.application.use_cases.chat_orchestrator import (
    ChatOrchestrator,
    _best_candidate,
    _fallback_dates,
)
from app.domain.entities.trip import GeocodedPlace
from app.domain.rules.intent import ChatIntent
from app.infrastructure.ai.llm_client import LlmClientError
from tests.application.conftest import GOA, PARIS_FRANCE, PARIS_TEXAS, FakeGeocoding

OOTY = GeocodedPlace(
    name="Udhagamandalam",
    latitude=11.4102,
    longitude=76.6950,
    country="India",
    country_code="IN",
    admin1="Tamil Nadu, Nilgiris District",
)
PUDUCHERRY = GeocodedPlace(
    name="Puducherry", latitude=11.9339, longitude=79.8298, country="India", country_code="IN"
)
GOKARNA = GeocodedPlace(
    name="Gokarna", latitude=14.5479, longitude=74.3188, country="India", country_code="IN"
)
#: A same-named place in another country — the geocoder's fuzzy match for a
#: suggestion the LLM meant in India.
VARKALA_LITHUANIA = GeocodedPlace(
    name="Varkala", latitude=55.0, longitude=24.0, country="Lithuania", country_code="LT"
)
ANDAMAN_VILLAGE = GeocodedPlace(
    name="Andamān",
    latitude=9.9,
    longitude=78.1,
    country="India",
    country_code="IN",
    admin1="Tamil Nadu, Madurai",
)
ANDAMAN_ISLANDS = GeocodedPlace(
    name="Andaman Islands",
    latitude=12.5,
    longitude=92.8,
    country="India",
    country_code="IN",
    admin1="Andaman and Nicobar",
)

_RATE_LIMITED = LlmClientError("LLM call failed: 429 Too Many Requests")


def make_orchestrator(
    geocoding, conversation_repo, intelligence_use_case, attraction_provider, llm_client
):
    return ChatOrchestrator(
        conversation_repo=conversation_repo,
        geocoding=geocoding,
        intelligence_use_case=intelligence_use_case,
        attraction_provider=attraction_provider,
        llm_client=llm_client,
        max_forecast_horizon_days=16,
    )


@pytest.fixture
def deps(conversation_repo, intelligence_use_case, attraction_provider, llm_client):
    return conversation_repo, intelligence_use_case, attraction_provider, llm_client


def _suggestions(*items: dict[str, str]) -> str:
    return json.dumps({"intro": "Here are a few warm, quiet picks:", "suggestions": list(items)})


class TestDestinationSuggestions:
    async def test_help_me_pick_offers_verified_suggestions_instead_of_asking_where(self, deps):
        conversation_repo, intelligence_use_case, attraction_provider, llm_client = deps
        geocoding = FakeGeocoding(results={"puducherry": [PUDUCHERRY], "gokarna": [GOKARNA]})
        llm_client.extraction_response = (
            '{"intent": "trip_planning", "wantsDestinationSuggestions": true}'
        )
        llm_client.suggestion_response = _suggestions(
            {
                "name": "Puducherry",
                "region": "Puducherry",
                "country": "India",
                "reason": "Calm French-quarter streets.",
            },
            {
                "name": "Gokarna",
                "region": "Karnataka",
                "country": "India",
                "reason": "Quiet beaches.",
            },
        )
        orchestrator = make_orchestrator(
            geocoding, conversation_repo, intelligence_use_case, attraction_provider, llm_client
        )

        result = await orchestrator.process_message(
            conversation_id=None, user_message="Help me pick a destination for next month."
        )

        assert result.destination_candidates == (PUDUCHERRY, GOKARNA)
        assert result.intent == ChatIntent.RECOMMENDATION_REQUEST
        assert result.llm_generated is True
        assert "1. **Puducherry, India**" in result.response
        assert "2. **Gokarna, India**" in result.response
        assert "Where would you like to go" not in result.response
        assert result.conversation.trip_context.destination is None
        # Nothing about a real trip is fetched before a destination exists.
        assert intelligence_use_case.call_count == 0

    async def test_suggestions_that_do_not_resolve_in_the_named_country_are_dropped(self, deps):
        conversation_repo, intelligence_use_case, attraction_provider, llm_client = deps
        geocoding = FakeGeocoding(results={"varkala": [VARKALA_LITHUANIA], "gokarna": [GOKARNA]})
        llm_client.extraction_response = '{"wantsDestinationSuggestions": true}'
        llm_client.suggestion_response = _suggestions(
            {"name": "Varkala", "region": "Kerala", "country": "India", "reason": "Cliffs."},
            {"name": "Atlantis", "region": None, "country": "India", "reason": "Myth."},
            {"name": "Gokarna", "region": "Karnataka", "country": "India", "reason": "Beaches."},
        )
        orchestrator = make_orchestrator(
            geocoding, conversation_repo, intelligence_use_case, attraction_provider, llm_client
        )

        result = await orchestrator.process_message(
            conversation_id=None, user_message="I want a quiet weekend somewhere warm."
        )

        assert result.destination_candidates == (GOKARNA,)
        assert "Varkala" not in result.response
        assert "Atlantis" not in result.response

    async def test_picking_a_suggestion_by_number_or_name_sets_the_destination(self, deps):
        conversation_repo, intelligence_use_case, attraction_provider, llm_client = deps
        geocoding = FakeGeocoding(results={"puducherry": [PUDUCHERRY], "gokarna": [GOKARNA]})
        llm_client.extraction_response = '{"wantsDestinationSuggestions": true}'
        llm_client.suggestion_response = _suggestions(
            {"name": "Puducherry", "country": "India", "reason": "Calm."},
            {"name": "Gokarna", "country": "India", "reason": "Beaches."},
        )
        orchestrator = make_orchestrator(
            geocoding, conversation_repo, intelligence_use_case, attraction_provider, llm_client
        )
        first = await orchestrator.process_message(
            conversation_id=None, user_message="I'm planning a trip and I'm not sure where yet."
        )

        llm_client.extraction_response = "{}"
        by_number = await orchestrator.process_message(
            conversation_id=first.conversation.id, user_message="2"
        )
        assert by_number.conversation.trip_context.destination == GOKARNA
        assert "Gokarna" in by_number.response  # now asks for the start date

        llm_client.extraction_response = '{"wantsDestinationSuggestions": true}'
        second = await orchestrator.process_message(
            conversation_id=None, user_message="Help me pick a destination"
        )
        llm_client.extraction_response = "{}"
        by_name = await orchestrator.process_message(
            conversation_id=second.conversation.id, user_message="puducherry sounds nice"
        )
        assert by_name.conversation.trip_context.destination == PUDUCHERRY

    async def test_suggestion_failure_is_honest_and_offers_no_invented_places(self, deps):
        conversation_repo, intelligence_use_case, attraction_provider, llm_client = deps
        geocoding = FakeGeocoding()
        llm_client.extraction_response = '{"wantsDestinationSuggestions": true}'
        llm_client.raise_on_suggestion = _RATE_LIMITED
        orchestrator = make_orchestrator(
            geocoding, conversation_repo, intelligence_use_case, attraction_provider, llm_client
        )

        result = await orchestrator.process_message(
            conversation_id=None, user_message="Help me pick a destination"
        )

        assert result.destination_candidates == ()
        assert result.llm_generated is False
        assert "suggest" in result.response
        assert geocoding.queries == []


class TestFallbackExtractionWhenTheLlmIsRateLimited:
    async def test_help_is_never_geocoded_as_a_destination(self, deps):
        conversation_repo, intelligence_use_case, attraction_provider, llm_client = deps
        geocoding = FakeGeocoding(results={"help": [GOA]})  # anything "Help" would hit
        llm_client.raise_on_extraction = _RATE_LIMITED
        llm_client.raise_on_suggestion = _RATE_LIMITED
        orchestrator = make_orchestrator(
            geocoding, conversation_repo, intelligence_use_case, attraction_provider, llm_client
        )

        result = await orchestrator.process_message(
            conversation_id=None, user_message="Help me pick a destination for next month."
        )

        assert "help" not in [q.lower() for q in geocoding.queries]
        assert result.conversation.trip_context.destination is None
        # Still recognised as a request for suggestions, even on this path.
        assert result.intent == ChatIntent.RECOMMENDATION_REQUEST

    async def test_lowercase_reply_to_where_is_taken_as_the_destination(self, deps):
        conversation_repo, intelligence_use_case, attraction_provider, llm_client = deps
        geocoding = FakeGeocoding(results={"ooty": [OOTY]})
        llm_client.raise_on_extraction = _RATE_LIMITED
        orchestrator = make_orchestrator(
            geocoding, conversation_repo, intelligence_use_case, attraction_provider, llm_client
        )
        first = await orchestrator.process_message(
            conversation_id=None, user_message="i want to plan a trip"
        )
        assert first.missing_essentials[0] == "destination"

        result = await orchestrator.process_message(
            conversation_id=first.conversation.id, user_message="ooty"
        )

        assert result.conversation.trip_context.destination == OOTY

    async def test_a_date_reply_is_saved_and_the_next_missing_field_is_asked(self, deps):
        conversation_repo, intelligence_use_case, attraction_provider, llm_client = deps
        geocoding = FakeGeocoding(results={"andaman": [ANDAMAN_VILLAGE, ANDAMAN_ISLANDS]})
        llm_client.raise_on_extraction = _RATE_LIMITED
        orchestrator = make_orchestrator(
            geocoding, conversation_repo, intelligence_use_case, attraction_provider, llm_client
        )
        first = await orchestrator.process_message(
            conversation_id=None, user_message="I am planning a trip"
        )
        second = await orchestrator.process_message(
            conversation_id=first.conversation.id, user_message="Andaman Nicobar island"
        )
        assert second.conversation.trip_context.destination == ANDAMAN_ISLANDS

        third = await orchestrator.process_message(
            conversation_id=first.conversation.id, user_message="october 10"
        )

        context = third.conversation.trip_context
        assert context.start_date is not None
        assert (context.start_date.month, context.start_date.day) == (10, 10)
        assert third.missing_essentials == ("end_date",)
        assert "When does your trip end" in third.response
        assert third.response != second.response

    async def test_a_full_date_range_completes_the_trip(self, deps):
        conversation_repo, intelligence_use_case, attraction_provider, llm_client = deps
        geocoding = FakeGeocoding(results={"goa": [GOA]})
        llm_client.raise_on_extraction = _RATE_LIMITED
        orchestrator = make_orchestrator(
            geocoding, conversation_repo, intelligence_use_case, attraction_provider, llm_client
        )
        start = date.today() + timedelta(days=3)
        end = start + timedelta(days=2)
        message = f"Trip to Goa from {start:%B} {start.day} to {end:%B} {end.day}"

        result = await orchestrator.process_message(conversation_id=None, user_message=message)

        assert result.conversation.trip_context.start_date == start
        assert result.conversation.trip_context.end_date == end
        assert result.context_complete is True


class TestConversationState:
    async def test_a_duration_stated_before_any_date_is_kept_until_the_start_date(self, deps):
        conversation_repo, intelligence_use_case, attraction_provider, llm_client = deps
        geocoding = FakeGeocoding(results={"ooty": [OOTY]})
        orchestrator = make_orchestrator(
            geocoding, conversation_repo, intelligence_use_case, attraction_provider, llm_client
        )
        llm_client.extraction_response = '{"destination": "Ooty", "duration": 2}'
        first = await orchestrator.process_message(
            conversation_id=None, user_message="A quiet weekend in Ooty"
        )
        assert "2 days" in first.response

        start = date.today() + timedelta(days=4)
        llm_client.extraction_response = json.dumps({"startDate": start.isoformat()})
        result = await orchestrator.process_message(
            conversation_id=first.conversation.id, user_message=f"{start:%B} {start.day}"
        )

        assert result.conversation.trip_context.end_date == start + timedelta(days=1)
        assert result.context_complete is True

    async def test_the_same_clarification_is_never_sent_twice_in_a_row(self, deps):
        conversation_repo, intelligence_use_case, attraction_provider, llm_client = deps
        geocoding = FakeGeocoding(results={"goa": [GOA]})
        orchestrator = make_orchestrator(
            geocoding, conversation_repo, intelligence_use_case, attraction_provider, llm_client
        )
        llm_client.extraction_response = '{"destination": "Goa"}'
        first = await orchestrator.process_message(conversation_id=None, user_message="Goa")
        llm_client.extraction_response = "{}"

        second = await orchestrator.process_message(
            conversation_id=first.conversation.id, user_message="hmm not sure"
        )

        assert second.missing_essentials == first.missing_essentials
        assert second.response != first.response
        assert "October 10" in second.response  # shows an answer format

    async def test_an_unfindable_destination_is_named_back_not_ignored(self, deps):
        conversation_repo, intelligence_use_case, attraction_provider, llm_client = deps
        geocoding = FakeGeocoding()
        orchestrator = make_orchestrator(
            geocoding, conversation_repo, intelligence_use_case, attraction_provider, llm_client
        )
        llm_client.extraction_response = '{"destination": "Xyzzyville"}'

        result = await orchestrator.process_message(conversation_id=None, user_message="Xyzzyville")

        assert '"Xyzzyville"' in result.response
        assert result.conversation.trip_context.destination_query is None

    async def test_an_unfindable_new_destination_keeps_the_established_trip(self, deps):
        conversation_repo, intelligence_use_case, attraction_provider, llm_client = deps
        geocoding = FakeGeocoding(results={"goa": [GOA]})
        orchestrator = make_orchestrator(
            geocoding, conversation_repo, intelligence_use_case, attraction_provider, llm_client
        )
        llm_client.extraction_response = '{"destination": "Goa"}'
        first = await orchestrator.process_message(conversation_id=None, user_message="Goa")
        llm_client.extraction_response = '{"destination": "Xyzzyville"}'

        result = await orchestrator.process_message(
            conversation_id=first.conversation.id, user_message="actually Xyzzyville"
        )

        assert result.conversation.trip_context.destination == GOA
        assert result.conversation.trip_context.destination_query == "Goa"
        assert "kept your trip to Goa, India" in result.response

    async def test_each_turn_records_what_it_was_waiting_for(self, deps):
        conversation_repo, intelligence_use_case, attraction_provider, llm_client = deps
        orchestrator = make_orchestrator(
            FakeGeocoding(),
            conversation_repo,
            intelligence_use_case,
            attraction_provider,
            llm_client,
        )

        result = await orchestrator.process_message(conversation_id=None, user_message="hi")

        assistant = result.conversation.messages[-1]
        assert assistant.metadata["missing_essentials"] == ["destination", "start_date", "end_date"]


class TestDestinationNormalization:
    def test_the_candidate_matching_more_of_the_query_wins(self):
        best = _best_candidate([ANDAMAN_VILLAGE, ANDAMAN_ISLANDS], "Andaman Nicobar island")
        assert best == ANDAMAN_ISLANDS

    def test_a_tie_keeps_the_providers_order(self):
        assert _best_candidate([OOTY, GOA], "ooty") == OOTY
        assert _best_candidate([GOA, OOTY], "somewhere") == GOA


class TestFallbackDates:
    TODAY = date(2026, 10, 3)

    @pytest.mark.parametrize(
        ("message", "start", "end"),
        [
            ("october 10", date(2026, 10, 10), None),
            ("Oct 10th", date(2026, 10, 10), None),
            ("10 october", date(2026, 10, 10), None),
            ("from October 10 to October 14", date(2026, 10, 10), date(2026, 10, 14)),
            ("october 10 to 14", date(2026, 10, 10), date(2026, 10, 14)),
            ("10-14 oct", date(2026, 10, 10), date(2026, 10, 14)),
            ("2026-10-12", date(2026, 10, 12), None),
            ("december 28 to january 2", date(2026, 12, 28), date(2027, 1, 2)),
            # Already passed this year -> next year, the prompt's own rule.
            ("september 5", date(2027, 9, 5), None),
            ("tomorrow", date(2026, 10, 4), None),
        ],
    )
    def test_recognised_phrasings(self, message, start, end):
        assert _fallback_dates(message, self.TODAY) == (start, end)

    @pytest.mark.parametrize(
        "message", ["5 days", "a quiet weekend", "october", "February 30", "Help me pick"]
    )
    def test_no_date_is_ever_guessed(self, message):
        assert _fallback_dates(message, self.TODAY) == (None, None)

    def test_a_trip_length_is_not_read_as_an_end_day(self):
        assert _fallback_dates("october 10 to 5 days", self.TODAY) == (date(2026, 10, 10), None)


class TestEndDateReply:
    async def _asked_for_end_date(self, orchestrator, llm_client, start: date):
        llm_client.extraction_response = json.dumps(
            {"destination": "Goa", "startDate": start.isoformat()}
        )
        first = await orchestrator.process_message(
            conversation_id=None, user_message="Goa starting soon"
        )
        assert first.missing_essentials == ("end_date",)
        return first

    @pytest.mark.parametrize("llm_fails", [False, True])
    async def test_a_lone_date_after_the_end_date_question_is_the_end_date(self, deps, llm_fails):
        conversation_repo, intelligence_use_case, attraction_provider, llm_client = deps
        orchestrator = make_orchestrator(
            FakeGeocoding(results={"goa": [GOA]}),
            conversation_repo,
            intelligence_use_case,
            attraction_provider,
            llm_client,
        )
        start = date.today() + timedelta(days=3)
        end = start + timedelta(days=4)
        first = await self._asked_for_end_date(orchestrator, llm_client, start)

        # The LLM (or the heuristic) reports the lone date as a start date.
        llm_client.extraction_response = json.dumps({"startDate": end.isoformat()})
        if llm_fails:
            llm_client.raise_on_extraction = _RATE_LIMITED
        result = await orchestrator.process_message(
            conversation_id=first.conversation.id, user_message=f"{end:%B} {end.day}"
        )

        assert result.conversation.trip_context.start_date == start
        assert result.conversation.trip_context.end_date == end
        assert result.context_complete is True

    async def test_an_explicit_new_start_still_moves_the_start(self, deps):
        conversation_repo, intelligence_use_case, attraction_provider, llm_client = deps
        orchestrator = make_orchestrator(
            FakeGeocoding(results={"goa": [GOA]}),
            conversation_repo,
            intelligence_use_case,
            attraction_provider,
            llm_client,
        )
        start = date.today() + timedelta(days=3)
        later = start + timedelta(days=2)
        first = await self._asked_for_end_date(orchestrator, llm_client, start)

        llm_client.extraction_response = json.dumps({"startDate": later.isoformat()})
        result = await orchestrator.process_message(
            conversation_id=first.conversation.id,
            user_message=f"actually let's start on {later:%B} {later.day}",
        )

        assert result.conversation.trip_context.start_date == later
        assert result.conversation.trip_context.end_date is None


class _ExactGeocoding(FakeGeocoding):
    """Matches whole queries only — like the real geocoder, a comma query
    that splits one name ("Andaman, Nicobar Islands") finds nothing."""

    async def search(self, query: str, *, limit: int = 5) -> list[GeocodedPlace]:
        self.queries.append(query)
        return self._results.get(query.lower(), [])


class TestGeocodingRecovery:
    async def test_a_comma_form_that_finds_nothing_is_retried_as_plain_words(self, deps):
        conversation_repo, intelligence_use_case, attraction_provider, llm_client = deps
        geocoding = _ExactGeocoding(
            results={"andaman nicobar islands": [ANDAMAN_VILLAGE, ANDAMAN_ISLANDS]}
        )
        orchestrator = make_orchestrator(
            geocoding, conversation_repo, intelligence_use_case, attraction_provider, llm_client
        )
        llm_client.extraction_response = '{"destination": "Andaman, Nicobar Islands"}'

        result = await orchestrator.process_message(
            conversation_id=None, user_message="Andaman Nicobar island"
        )

        assert result.conversation.trip_context.destination == ANDAMAN_ISLANDS
        assert geocoding.queries == ["Andaman, Nicobar Islands", "Andaman Nicobar Islands"]

    async def test_a_suggestion_prefers_an_exact_place_then_its_region(self, deps):
        conversation_repo, intelligence_use_case, attraction_provider, llm_client = deps
        airport = GeocodedPlace(
            name="Pondicherry Airport", latitude=11.96, longitude=79.81, country="India"
        )
        geocoding = _ExactGeocoding(results={"pondicherry": [airport], "puducherry": [PUDUCHERRY]})
        llm_client.extraction_response = '{"wantsDestinationSuggestions": true}'
        llm_client.suggestion_response = _suggestions(
            {"name": "Pondicherry", "region": "Puducherry", "country": "India", "reason": "Calm."}
        )
        orchestrator = make_orchestrator(
            geocoding, conversation_repo, intelligence_use_case, attraction_provider, llm_client
        )

        result = await orchestrator.process_message(
            conversation_id=None, user_message="somewhere calm by the sea"
        )

        assert result.destination_candidates == (PUDUCHERRY,)
        assert "Airport" not in result.response


class TestAmbiguousDestinationKeepsTheRestOfTheTrip:
    """Live-reported: "Plan a 5 day trip to Paris from October 10 to October
    14. I like beaches and photography." -> "which Paris?" -> "1" asked for
    the start date again. Choosing between candidates must only fill in the
    destination; everything else the first message said has to survive."""

    START = date.today() + timedelta(days=5)
    END = START + timedelta(days=4)
    MESSAGE = (
        f"Plan a 5 day trip to Paris from {START:%B} {START.day} to {END:%B} {END.day}. "
        "I like beaches and photography."
    )

    def _assert_complete_paris_trip(self, result) -> None:
        context = result.conversation.trip_context
        assert context.destination == PARIS_FRANCE
        assert context.start_date == self.START
        assert context.end_date == self.END
        assert context.duration_days == 5
        # "beaches" from the LLM path, "beach" from the heuristic one.
        assert {"beach", "photography"} <= {i.removesuffix("es") for i in context.interests}
        assert result.missing_essentials == ()
        assert result.context_complete is True
        # A trip answer, not another clarifying question.
        assert "When does your trip" not in result.response

    async def _run(self, deps, *, llm_fails: bool):
        conversation_repo, intelligence_use_case, attraction_provider, llm_client = deps
        orchestrator = make_orchestrator(
            FakeGeocoding(results={"paris": [PARIS_FRANCE, PARIS_TEXAS]}),
            conversation_repo,
            intelligence_use_case,
            attraction_provider,
            llm_client,
        )
        llm_client.extraction_response = json.dumps(
            {
                "destination": "Paris",
                "startDate": self.START.isoformat(),
                "endDate": self.END.isoformat(),
                "interests": ["beaches", "photography"],
                "intent": "trip_planning",
            }
        )
        if llm_fails:
            llm_client.raise_on_extraction = _RATE_LIMITED

        first = await orchestrator.process_message(conversation_id=None, user_message=self.MESSAGE)

        assert first.destination_candidates[:2] == (PARIS_FRANCE, PARIS_TEXAS)
        held = first.conversation.trip_context
        assert (held.start_date, held.end_date, held.duration_days) == (self.START, self.END, 5)
        assert "photography" in held.interests

        # Whatever the extractor would say about "1" must not matter.
        llm_client.raise_on_extraction = None
        llm_client.extraction_response = json.dumps({"duration": 1, "startDate": "2030-01-01"})
        extractions_before = llm_client.extraction_call_count

        second = await orchestrator.process_message(
            conversation_id=first.conversation.id, user_message="1"
        )

        assert llm_client.extraction_call_count == extractions_before
        return second, intelligence_use_case

    async def test_selection_completes_the_trip_from_the_original_message(self, deps):
        result, intelligence_use_case = await self._run(deps, llm_fails=False)

        self._assert_complete_paris_trip(result)
        # The dashboard data is fetched for the full, merged trip.
        assert intelligence_use_case.call_count == 1
        assert intelligence_use_case.last_call is not None

    async def test_same_when_the_first_turn_fell_back_to_the_heuristic(self, deps):
        result, _ = await self._run(deps, llm_fails=True)

        self._assert_complete_paris_trip(result)

    async def test_a_selection_with_extra_details_is_still_extracted(self, deps):
        conversation_repo, intelligence_use_case, attraction_provider, llm_client = deps
        orchestrator = make_orchestrator(
            FakeGeocoding(results={"paris": [PARIS_FRANCE, PARIS_TEXAS]}),
            conversation_repo,
            intelligence_use_case,
            attraction_provider,
            llm_client,
        )
        llm_client.extraction_response = '{"destination": "Paris"}'
        first = await orchestrator.process_message(conversation_id=None, user_message="Paris")
        llm_client.extraction_response = json.dumps(
            {"startDate": self.START.isoformat(), "endDate": self.END.isoformat()}
        )

        start, end = self.START, self.END
        result = await orchestrator.process_message(
            conversation_id=first.conversation.id,
            user_message=f"1, from {start:%B} {start.day} to {end:%B} {end.day}",
        )

        assert result.conversation.trip_context.destination == PARIS_FRANCE
        assert result.conversation.trip_context.end_date == self.END


class TestTripsBeyondTheForecastWindow:
    """Trip length is the user's; the forecast window is the provider's.

    Previously a trip longer than 16 days was refused ("I can only plan up to
    16 days at a time") and one ending past the window was refused too, with
    its dates thrown away.
    """

    TODAY = datetime.now(UTC).date()
    WINDOW_END = TODAY + timedelta(days=15)  # 16 days including today

    def _orchestrator(self, deps):
        conversation_repo, intelligence_use_case, attraction_provider, llm_client = deps
        return make_orchestrator(
            FakeGeocoding(results={"goa": [GOA]}),
            conversation_repo,
            intelligence_use_case,
            attraction_provider,
            llm_client,
        )

    async def _plan(self, deps, start: date, end: date):
        llm_client = deps[3]
        llm_client.extraction_response = json.dumps(
            {"destination": "Goa", "startDate": start.isoformat(), "endDate": end.isoformat()}
        )
        return await self._orchestrator(deps).process_message(
            conversation_id=None, user_message="Goa trip"
        )

    async def test_a_20_day_trip_is_analysed_for_the_forecastable_days(self, deps):
        _, intelligence_use_case, attraction_provider, llm_client = deps
        start = self.TODAY + timedelta(days=5)
        end = start + timedelta(days=19)

        result = await self._plan(deps, start, end)

        context = result.conversation.trip_context
        assert (context.start_date, context.end_date) == (start, end)  # never shortened
        assert context.duration_days == 20
        assert result.context_complete is True
        assert "16 days" not in result.response
        assert intelligence_use_case.last_call["start"] == start
        assert intelligence_use_case.last_call["end"] == self.WINDOW_END
        assert attraction_provider.calls_trip_context[-1].end_date == self.WINDOW_END
        # The uncovered dates are stated exactly, after the model's reply.
        first_uncovered = self.WINDOW_END + timedelta(days=1)
        assert result.response.startswith(llm_client.chat_response)
        assert "beyond the available forecast range" in result.response
        assert f"{first_uncovered:%B} {first_uncovered.day}" in result.response
        fully_covered_from = end - timedelta(days=15)
        assert f"from {fully_covered_from:%B} {fully_covered_from.day}" in result.response
        # And the model was told not to describe weather for them.
        response_prompt = next(p for p, _, json_mode, _ in llm_client.calls if not json_mode)
        assert "FORECAST COVERAGE" in response_prompt

    async def test_a_trip_ending_on_the_last_forecast_day_has_no_note(self, deps):
        intelligence_use_case = deps[1]

        result = await self._plan(deps, self.TODAY + timedelta(days=2), self.WINDOW_END)

        assert intelligence_use_case.last_call["end"] == self.WINDOW_END
        assert "forecast range" not in result.response

    async def test_a_trip_entirely_beyond_the_window_is_kept_without_a_fetch(self, deps):
        _, intelligence_use_case, attraction_provider, _ = deps
        start = self.WINDOW_END + timedelta(days=20)
        end = start + timedelta(days=5)

        result = await self._plan(deps, start, end)

        assert result.context_complete is True
        assert result.conversation.trip_context.start_date == start
        assert intelligence_use_case.call_count == 0
        assert attraction_provider.call_count == 0
        assert "6-day trip" in result.response and "is saved" in result.response
        available_from = start - timedelta(days=15)
        assert f"{available_from:%B} {available_from.day}" in result.response

    async def test_a_follow_up_on_a_trip_beyond_the_window_does_not_fetch_either(self, deps):
        _, intelligence_use_case, _, llm_client = deps
        orchestrator = self._orchestrator(deps)
        start = self.WINDOW_END + timedelta(days=30)
        llm_client.extraction_response = json.dumps(
            {
                "destination": "Goa",
                "startDate": start.isoformat(),
                "endDate": (start + timedelta(days=3)).isoformat(),
            }
        )
        first = await orchestrator.process_message(conversation_id=None, user_message="Goa")
        llm_client.extraction_response = '{"intent": "packing_request"}'

        result = await orchestrator.process_message(
            conversation_id=first.conversation.id, user_message="what should I pack?"
        )

        assert intelligence_use_case.call_count == 0
        assert "is saved" in result.response


class TestSelectionNumberIsNotADate:
    """Live report: "2 from October 10 to November 10", answering a list of
    suggested destinations, came out as a 32-day trip. Oct 10 - Nov 10 *is*
    32 days inclusive; the "2" must only pick the option."""

    async def _suggest(self, deps):
        conversation_repo, intelligence_use_case, attraction_provider, llm_client = deps
        geocoding = FakeGeocoding(results={"puducherry": [PUDUCHERRY], "gokarna": [GOKARNA]})
        orchestrator = make_orchestrator(
            geocoding, conversation_repo, intelligence_use_case, attraction_provider, llm_client
        )
        llm_client.extraction_response = '{"wantsDestinationSuggestions": true}'
        llm_client.suggestion_response = _suggestions(
            {"name": "Puducherry", "country": "India", "reason": "Calm."},
            {"name": "Gokarna", "country": "India", "reason": "Beaches."},
        )
        first = await orchestrator.process_message(
            conversation_id=None, user_message="help me pick somewhere"
        )
        return orchestrator, first

    async def test_the_option_number_selects_and_never_reaches_the_dates(self, deps):
        llm_client = deps[3]
        orchestrator, first = await self._suggest(deps)
        start = date(2026, 10, 10)
        end = date(2026, 11, 10)
        llm_client.extraction_response = json.dumps(
            {"startDate": start.isoformat(), "endDate": end.isoformat()}
        )

        result = await orchestrator.process_message(
            conversation_id=first.conversation.id, user_message="2 from October 10 to November 10"
        )

        context = result.conversation.trip_context
        assert context.destination == GOKARNA
        assert (context.start_date, context.end_date) == (start, end)
        assert context.duration_days == (end - start).days + 1 == 32

    async def test_same_through_the_heuristic_path(self, deps):
        llm_client = deps[3]
        orchestrator, first = await self._suggest(deps)
        llm_client.raise_on_extraction = _RATE_LIMITED

        result = await orchestrator.process_message(
            conversation_id=first.conversation.id, user_message="2 from October 10 to November 10"
        )

        context = result.conversation.trip_context
        assert context.destination == GOKARNA
        assert context.start_date is not None and context.end_date is not None
        assert (context.start_date.month, context.start_date.day) == (10, 10)
        assert (context.end_date.month, context.end_date.day) == (11, 10)
        assert context.duration_days == (context.end_date - context.start_date).days + 1
