"""Temporal Activities (spec sections 4, 5, 18).

Activities own all I/O: Garage, the OCR/screening/account stubs, and the
business Postgres. Workflow code does none of it. Each Activity is a mixin in
its own module; `KycActivities` combines them.
"""
from .activate_account import ActivateAccount
from .base import ActivityBase
from .document_parse import DocumentParse
from .evaluate_application import EvaluateApplication
from .load_policy import LoadPolicy
from .load_submission import LoadSubmission
from .persist_activation import PersistActivation
from .persist_assessment import PersistAssessment
from .persist_final_outcome import PersistFinalOutcome
from .record_event import RecordEvent
from .screen_applicant import ScreenApplicant
from .start_processing import StartProcessing


class KycActivities(
    StartProcessing, RecordEvent, LoadSubmission, DocumentParse, LoadPolicy, EvaluateApplication,
    ScreenApplicant, PersistAssessment, PersistFinalOutcome, ActivateAccount, PersistActivation,
    ActivityBase,
):
    def all_activities(self) -> list:
        return [
            self.app_start_processing, self.app_record_event, self.app_load_submission,
            self.app_document_parse, self.app_load_policy, self.app_evaluate_application,
            self.app_screen_applicant, self.app_persist_assessment, self.app_persist_final_outcome,
            self.app_activate_account, self.app_persist_activation,
        ]
