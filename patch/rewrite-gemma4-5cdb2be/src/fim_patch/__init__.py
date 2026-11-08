"""Existing-model FIM patch generation for Stage-3 localization outputs."""
from .generator import FIMPatchGenerator
from .backend import OllamaFIM, OllamaRewrite

__all__ = ["FIMPatchGenerator", "OllamaFIM", "OllamaRewrite"]
