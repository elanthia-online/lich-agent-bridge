import unittest

from lich_agent_bridge.errors import ValidationError
from lich_agent_bridge.protocol import AskRequest, CharacterSnapshot, Observation
from .test_character_knowledge import character_snapshot


class ProtocolTests(unittest.TestCase):
    def test_observation_normalizes_room_id(self):
        event = Observation.from_mapping(
            {
                "timestamp": "2026-08-30T12:00:00Z",
                "character": "Testscout",
                "text": "A massive troll king arrives.",
                "room_id": 123,
            }
        )
        self.assertEqual(event.room_id, "123")

    def test_ask_rejects_command_fields(self):
        with self.assertRaisesRegex(ValidationError, "unsupported field.*command"):
            AskRequest.from_mapping(
                {"character": "Testscout", "question": "What happened?", "command": "attack"}
            )

    def test_blank_question_is_invalid(self):
        with self.assertRaisesRegex(ValidationError, "must not be blank"):
            AskRequest.from_mapping({"character": "Testscout", "question": "  "})

    def test_ask_read_only_and_session_binding_are_strict_and_optional(self):
        payload = {"character": "Testmage", "question": "My training?"}
        legacy = AskRequest.from_mapping(payload)
        self.assertFalse(legacy.read_only)
        self.assertIsNone(legacy.expected_generation)
        protected = AskRequest.from_mapping({**payload, "read_only": True, "expected_generation": "one"})
        self.assertTrue(protected.read_only)
        self.assertEqual(protected.expected_generation, "one")
        for field, values in (("read_only", [None, 0, 1, "false", []]),
                              ("expected_generation", [None, "", " ", 1, [], "x" * 129])):
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                    AskRequest.from_mapping({**payload, field: value})

    def test_snapshot_parses_bounded_structured_state(self):
        snapshot = CharacterSnapshot.from_mapping(
            {
                "character": "Testmage",
                "generation": "generation-1",
                "sequence": 7,
                "observed_at": "2026-08-31T12:00:00Z",
                "room": {"id": 123, "title": "Town Well"},
                "vitals": {"health": {"current": 90, "max": 100}},
                "hands": {
                    "right": {"id": 456, "name": "a runestaff"},
                    "left": None,
                },
                "nearby": {
                    "creatures": [
                        {"id": 789, "noun": "mastiff", "name": "a stone mastiff"}
                    ]
                },
                "owners": {"combat": "bigshot", "movement": None},
                "script_status": {"bigshot": "running"},
            }
        )
        payload = snapshot.to_mapping()
        self.assertEqual(payload["room"]["id"], "123")
        self.assertEqual(payload["hands"]["right"]["id"], "456")
        self.assertEqual(payload["nearby"]["creatures"][0]["noun"], "mastiff")
        self.assertEqual(payload["script_status"]["bigshot"], "running")

    def test_snapshot_allows_negative_and_overmax_current_vitals(self):
        snapshot = CharacterSnapshot.from_mapping(
            {
                "character": "Testscout",
                "generation": "generation-1",
                "sequence": 1,
                "observed_at": "2026-08-31T12:00:00Z",
                "vitals": {
                    "health": {"current": -15, "max": 100},
                    "mana": {"current": 120, "max": 100},
                },
            }
        )
        self.assertEqual(snapshot.vitals["health"]["current"], -15)
        self.assertEqual(snapshot.vitals["mana"]["current"], 120)

    def test_snapshot_rejects_unknown_nested_fields(self):
        with self.assertRaisesRegex(ValidationError, "unsupported field.*secret"):
            CharacterSnapshot.from_mapping(
                {
                    "character": "Testscout",
                    "generation": "generation-1",
                    "sequence": 1,
                    "observed_at": "2026-08-31T12:00:00Z",
                    "hands": {
                        "right": {"id": "1", "name": "a sword", "secret": True}
                    },
                }
            )

    def test_snapshot_requires_timezone_aware_observed_at(self):
        with self.assertRaisesRegex(ValidationError, "include a timezone"):
            CharacterSnapshot.from_mapping(
                {
                    "character": "Testscout",
                    "generation": "generation-1",
                    "sequence": 1,
                    "observed_at": "2026-08-31T12:00:00",
                }
            )

    def test_character_build_roundtrip_preserves_zero_and_no_invented_base_stats(self):
        snapshot = character_snapshot()
        result = CharacterSnapshot.from_mapping(snapshot.to_mapping()).character_data
        self.assertEqual(result, snapshot.character_data)
        self.assertEqual(result["skills"]["values"]["skills"]["armoruse"]["ranks"], 0)
        self.assertNotIn("base_value", result["info"]["values"]["stats"]["STR"])

    def test_character_build_rejects_unknown_fields_and_false_provenance(self):
        for bad in ("unknown", "cache_time", "complete_no_time", "unknown_rank", "too_many_skills", "invalid_stat"):
            payload = character_snapshot().to_mapping()
            info = payload["character_data"]["info"]
            skills = payload["character_data"]["skills"]
            if bad == "unknown":
                info["values"]["credentials"] = "not permitted"
            elif bad == "cache_time":
                info["source"] = "infomon_cache"
            elif bad == "complete_no_time":
                info["observed_at"] = None
            elif bad == "unknown_rank":
                skills["values"]["skills"]["armoruse"]["ranks"] = None
            elif bad == "too_many_skills":
                skills["values"]["skills"] = {f"skill-{index}": {"ranks": 0} for index in range(65)}
            else:
                info["values"]["stats"]["invented"] = {"value": 100}
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                CharacterSnapshot.from_mapping(payload)


if __name__ == "__main__":
    unittest.main()
