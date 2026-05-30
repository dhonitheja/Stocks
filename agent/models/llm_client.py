"""
LLM Client — Multi-Provider AI Reasoning
==========================================
Supports Grok (xAI), Google Gemini, and OpenAI.
Grok uses the OpenAI-compatible SDK — just a different base_url and API key.

Set LLM_PROVIDER in .env to switch between providers:
  LLM_PROVIDER=grok    → xAI Grok (grok-3-mini recommended)
  LLM_PROVIDER=gemini  → Google Gemini (gemini-1.5-flash)
  LLM_PROVIDER=openai  → OpenAI (gpt-4o-mini)

Used by the DecisionEngine for final reasoning on ambiguous signals.
"""
import os
from typing import Optional
from loguru import logger


class LLMClient:
    """
    Unified LLM client with automatic provider selection.
    Falls back to next available provider if primary fails.
    """

    # Default models per provider (cost-optimized for frequent trading use)
    MODELS = {
        "grok":   "grok-3-mini",         # xAI — fast, cheap, finance-aware
        "gemini": "gemini-1.5-flash",    # Google — free tier, very capable
        "openai": "gpt-4o-mini",         # OpenAI — reliable fallback
    }

    # Grok uses the OpenAI SDK but with xAI's base URL
    BASE_URLS = {
        "grok":   "https://api.x.ai/v1",
        "openai": "https://api.openai.com/v1",
        # Gemini uses its own SDK, not OpenAI-compatible
    }

    def __init__(self):
        self.provider = os.getenv("LLM_PROVIDER", "grok").lower()
        self._client = None
        self._loaded_provider = None
        logger.info(f"LLMClient created | Provider: {self.provider}")

    def _load_client(self):
        """Lazy-load the LLM client on first use."""
        if self._client and self._loaded_provider == self.provider:
            return

        if self.provider in ("grok", "openai"):
            self._load_openai_compatible()
        elif self.provider == "gemini":
            self._load_gemini()
        else:
            logger.warning(f"Unknown LLM provider '{self.provider}', defaulting to grok")
            self.provider = "grok"
            self._load_openai_compatible()

    def _load_openai_compatible(self):
        """Load OpenAI-compatible client (works for both Grok and OpenAI)."""
        try:
            from openai import OpenAI

            if self.provider == "grok":
                api_key = os.getenv("XAI_API_KEY")
                if not api_key:
                    raise ValueError("XAI_API_KEY not set in .env")
                self._client = OpenAI(
                    api_key=api_key,
                    base_url=self.BASE_URLS["grok"],
                )
                logger.info("Grok (xAI) LLM client loaded via OpenAI-compatible SDK")

            else:  # openai
                api_key = os.getenv("OPENAI_API_KEY")
                if not api_key:
                    raise ValueError("OPENAI_API_KEY not set in .env")
                self._client = OpenAI(api_key=api_key)
                logger.info("OpenAI LLM client loaded")

            self._loaded_provider = self.provider

        except ImportError:
            logger.error("openai package not installed. Run: pip install openai")
            raise
        except ValueError as e:
            logger.error(f"LLM config error: {e}")
            raise

    def _load_gemini(self):
        """Load Google Gemini client."""
        try:
            import google.generativeai as genai

            api_key = os.getenv("GOOGLE_API_KEY")
            if not api_key:
                raise ValueError("GOOGLE_API_KEY not set in .env")
            genai.configure(api_key=api_key)
            self._client = genai.GenerativeModel(self.MODELS["gemini"])
            self._loaded_provider = "gemini"
            logger.info("Google Gemini LLM client loaded")

        except ImportError:
            logger.error("google-generativeai not installed. Run: pip install google-generativeai")
            raise

    def reason(self, prompt: str, max_tokens: int = 200) -> str:
        """
        Send a prompt and get back a reasoning response.
        
        Args:
            prompt: The analysis prompt (e.g., "Should I buy AAPL given...")
            max_tokens: Max response length (keep short for speed + cost)
        
        Returns:
            Plain text reasoning from the LLM
        """
        self._load_client()

        try:
            if self._loaded_provider in ("grok", "openai"):
                return self._reason_openai_compat(prompt, max_tokens)
            elif self._loaded_provider == "gemini":
                return self._reason_gemini(prompt, max_tokens)
        except Exception as e:
            logger.warning(f"LLM reasoning failed ({self.provider}): {e}")
            return ""

    def _reason_openai_compat(self, prompt: str, max_tokens: int) -> str:
        """Call Grok or OpenAI with the OpenAI SDK."""
        model = self.MODELS.get(self._loaded_provider, "grok-3-mini")
        response = self._client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a 30-year veteran Wall Street trader. "
                        "Give concise, confident trade reasoning in 1-2 sentences. "
                        "Be direct: state BUY, SELL, or HOLD and why. "
                        "Never hedge. Treat this as your real money on the line."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            max_tokens=max_tokens,
            temperature=0.3,  # Low temperature = consistent, decisive responses
        )
        return response.choices[0].message.content.strip()

    def _reason_gemini(self, prompt: str, max_tokens: int) -> str:
        """Call Google Gemini."""
        system = (
            "You are a 30-year veteran Wall Street trader. "
            "Give concise, confident trade reasoning in 1-2 sentences. "
            "Be direct: BUY, SELL, or HOLD and why. Never hedge."
        )
        response = self._client.generate_content(
            f"{system}\n\n{prompt}",
            generation_config={"max_output_tokens": max_tokens, "temperature": 0.3},
        )
        return response.text.strip()

    def build_trading_prompt(
        self,
        symbol: str,
        price: float,
        tech_signal: str,
        tech_confidence: float,
        sentiment_score: float,
        sentiment_label: str,
        key_news: list,
        rsi: float,
        trend: str,
    ) -> str:
        """
        Build a structured trading analysis prompt for the LLM.
        Called when technical + sentiment signals are mixed or borderline.
        """
        news_str = "\n".join(f"  - {n}" for n in key_news[:3]) if key_news else "  - No significant news"

        return f"""
Stock: {symbol} | Price: ${price:.2f}

Technical Analysis:
  Signal: {tech_signal} (confidence: {tech_confidence:.0%})
  Trend: {trend}
  RSI: {rsi:.1f}

News Sentiment:
  Score: {sentiment_score:+.2f} ({sentiment_label})
  Key Headlines:
{news_str}

Based on all signals above, what is your final trading recommendation for {symbol}?
Respond with exactly: BUY | SELL | HOLD — followed by one sentence of reasoning.
""".strip()

    def is_available(self) -> bool:
        """Check if the LLM provider key is configured."""
        key_map = {
            "grok":   "XAI_API_KEY",
            "gemini": "GOOGLE_API_KEY",
            "openai": "OPENAI_API_KEY",
        }
        key_name = key_map.get(self.provider, "XAI_API_KEY")
        return bool(os.getenv(key_name))

    @property
    def provider_name(self) -> str:
        """Human-readable provider name."""
        return {
            "grok":   "Grok (xAI grok-3-mini)",
            "gemini": "Google Gemini 1.5 Flash",
            "openai": "OpenAI GPT-4o Mini",
        }.get(self.provider, self.provider)
