"""
Tests for fuzzy matching utilities.
"""

from ledger.utils.fuzzy import find_best_match, find_merchant_match


class TestFindBestMatch:
    """Test find_best_match function."""

    def test_exact_match(self):
        """Test exact match returns 100 score."""
        candidates = ["MCDONALDS", "STARBUCKS", "TIM HORTONS"]
        match, score = find_best_match("MCDONALDS", candidates, threshold=85)
        assert match == "MCDONALDS"
        assert score == 100

    def test_close_match(self):
        """Test close match above threshold."""
        candidates = ["MCDONALDS", "STARBUCKS", "TIM HORTONS"]
        match, score = find_best_match("MCDONALD'S #123", candidates, threshold=85)
        assert match == "MCDONALDS"
        assert score >= 85

    def test_no_match_below_threshold(self):
        """Test no match when score below threshold."""
        candidates = ["MCDONALDS", "STARBUCKS"]
        match, score = find_best_match("AMAZON", candidates, threshold=85)
        assert match is None
        assert score == 0

    def test_case_insensitive(self):
        """Test matching is case insensitive."""
        candidates = ["MCDONALDS"]
        match, score = find_best_match("mcdonalds", candidates, threshold=85)
        assert match == "MCDONALDS"
        assert score == 100

    def test_typo_correction(self):
        """Test fuzzy matching handles typos."""
        candidates = ["STARBUCKS"]
        match, score = find_best_match("STARBCKS", candidates, threshold=80)
        assert match == "STARBUCKS"
        assert score >= 80

    def test_min_length_filter(self):
        """Test min_length prevents short queries."""
        candidates = ["MCDONALDS"]
        match, score = find_best_match("MC", candidates, threshold=85, min_length=3)
        assert match is None
        assert score == 0

    def test_empty_candidates(self):
        """Test empty candidates returns None."""
        match, score = find_best_match("MCDONALDS", [], threshold=85)
        assert match is None
        assert score == 0

    def test_empty_query(self):
        """Test empty query returns None."""
        candidates = ["MCDONALDS"]
        match, score = find_best_match("", candidates, threshold=85)
        assert match is None
        assert score == 0


class TestFindMerchantMatch:
    """Test find_merchant_match convenience function."""

    def test_finds_match(self):
        """Test successful match."""
        merchants = ["MCDONALDS", "STARBUCKS", "TIM HORTONS"]
        match = find_merchant_match("MCDONALD'S", merchants, threshold=85)
        assert match == "MCDONALDS"

    def test_no_match(self):
        """Test no match returns None."""
        merchants = ["MCDONALDS", "STARBUCKS"]
        match = find_merchant_match("AMAZON", merchants, threshold=85)
        assert match is None

    def test_partial_match(self):
        """Test partial merchant name matching."""
        merchants = ["UBER EATS", "UBER"]
        # Should match the longer/more specific one
        match = find_merchant_match("UBER EATS DELIVERY", merchants, threshold=80)
        assert match is not None
        assert "UBER" in match


class TestRealWorldExamples:
    """Test real-world fuzzy matching scenarios."""

    def test_merchant_variations(self):
        """Test matching merchant name variations."""
        merchants = ["MCDONALDS", "STARBUCKS", "TIM HORTONS", "UBER EATS"]

        test_cases = [
            ("MCDONALD'S #123 TORONTO", "MCDONALDS"),
            ("MCDONALD'S YONGE ST", "MCDONALDS"),
            ("STARBUCKS COFFEE", "STARBUCKS"),
            ("TIM HORTONS #456", "TIM HORTONS"),
            ("UBER CANADA/UBEREATS", "UBER EATS"),
        ]

        for raw, expected in test_cases:
            match = find_merchant_match(raw, merchants, threshold=70)
            assert match == expected, f"Failed to match '{raw}' to '{expected}', got '{match}'"

    def test_misspellings(self):
        """Test fuzzy matching handles common misspellings."""
        merchants = ["SHOPPERS DRUG MART", "COSTCO", "LOBLAWS"]

        test_cases = [
            ("SHOPPERS DRUGMART", "SHOPPERS DRUG MART"),  # Missing space
            ("COSTO", "COSTCO"),  # Missing letter
            ("LOBLAW", "LOBLAWS"),  # Missing 's'
        ]

        for raw, expected in test_cases:
            match = find_merchant_match(raw, merchants, threshold=75)
            assert match == expected, f"Failed to match '{raw}' to '{expected}', got '{match}'"

    def test_no_false_positives(self):
        """Test fuzzy matching doesn't create false positives."""
        merchants = ["MCDONALDS", "STARBUCKS"]

        # These should NOT match
        no_match_cases = [
            "AMAZON",
            "WALMART",
            "BEST BUY",
        ]

        for raw in no_match_cases:
            match = find_merchant_match(raw, merchants, threshold=85)
            assert match is None, f"Should not match '{raw}', but got '{match}'"
