from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any

from utils.json_schema import repair_json_object


@dataclass(slots=True)
class OllamaClient:
    """Minimal client for the optional local Ollama reranking stage."""

    url: str = "http://localhost:11434/api/generate"
    model: str = "codellama:7b-instruct"
    timeout: int = 180

    def generate(self, prompt: str) -> str:
        return self._generate(prompt, response_format="json")

    def _generate(self, prompt: str, *, response_format: str | dict[str, Any]) -> str:
        if self.timeout <= 0:
            raise ValueError("Ollama timeout must be positive.")
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": response_format,
            "options": {"temperature": 0.0, "top_p": 1.0, "num_predict": 768},
        }
        data = json.dumps(payload).encode("utf-8")
        timeout = float(self.timeout)
        command = [
            "curl",
            "--silent",
            "--show-error",
            "--fail-with-body",
            "--max-time",
            f"{timeout:g}",
            "--connect-timeout",
            f"{min(timeout, 10.0):g}",
            "--header",
            "Content-Type: application/json",
            "--data-binary",
            "@-",
            self.url,
        ]
        try:
            completed = subprocess.run(
                command,
                input=data,
                capture_output=True,
                check=False,
                timeout=timeout + 5.0,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("Ollama reranking requires the curl executable.") from exc
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"Ollama request exceeded the {timeout:g}s hard deadline.") from exc
        if completed.returncode == 28:
            raise TimeoutError(f"Ollama request exceeded the {timeout:g}s hard deadline.")
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Ollama request failed: {detail or f'curl exit {completed.returncode}'}")
        try:
            body = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Ollama returned an invalid JSON response envelope.") from exc
        return str(body.get("response", "")).strip()

    def generate_json(self, prompt: str) -> dict[str, Any]:
        return repair_json_object(self.generate(prompt))

    def generate_json_with_schema(
        self,
        prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Request an Ollama response constrained by a JSON Schema."""

        return repair_json_object(self._generate(prompt, response_format=schema))
