"""Try every callable target Stage-3 offers, not only the first supported one.

Why this exists. ``FIMPatchGenerator`` walks the ranked symbols and stops at
the first one the AST can patch. On the fixed five tickets that rule, not the
ranking, is what loses the repair: filtering classes out of the pool moved the
symbol the reference patch edits into the top 5 on two tickets (astropy-13236
to #4, astropy-14365 to #5), and the run still chose the same wrong function,
because a supported symbol sat at #1 or #2 and the walk ended there.

Stage-3's own scores say that walk is not worth trusting: the ranking is plain
TF-IDF retrieval, and on astropy-14365 rank 1 scores 0.425 against 0.351 for
the gold symbol at candidate #10 -- a gap of 0.074, which is noise. So this
class does not try to rank better. It refuses to bet on one target at all:
every callable target gets its own generation, and the gates judge the results
together.

Cost is linear in the number of targets. Five targets at two candidates each is
ten model calls per ticket against the current four, so lower ``candidates``
when raising ``max_targets``.

What this class does NOT do is decide which of several generated patches is the
real repair -- that needs test evidence this pipeline does not yet produce. What
it must avoid is deciding it badly, and the original rule did.

Selection rules. ``smallest_diff`` ordered the generated patches by diff size and
used the rank only to break ties. Within one function that is defensible: the
smaller of two edits to the same body is the more conservative one. Across
different functions it measures something else entirely -- how little the model
had to say about that function -- and a model with little to say about a function
is usually looking at the wrong one. On baseline-T08 the five tickets chose ranks
3, 3, 4, 4 and 5; not one chose rank 1 or 2, and 13465's gold symbol
``report_diff_values`` sat at rank 2 while a rank-4 patch won on size. The only
run that ever hit a gold symbol, rewrite-R2, was single-target at rank 2.

``rank_first`` inverts the key: Stage-3's ranking decides, and diff size only
separates patches on the same target.

It is NOT the default, and the reason is measured rather than argued.
``scripts/replay_multitarget_selection.py`` re-decided baseline-T08 under both
rules from its own records, at no model cost, and ``rank_first`` changed the
chosen target on 4 of the 5 tickets for a score of 0 better, 1 worse: same_file
fell from 3 to 2 and miss rose from 2 to 3. Neither rule ever hits a gold symbol.

The prediction that failed was 13465: the gold symbol ``report_diff_values`` does
sit at rank 2, so promoting rank should have reached it. It was reached -- and
both its candidates were rejected, one by the 50% body-retention floor (31 of 77
lines kept) and one as no executable change. The targets that produced a patch on
that ticket were ranks 1, 4 and 5, and rank 1 is ``fitsopen`` in another file
entirely, so promoting rank moved a same_file to a miss.

What that says is that selection was not the binding constraint. On a 77-line
function, asking for the whole body back cannot clear a retention floor, so the
right target is unreachable however the winners are ordered. Both rules are poor;
nothing here shows one is better, and a default is not moved on that.
"""

from __future__ import annotations

import copy

from .generator import (FIMPatchGenerator, callable_candidate_rows,
                        changed_line_count)

TARGET_POLICY_NAME = "multi_target_callable"
DEFAULT_MAX_TARGETS = 5


SELECTION_RULES = ("rank_first", "smallest_diff")


def _patch_size(result):
    return changed_line_count(result.get("patch") or "")


def selection_key(rule):
    """The sort key over ``(rank, row, result)`` triples for one rule."""

    if rule == "rank_first":
        return lambda item: (item[0], _patch_size(item[2]))
    return lambda item: (_patch_size(item[2]), item[0])


