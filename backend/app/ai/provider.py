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

    def ask_structured(self, prompt: str, response_schema: Type[T]) -> T:
        try:
            return self.primary.ask_structured(prompt, response_schema)
        except Exception as e:
            # We ONLY fall back for API-related exceptions.
            # We must NOT fall back for Pydantic ValidationErrors, TypeErrors, or internal bugs.
            exc_name = type(e).__name__
            
            # Common names for network/API errors across SDKs (Groq, Google)
            expected_api_failures = [
                "APIError", "APIConnectionError", "APITimeoutError", 
                "RateLimitError", "InternalServerError", "ServiceUnavailable",
                "ClientError", "ServerError"
            ]
            
            if exc_name in expected_api_failures:
                logger.warning(f"Primary provider failed with {exc_name}: {str(e)}. Falling back to secondary.")
                return self.fallback.ask_structured(prompt, response_schema)
            
            # If it's a ValidationError (bad JSON) or something unexpected, bubble it up!
            logger.error(f"Critical or unexpected error in AI provider ({exc_name}): {str(e)}. No fallback triggered.")
            raise
