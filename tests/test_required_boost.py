import pytest
import asyncio

from types import SimpleNamespace

from src.form_sender.analyzer.field_mapper import FieldMapper


class FakeElement:
    pass


class FakeElementScorer:
    async def calculate_element_score(self, element, field_patterns, field_name):
        # 強いシグナルを一つ含めて boost の発火条件を満たす
        return 60, {'score_breakdown': {'name': 1}}

    async def _detect_required_status(self, element, parallel_groups=None):
        return True


class FakeContextExtractor:
    async def extract_context_for_element(self, element, bounds):
        return []


class FakeFieldPatterns:
    pass


class FakeDupPrevent:
    pass


def test_required_boost_applied_from_settings_normal():
    mapper = FieldMapper(
        page=None,
        element_scorer=FakeElementScorer(),
        context_text_extractor=FakeContextExtractor(),
        field_patterns=FakeFieldPatterns(),
        duplicate_prevention=FakeDupPrevent(),
        settings={
            'required_boost': 45,
            'required_phone_boost': 210,
            'quick_top_k': 10,
            'essential_fields': [],
        },
        create_enhanced_element_info_func=None,
        generate_temp_value_func=None,
        field_combination_manager=None,
    )
    mapper._element_bounds_cache = {}
    import asyncio
    score, details, ctx = asyncio.get_event_loop().run_until_complete(
        mapper._score_element_in_detail(FakeElement(), {}, '会社名')
    )
    assert score == 60 + 45
    assert details['score_breakdown']['required_boost'] == 45


def test_required_boost_applied_from_settings_phone():
    mapper = FieldMapper(
        page=None,
        element_scorer=FakeElementScorer(),
        context_text_extractor=FakeContextExtractor(),
        field_patterns=FakeFieldPatterns(),
        duplicate_prevention=FakeDupPrevent(),
        settings={
            'required_boost': 40,
            'required_phone_boost': 200,
            'quick_top_k': 10,
            'essential_fields': [],
        },
        create_enhanced_element_info_func=None,
        generate_temp_value_func=None,
        field_combination_manager=None,
    )
    mapper._element_bounds_cache = {}
    import asyncio
    score, details, ctx = asyncio.get_event_loop().run_until_complete(
        mapper._score_element_in_detail(FakeElement(), {}, '電話番号')
    )
    assert score == 60 + 200
    assert details['score_breakdown']['required_boost'] == 200
