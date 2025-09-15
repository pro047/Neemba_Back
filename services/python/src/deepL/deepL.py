from deepl import TextResult, Translator


class DeeplTranslationService:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def translate(self, source_text: str, target_language: str) -> TextResult | list[TextResult]:
        translator = Translator(self.api_key)
        result: TextResult | list[TextResult] = translator.translate_text(
            source_text, target_lang=target_language)
        return result
