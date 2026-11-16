"""Shared constants for releasing a draft whose only failed checks are content checks.

Standard library only, no project imports: local_rag_answer, public_rag_response and
evaluate_local_rag all read these, and public_rag_response must import them both as a
top-level module (runtime: sys.path holds src/) and as src.public_rag_response
(evaluation tools: sys.path holds only the project root).
"""

ATTRIBUTION_ISSUE = "claim_attributes_uncited_provision"
INTRO_ONLY_ISSUE = "claim_cites_only_intro_of_itemized_paragraph"

# Content-check issues that alone do not withhold a draft (a withheld answer
# scores as wrong). Such a draft goes through the same provision check as a
# passed draft and is published as released_with_check_issues; any other issue
# mixed in keeps the draft held exactly as before.
RELEASABLE_CHECK_ISSUES = frozenset({"answer_contains_unsupported_number", "unverified_derived_calculation",
                                     INTRO_ONLY_ISSUE, "obligation_strength_mismatch", ATTRIBUTION_ISSUE})
RELEASED_WITH_CHECK_ISSUES = "released_with_check_issues"
