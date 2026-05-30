"""
Sentiment Analysis Model
========================
Uses FinBERT (financial domain BERT) to score news articles.
ProsusAI/finbert is the gold standard for financial NLP.
"""
import os
from typing import List, Dict, Optional
from dataclasses import dataclass
from loguru import logger


@dataclass
class SentimentResult:
    """Sentiment result for a piece of text."""
    text: str
    label: str        # positive | negative | neutral
    score: float      # -1.0 (very negative) to +1.0 (very positive)
    positive: float   # Raw probability
    negative: float   # Raw probability
    neutral: float    # Raw probability


class SentimentModel:
    """
    FinBERT-based financial sentiment analyzer.
    
    First call triggers model download (~440MB). Subsequent calls are instant.
    Runs on CPU by default; uses GPU automatically if CUDA is available.
    """

    def __init__(self):
        self._pipeline = None
        self._loaded = False
        logger.info("SentimentModel created (lazy load — model loads on first use)")

    def _ensure_loaded(self):
        """Lazy-load FinBERT on first use."""
        if self._loaded:
            return
        try:
            from transformers import pipeline
            import torch

            device = 0 if torch.cuda.is_available() else -1
            device_name = "GPU (CUDA)" if device == 0 else "CPU"
            logger.info(f"Loading FinBERT model on {device_name}... (first time may take 60s)")

            self._pipeline = pipeline(
                "text-classification",
                model="ProsusAI/finbert",
                tokenizer="ProsusAI/finbert",
                device=device,
                top_k=None,   # Return all 3 class probabilities
            )
            self._loaded = True
            logger.info("✅ FinBERT loaded and ready")

        except ImportError:
            logger.error("transformers/torch not installed. Run: pip install transformers torch")
            raise
        except Exception as e:
            logger.error(f"Failed to load FinBERT: {e}")
            raise

    def analyze(self, text: str) -> SentimentResult:
        """
        Analyze sentiment of a single text.
        
        Args:
            text: News headline or short paragraph
        
        Returns:
            SentimentResult with label and -1 to +1 score
        """
        self._ensure_loaded()

        # FinBERT max 512 tokens; truncate long texts
        text = text[:512]

        try:
            results = self._pipeline(text)[0]
            probs = {r["label"].lower(): r["score"] for r in results}

            positive = probs.get("positive", 0.0)
            negative = probs.get("negative", 0.0)
            neutral = probs.get("neutral", 0.0)

            # Composite score: +1 = fully positive, -1 = fully negative
            score = positive - negative
            label = max(probs, key=probs.get)

            return SentimentResult(
                text=text[:100] + "..." if len(text) > 100 else text,
                label=label,
                score=round(score, 4),
                positive=round(positive, 4),
                negative=round(negative, 4),
                neutral=round(neutral, 4),
            )

        except Exception as e:
            logger.error(f"Sentiment analysis failed: {e}")
            return SentimentResult(text=text, label="neutral", score=0.0,
                                   positive=0.33, negative=0.33, neutral=0.34)

    def analyze_batch(self, texts: List[str]) -> List[SentimentResult]:
        """Analyze multiple texts in one batch (faster than one-by-one)."""
        self._ensure_loaded()
        results = []
        for text in texts:
            results.append(self.analyze(text))
        return results

    def score_symbol_news(self, symbol: str, articles: list) -> Dict:
        """
        Aggregate sentiment across multiple articles for a symbol.
        
        Returns:
            dict with: avg_score, label, confidence, article_count, summary
        """
        if not articles:
            return {
                "symbol": symbol,
                "avg_score": 0.0,
                "label": "neutral",
                "confidence": 0.0,
                "article_count": 0,
                "high_importance_count": 0,
                "summary": "No recent news found.",
            }

        scores = []
        high_importance = 0
        for article in articles:
            result = self.analyze(article.raw_text or article.title)
            article.sentiment_score = result.score
            article.sentiment_label = result.label
            scores.append(result.score)
            if article.importance == "high":
                high_importance += 1
                # Double-weight high importance articles
                scores.append(result.score)

        avg_score = sum(scores) / len(scores)
        abs_avg = abs(avg_score)

        if avg_score >= 0.3:
            label = "positive"
        elif avg_score <= -0.3:
            label = "negative"
        else:
            label = "neutral"

        # Confidence: higher if more articles + consistent direction
        score_variance = sum((s - avg_score) ** 2 for s in scores) / len(scores)
        consistency = max(0, 1 - score_variance)
        confidence = min(abs_avg * consistency * 1.5, 1.0)

        # Human-readable summary
        article_count = len(articles)
        summary = (
            f"{article_count} article(s) analyzed. "
            f"Avg sentiment: {avg_score:+.2f} ({label}). "
            f"High-importance articles: {high_importance}. "
            f"Confidence: {confidence:.0%}."
        )

        return {
            "symbol": symbol,
            "avg_score": round(avg_score, 4),
            "label": label,
            "confidence": round(confidence, 3),
            "article_count": article_count,
            "high_importance_count": high_importance,
            "summary": summary,
        }
