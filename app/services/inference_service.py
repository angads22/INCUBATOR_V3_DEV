class InferenceService:
    """
    Reserved integration point for future egg viability inference.

    Future endpoint target: `/api/viability/predict`
    """

    def health(self) -> dict:
        return {"enabled": False, "message": "Inference module not yet installed"}
