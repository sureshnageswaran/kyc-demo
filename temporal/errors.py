"""Activity error helpers. Type names are referenced by retry policies."""
from temporalio.exceptions import ApplicationError

INTEGRITY = "IntegrityError"            # conflicting identity/decision/hash
DOCUMENT_INTEGRITY = "DocumentIntegrityError"
POLICY_INVALID = "PolicyInvalid"
SUBMISSION_INVALID = "SubmissionInvalid"
CONFIGURATION = "ConfigurationError"    # bad URL/credentials: retrying won't help
ACTIVATION_REJECTED = "ActivationRejected"
SERVICE_UNAVAILABLE = "ServiceUnavailable"

NON_RETRYABLE_TYPES = [
    INTEGRITY, DOCUMENT_INTEGRITY, POLICY_INVALID, SUBMISSION_INVALID,
    CONFIGURATION, ACTIVATION_REJECTED,
]


def fatal(error_type: str, message: str) -> ApplicationError:
    return ApplicationError(message, type=error_type, non_retryable=True)


def transient(error_type: str, message: str) -> ApplicationError:
    return ApplicationError(message, type=error_type)
