class AppError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(code)


class ProviderError(AppError):
    def __init__(self, code="provider_unavailable", retryable=True):
        self.retryable = retryable
        super().__init__(code, "The model service could not complete the request safely.", 503)
