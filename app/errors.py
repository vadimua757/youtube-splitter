class AppError(Exception):
    """Помилка конвеєра з машинним кодом (VIDEO_UNAVAILABLE, SPLIT_FAILED, ...)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
