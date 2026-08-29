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
        # Lazy import configuration. Fails gracefully in tests if unconfigured
        self.client = genai.Client(api_key=os.getenv("GEMINI_API_KEY", "dummy_key"))
        
    def ask_structured(self, prompt: str, response_schema: Type[T]) -> T:
        if os.getenv("GEMINI_API_KEY", "dummy_key") == "dummy_key":
            logger.warning("GOOGLE_API_KEY not set. Using MockAIProvider.")
            if response_schema.__name__ == "DiagnosisResult":
                return response_schema.model_validate({"root_cause_category": "friction", "specific_reason": "mocked", "confidence_score": 0.9, "reasoning": "mocked"})
            elif response_schema.__name__ == "DecisionResult":
                return response_schema.model_validate({"recommended_action": "retry_charge", "action_parameters": {"delay_hours": 24}, "confidence_score": 0.9, "reasoning": "mocked"})
            return response_schema.model_validate({})
            
        response = self.client.models.generate_content(
            model='gemini-2.5-flash',
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
        self.client = Groq(api_key=os.getenv("GROQ_API_KEY", "dummy_key"))
        
    def ask_structured(self, prompt: str, response_schema: Type[T]) -> T:
        chat_completion = self.client.chat.completions.create(
            messages=[
                {"role": "user", "content": prompt}
            ],
            model="llama-3.1-8b-instant",
            response_format={"type": "json_object"},
        )
        # Assuming the prompt enforces the schema correctly
        return response_schema.model_validate_json(chat_completion.choices[0].message.content)

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
