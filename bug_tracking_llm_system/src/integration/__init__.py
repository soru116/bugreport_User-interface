"""Integration layer that wires the team's implemented sub-projects into one ticket workflow.

Every stage calls the sub-project's own code instead of re-implementing it:

* extraction   -> ToJson/scripts/To_Json/extract_to_json.py (Code Llama-Instruct via Ollama)
* duplicate    -> bug-duplicate-detection/src/duplicate_ticket_detection (SBERT / TF-IDF + rerank)
* priority     -> bug-priority-drone/scripts/build_features.py + recall-balanced model bundle
* assignee     -> bug_tracking_llm_system/src/modules/assignee_triager.py
* localization -> bug_tracking_llm_system/src/modules/bug_localizer.py (Stage-3, identical to
                  fault_localization_feature_files/src/utils/fault_localization.py)

When a trained artifact or service is missing, a stage reports ``status="fallback"`` together
with the reason instead of silently pretending that the trained model ran.
"""

from integration.config import IntegrationConfig, load_integration_config

__all__ = ["IntegrationConfig", "load_integration_config"]
