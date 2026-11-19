"""Hermetic unit tests for Reciprocal Rank Fusion (R1-T3) — pure, no DB, no settings.

Anchored to a HAND-WORKED example (k=60) so the formula + ordering are pinned, not
self-generated from the implementation under test.
"""

from __future__ import annotations

from dataclasses import dataclass

from ai_agents.rag.fusion import reciprocal_rank_fusion

_K = 60


@dataclass(frozen=True)
class _Item:
    """Minimal candidate double carrying the two keys the live caller fuses on."""

    embedding_id: str
    clause_ref: str | None


def _ident(x: str) -> str:
    return x


class TestHandWorkedExample:
    # List A (dense):  [doc1, doc2, doc3]  -> ranks 1,2,3
    # List B (sparse): [doc2, doc4, doc1]  -> ranks 1,2,3
    #   doc1 = 1/61 + 1/63 = 0.0322664
    #   doc2 = 1/62 + 1/61 = 0.0325224   (highest)
    #   doc3 = 1/63        = 0.0158730
    #   doc4 = 1/62        = 0.0161290
    # EXPECTED fused order: [doc2, doc1, doc4, doc3]
    def test_fused_order_matches_hand_computation(self) -> None:
        dense = ["doc1", "doc2", "doc3"]
        sparse = ["doc2", "doc4", "doc1"]
        fused = reciprocal_rank_fusion([dense, sparse], key=_ident, k=_K)
        assert fused == ["doc2", "doc1", "doc4", "doc3"]

    def test_item_in_both_lists_outscores_a_better_single_list_rank(self) -> None:
        # doc2 (rank2 in A, rank1 in B) beats doc1 (rank1 in A, rank3 in B): two terms win.
        fused = reciprocal_rank_fusion(
            [["doc1", "doc2", "doc3"], ["doc2", "doc4", "doc1"]], key=_ident, k=_K
        )
        assert fused.index("doc2") < fused.index("doc1")


class TestKeyAndRankSemantics:
    def test_rank_is_one_based_top_wins(self) -> None:
        # A single list: rank 1 (1/(k+1)) must outrank rank 2 (1/(k+2)).
        fused = reciprocal_rank_fusion([["top", "second"]], key=_ident, k=_K)
        assert fused == ["top", "second"]

    def test_item_in_all_three_lists_sums_three_terms(self) -> None:
        a = ["x", "p"]
        b = ["x", "q"]
        c = ["x", "r"]
        fused = reciprocal_rank_fusion([a, b, c], key=_ident, k=_K)
        # x: 3 * 1/(k+1) dominates any single-list rank-1 (1/(k+1)).
        assert fused[0] == "x"

    def test_fusion_keys_collapse_same_entity_across_lists(self) -> None:
        # Two distinct items that share an embedding_id are ONE fused entity (terms sum);
        # the representative is the first-seen occurrence.
        d1 = _Item(embedding_id="e-1", clause_ref="A-01")  # dense rank 1
        s1 = _Item(embedding_id="e-1", clause_ref="A-01")  # sparse rank 1 (same chunk)
        s2 = _Item(embedding_id="e-2", clause_ref="B-02")  # sparse rank 2 (sparse-only)
        fused = reciprocal_rank_fusion([[d1], [s1, s2]], key=lambda c: c.embedding_id, k=_K)
        assert [c.embedding_id for c in fused] == ["e-1", "e-2"]
        assert fused[0] is d1  # first-seen representative kept


class TestKConfigDriven:
    def test_changing_k_changes_the_fused_order(self) -> None:
        # P appears once at rank 1 (dense); Q appears at rank 5 in BOTH lists.
        #   P = 1/(k+1);  Q = 2/(k+5).   P > Q  iff  (k+5) > 2(k+1)  iff  k < 3.
        dense = ["P", "a", "b", "c", "Q"]
        sparse = ["d", "e", "f", "g", "Q"]
        small_k = reciprocal_rank_fusion([dense, sparse], key=_ident, k=2)
        large_k = reciprocal_rank_fusion([dense, sparse], key=_ident, k=60)
        assert small_k.index("P") < small_k.index("Q")  # k=2 -> P wins
        assert large_k.index("Q") < large_k.index("P")  # k=60 -> Q wins (k is used)


class TestTieBreaking:
    def test_equal_scores_break_on_tie_breaker_asc(self) -> None:
        # X at rank 1 of list A, Y at rank 1 of list B -> both 1/(k+1) -> exact tie.
        x = _Item(embedding_id="e-x", clause_ref="AIR-01")
        y = _Item(embedding_id="e-y", clause_ref="LOD-01")
        fused = reciprocal_rank_fusion(
            [[x], [y]], key=lambda c: c.embedding_id, k=_K, tie_breaker=lambda c: c.clause_ref or ""
        )
        assert [c.clause_ref for c in fused] == ["AIR-01", "LOD-01"]

    def test_tie_breaker_is_deterministic_regardless_of_input_order(self) -> None:
        x = _Item(embedding_id="e-x", clause_ref="AIR-01")
        y = _Item(embedding_id="e-y", clause_ref="LOD-01")

        def _fuse(lists: list[list[_Item]]) -> list[str | None]:
            fused = reciprocal_rank_fusion(
                lists, key=lambda c: c.embedding_id, k=_K, tie_breaker=lambda c: c.clause_ref or ""
            )
            return [c.clause_ref for c in fused]

        assert _fuse([[x], [y]]) == _fuse([[y], [x]]) == ["AIR-01", "LOD-01"]

    def test_none_safe_tie_breaker(self) -> None:
        # clause_ref can be None on a candidate; the live tie_breaker must not crash and
        # must order deterministically: (is_none, ref) puts non-None refs first.
        a = _Item(embedding_id="e-a", clause_ref=None)
        b = _Item(embedding_id="e-b", clause_ref="ZZZ")
        fused = reciprocal_rank_fusion(
            [[a], [b]],
            key=lambda c: c.embedding_id,
            k=_K,
            tie_breaker=lambda c: (c.clause_ref is None, c.clause_ref or ""),
        )
        assert [c.embedding_id for c in fused] == ["e-b", "e-a"]


class TestEmptyCases:
    def test_one_empty_list_returns_the_other_ranking_unchanged(self) -> None:
        dense = ["a", "b", "c"]
        assert reciprocal_rank_fusion([dense, []], key=_ident, k=_K) == ["a", "b", "c"]

    def test_both_empty_returns_empty(self) -> None:
        assert reciprocal_rank_fusion([[], []], key=_ident, k=_K) == []

    def test_no_lists_returns_empty(self) -> None:
        assert reciprocal_rank_fusion([], key=_ident, k=_K) == []
