from abc import ABC, abstractmethod
from typing import Type, TypeVar
import logging
from pydantic import BaseModel
import os

T = TypeVar('T', bound=BaseModel)
logger = logging.getLogger(__name__)

class AIProvider(ABC):
    @abstractmethod
    def ask_structured(self, prompt: str, response_schema: Type[T]) -> T:
        """Sends a prompt and guarantees a parsed Pydantic model in return."""
        pass

class GeminiProvider(AIProvider):
    def __init__(self):
        from google import genai
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError(
                "GEMINI_API_KEY environment variable is required. "
                "Set it to use the Gemini provider, or use a mock in tests."
            )
        self.client = genai.Client(api_key=api_key)
        self.model_id = os.getenv("GEMINI_MODEL_ID", "gemini-2.5-flash")
        
    def ask_structured(self, prompt: str, response_schema: Type[T]) -> T:
        response = self.client.models.generate_content(
            model=self.model_id,
            contents=prompt,
            config={
                'response_mime_type': 'application/json',
                'response_schema': response_schema,
            },
        )
        return response_schema.model_validate_json(response.text)

class GroqProvider(AIProvider):
    def __init__(self):
        from groq import Groq
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise ValueError(
                "GROQ_API_KEY environment variable is required. "
                "Set it to use the Groq provider, or use a mock in tests."
            )
        self.client = Groq(api_key=api_key)
        self.model_id = os.getenv("GROQ_MODEL_ID", "openai/gpt-oss-120b")
        
    def ask_structured(self, prompt: str, response_schema: Type[T]) -> T:
        chat_completion = self.client.chat.completions.create(
            messages=[
                {"role": "user", "content": prompt}
            ],
            model=self.model_id,
            response_format={"type": "json_object"},
        )
        # Assuming the prompt enforces the schema correctly
        return response_schema.model_validate_json(chat_completion.choices[0].message.content)

class AnthropicProvider(AIProvider):
    def __init__(self):
        from anthropic import Anthropic
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY environment variable is required. "
                "Set it to use the Anthropic provider, or use a mock in tests."
            )
        self.client = Anthropic(api_key=api_key)
        self.model_id = os.getenv("ANTHROPIC_MODEL_ID", "claude-sonnet-5")
        
    def ask_structured(self, prompt: str, response_schema: Type[T]) -> T:
        full_prompt = (
            f"{prompt}\n\n"
            f"You MUST respond with ONLY a valid JSON object matching this schema. "
            f"Do not include markdown formatting like ```json or ANY other text.\n"
            f"Schema: {response_schema.model_json_schema()}"
        )
        
        message = self.client.messages.create(
            max_tokens=1024,
            messages=[
                {"role": "user", "content": full_prompt}
            ],
            model=self.model_id,
        )
        
        response_text = message.content[0].text.strip()
        if response_text.startswith("```json"):
            response_text = response_text[7:]
        if response_text.startswith("```"):
            response_text = response_text[3:]
        if response_text.endswith("```"):
            response_text = response_text[:-3]
            
        return response_schema.model_validate_json(response_text.strip())

class FallbackProvider(AIProvider):
    def __init__(self, primary: AIProvider, fallback: AIProvider):
        self.primary = primary
        self.fallback = fallback

    def _get_recoverable_exceptions(self):
        recoverable = []
        try:
            import groq
            recoverable.extend([
                groq.APIConnectionError,
                groq.APITimeoutError,
                groq.RateLimitError,
                groq.InternalServerError,
                groq.APIStatusError
            ])
        except ImportError:
            pass

        try:
            import google.genai.errors as genai_errors
            recoverable.extend([
                genai_errors.APIError
            ])
        except ImportError:
            pass
            
        try:
            import anthropic
            recoverable.extend([
                anthropic.APIConnectionError,
                anthropic.APITimeoutError,
                anthropic.RateLimitError,
                anthropic.InternalServerError,
                anthropic.APIStatusError
            ])
        except ImportError:
            pass

        # If SDKs aren't available, we don't have expected exceptions to fall back on,
        # but we must not blindly catch Exception. We return a dummy exception tuple.
        class _DummyException(Exception): pass
        return tuple(recoverable) if recoverable else (_DummyException,)

    def ask_structured(self, prompt: str, response_schema: Type[T]) -> T:
        try:
            return self.primary.ask_structured(prompt, response_schema)
        except Exception as e:
            # We ONLY fall back for API-related exceptions defined by the SDKs.
            recoverable_exceptions = self._get_recoverable_exceptions()
            
            if isinstance(e, recoverable_exceptions):
                logger.warning(f"Primary provider failed with {type(e).__name__}: {str(e)}. Falling back to secondary.")
                return self.fallback.ask_structured(prompt, response_schema)
            
            # If it's a ValidationError (bad JSON) or something unexpected, bubble it up!
            logger.error(f"Critical or unexpected error in AI provider ({type(e).__name__}): {str(e)}. No fallback triggered.")
            raise
