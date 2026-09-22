from deepl import TextResult, Translator


class DeeplTranslationService:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def translate(self, source_text: str, target_language: str,
                  context: str | None = None) -> TextResult | list[TextResult]:
        translator = Translator(self.api_key)
        # Only sent when there is something to send: an empty context is not
        # the same request as no context, and the first sentence has none.
        extra = {'context': context} if context else {}
        result: TextResult | list[TextResult] = translator.translate_text(
            source_text, target_lang=target_language, **extra)
        return result