class MultiTargetGenerator:
    """Drop-in replacement for FIMPatchGenerator that scans several targets."""

    def __init__(self, backend=None, *, max_targets=DEFAULT_MAX_TARGETS,
                 expand_truncated_end=None, selection_rule="smallest_diff", **kwargs):
        if (not isinstance(max_targets, int) or isinstance(max_targets, bool)
                or not 1 <= max_targets <= 10):
            raise ValueError("max_targets must be between 1 and 10")
        self.max_targets = max_targets
        if selection_rule not in SELECTION_RULES:
            raise ValueError("Unknown selection_rule")
        self.selection_rule = selection_rule
        # The inner generator is handed one target at a time, so its own policy
        # must not scan. Scanning coordinates stay enabled: this class plays the
        # role a scanning policy used to, and a Stage-3 end_line truncated at a
        # chunk boundary is just as repairable here.
        kwargs["target_policy"] = "strict_top1"
        self.inner = FIMPatchGenerator(
            backend,
            expand_truncated_end=(True if expand_truncated_end is None
                                  else expand_truncated_end),
            **kwargs)
        self.backend = self.inner.backend
        self.candidates = self.inner.candidates
        self.seed = self.inner.seed
        self.research_mode = self.inner.research_mode
        self.generation_mode = getattr(self.inner, "generation_mode", "fim")

    def target_rows(self, bug_location):
        """Callable targets, best first; falls back to the ranked list."""

        rows = callable_candidate_rows(bug_location, self.max_targets)
        if rows:
            return rows, True
        ranked = bug_location.get("stage3_ranked_symbols")
        if not isinstance(ranked, list):
            return [], False
        return [row for row in ranked if isinstance(row, dict)][:self.max_targets], False

    def generate(self, ticket_json, bug_location, repo_path):
        if not isinstance(bug_location, dict):
            return self.inner.generate(ticket_json, bug_location, repo_path)
        rows, pool_used = self.target_rows(bug_location)
        if not rows:
            return self.inner.generate(ticket_json, bug_location, repo_path)

        attempts = []
        for rank, row in enumerate(rows, 1):
            location = copy.copy(bug_location)
            location["stage3_ranked_symbols"] = [row]
            # Seeds must not repeat across targets, or two targets whose bodies
            # happen to match would produce identical candidates.
            inner = self.inner
            inner.seed = self.seed + (rank - 1) * 1000
            result = inner.generate(ticket_json, location, repo_path)
            result["target_rank"] = rank
            attempts.append((rank, row, result))
        self.inner.seed = self.seed

        return self._merge(attempts, pool_used)

    def _merge(self, attempts, pool_used):
        generated = [(rank, row, r) for rank, row, r in attempts
                     if r.get("patch_status") == "generated"]
        if generated:
            chosen_rank, _, merged = min(generated,
                                         key=selection_key(self.selection_rule))
        else:
            chosen_rank, _, merged = attempts[0]
            merged = dict(merged)
            if merged.get("patch_status") == "generated":
                merged["patch_status"] = "no_valid_candidate"

        merged = dict(merged)
        merged["target_policy"] = TARGET_POLICY_NAME
        merged["callable_pool_used"] = pool_used
        merged["max_targets"] = self.max_targets
        merged["targets_attempted"] = len(attempts)
        merged["target_rank"] = chosen_rank
        merged["target_fallback_used"] = chosen_rank > 1
        merged["targets_generated"] = len(generated)
        merged["selection_rule"] = self.selection_rule
        # What the other rule would have chosen, recorded on every run so the
        # question "did the rule change the answer" never needs a re-run.
        merged["selection_alternatives"] = {
            rule: min(generated, key=selection_key(rule))[0] if generated else None
            for rule in SELECTION_RULES
        }

        # Every attempt, so a run can be read as "which target produced what"
        # rather than only "what the winner was".
        merged["target_attempts"] = [{
            "rank": rank,
            "file": (r.get("bug_location") or {}).get("file") or row.get("file_path"),
            "function": ((r.get("bug_location") or {}).get("function")
                         or row.get("symbol_qualified_name")),
            "symbol_kind": row.get("symbol_kind"),
            "patch_status": r.get("patch_status"),
            "selected": rank == chosen_rank,
            "candidate_reasons": [str(c.get("reason")) for c in (r.get("candidates") or [])
                                  if c.get("reason")],
            "explanation": r.get("explanation"),
        } for rank, row, r in attempts]

        # Aggregate candidate-level evidence so summary counters stay truthful:
        # they must report every model call, not just the winning target's.
        candidates, resolution = [], []
        for rank, _, r in attempts:
            for candidate in r.get("candidates") or []:
                candidate = dict(candidate)
                candidate["target_rank"] = rank
                candidates.append(candidate)
            for entry in r.get("target_resolution") or []:
                entry = dict(entry)
                entry["rank"] = rank
                resolution.append(entry)
        merged["candidates"] = candidates
        merged["target_resolution"] = resolution
        return merged
