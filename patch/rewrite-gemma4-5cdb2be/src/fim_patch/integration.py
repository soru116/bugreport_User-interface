"""Attach FIM to an existing initialized project orchestrator explicitly."""
from .generator import FIMPatchGenerator


def attach_fim(orchestrator, **generator_options):
    """Keep localization and other modules; replace just patch generation.

    Pass validator=UnittestValidator(spec) for validation-based candidate selection.
    The orchestrator's existing RegressionTester remains in the downstream path.
    """
    orchestrator.patch_generator = FIMPatchGenerator(**generator_options)
    return orchestrator
