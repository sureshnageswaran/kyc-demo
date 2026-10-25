"""KycApplicationWorkflow (spec sections 3, 4, 6, 8, 14-18).

Deterministic orchestration only: all I/O happens in Activities. Business
deadlines are workflow timers on workflow time; their constants come from the
versioned profile named in workflow input, never from worker environment.
"""
import asyncio
import dataclasses
import json
from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, SearchAttributeKey
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from .. import kyc_logic
    from ..errors import ACTIVATION_REJECTED, NON_RETRYABLE_TYPES
    from ..models import (
        ActivateAccountInput,
        ActivationReceipt,
        AssessmentPersisted,
        AuditEvent,
        DocumentOutcome,
        EvaluateApplicationInput,
        EvaluationDocument,
        EvaluationResult,
        FinalOutcomeInput,
        KycResult,
        KycWorkflowInput,
        LoadedSubmission,
        LoadSubmissionInput,
        ParseDocumentInput,
        PersistActivationInput,
        PersistAssessmentInput,
        PolicySnapshot,
        ProcessingState,
        Projection,
        ReevaluationRequest,
        RecordEventInput,
        RetryRequest,
        ReviewDecisionRequest,
        ReviewMilestone,
        ReviewTaskSpec,
        ScreenInput,
        ScreeningResult,
        StartProcessingInput,
        UpdateOutcome,
    )
    from .timers import DEFAULT_PROFILE, PROFILES, SENIOR_QUEUE, TimerProfile

# (start-to-close seconds, maximum attempts) - spec section 11.
ACTIVITY_SETTINGS: dict[str, tuple[int, int]] = {
    "app_start_processing": (10, 5),
    "app_load_submission": (15, 5),
    "app_document_parse": (60, 5),
    "app_evaluate_application": (20, 5),  # includes the policy read from Garage
    "app_screen_applicant": (30, 5),
    "app_persist_assessment": (10, 5),
    "app_persist_final_outcome": (10, 5),
    "app_activate_account": (30, 5),
    "app_persist_activation": (10, 5),
    "app_record_event": (10, 5),
}

DECISION_ROLES = {"REVIEWER", "SENIOR_REVIEWER"}
OPERATOR_ROLES = {"OPERATOR", "ADMIN"}
PERSISTENCE_RETRY_PAUSE = timedelta(seconds=30)

SA_APPLICATION_ID = SearchAttributeKey.for_keyword("KycApplicationId")
SA_STATUS = SearchAttributeKey.for_keyword("KycBusinessStatus")
SA_STAGE = SearchAttributeKey.for_keyword("KycStage")
SA_WAITING = SearchAttributeKey.for_keyword("KycWaitingReason")
SA_POLICY_VERSION = SearchAttributeKey.for_int("KycPolicyVersionInt")
SA_REVIEW_DEADLINE = SearchAttributeKey.for_datetime("KycReviewDeadlineDate")


class RecoveryExpired(Exception):
    """The operator-recovery wait ran out; the workflow ends unsuccessfully."""

    def __init__(self, status: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _fingerprint(req) -> str:
    return json.dumps(dataclasses.asdict(req), sort_keys=True, default=str)


def _failure_info(exc: ActivityError) -> tuple[str, str, bool]:
    """(error code, message, non_retryable) of the root cause."""
    cause = exc.cause
    while cause is not None and not isinstance(cause, ApplicationError) and getattr(cause, "cause", None):
        cause = cause.cause
    if isinstance(cause, ApplicationError):
        return (cause.type or "ActivityFailure")[:100], str(cause.message)[:500], cause.non_retryable
    return type(cause).__name__[:100] if cause else "ActivityFailure", str(cause or exc)[:500], False


@workflow.defn(name="KycApplicationWorkflow")
class KycApplicationWorkflow:
    def __init__(self) -> None:
        self._seq = 0
        self._lock = asyncio.Lock()
        self._requests: dict[str, tuple[str, UpdateOutcome]] = {}
        self._loaded: LoadedSubmission | None = None
        self._outcomes: dict[str, DocumentOutcome] = {}
        self._policy: PolicySnapshot | None = None
        self._seen_current: tuple[int, str] | None = None  # (version, sha256) of the last current policy read
        self._eval: EvaluationResult | None = None
        self._screening: ScreeningResult | None = None
        self._review: ReviewTaskSpec | None = None
        self._decision: dict | None = None
        self._recovery: dict | None = None
        self._retry_req: RetryRequest | None = None
        self._stage_open: str | None = None
        self._st: ProcessingState | None = None

    # ------------------------------------------------------------------ run

    @workflow.run
    async def run(self, inp: KycWorkflowInput) -> KycResult:
        info = workflow.info()
        self.inp = inp
        self.app_id = inp.application_id
        self.wf_id = info.workflow_id
        self.run_id = info.run_id
        self.profile: TimerProfile = PROFILES[inp.timer_profile or DEFAULT_PROFILE]
        now = workflow.now().isoformat()
        self._st = ProcessingState(
            application_id=self.app_id, workflow_id=self.wf_id, run_id=self.run_id,
            business_status="PROCESSING", current_stage="REGISTERED", stage_started_at=now,
            timer_profile=self.profile.name)
        try:
            result = await self._main()
        except RecoveryExpired as exc:
            result = KycResult(application_id=self.app_id, status=exc.status, detail=exc.detail)
        # Accepted state-changing handlers must finish before the workflow exits.
        await workflow.wait_condition(workflow.all_handlers_finished)
        return result

    async def _main(self) -> KycResult:
        inp = self.inp
        # 1. Registration: nothing else starts until it succeeds.
        await self._persist(
            "app_start_processing",
            StartProcessingInput(
                application_id=self.app_id, submission_id=inp.submission_id, submission_ref=inp.submission_ref,
                workflow_id=self.wf_id, run_id=self.run_id,
                initial_run_id=workflow.info().first_execution_run_id or self.run_id,
                processing_started_at=workflow.now().isoformat(),
                event=self._event("PROCESSING_STARTED", self.app_id,
                                  details={"timer_profile": self.profile.name,
                                           "submission_sha256": inp.submission_ref.sha256})),
            "start")
        self._sync_search_attributes()

        # 2. Frozen submission manifest.
        await self._enter_stage("LOADING_SUBMISSION")
        self._loaded = await self._service_stage(
            "LOADING_SUBMISSION", "app_load_submission",
            lambda gen: LoadSubmissionInput(self.app_id, inp.submission_id, inp.submission_ref),
            LoadedSubmission, schedule_prefix="load-submission")
        self._st.documents_total = len(self._loaded.documents)
        self._st.documents_pending = len(self._loaded.documents)

        # 3. Document fan-out / fan-in.
        await self._enter_stage("DOCUMENT_PARSING")
        await self._parse_documents()

        # 4. Screening first, so the policy is resolved as late as possible.
        await self._enter_stage("SCREENING")
        self._screening = await self._service_stage(
            "SCREENING", "app_screen_applicant",
            lambda gen: ScreenInput(self.app_id, self._loaded.client_reference,
                                    self._loaded.declared_applicant,
                                    invocation_id=f"{self.run_id}:screen-g{self._gen}",
                                    stub_metadata=(self._loaded.metadata_packet or {}).get("screening")),
            ScreeningResult, schedule_prefix="screen")

        # 5. One Activity loads the current policy, evaluates, and decides whether review is required.
        await self._enter_stage("EVALUATION")
        eval_id = str(workflow.uuid4())
        evaluation = await self._evaluate(eval_id, 1)
        self._policy, self._eval = evaluation.policy, evaluation
        self._seen_current = (evaluation.policy.version, evaluation.policy.sha256)
        await self._record_event(
            [self._event("POLICY_RESOLVED", eval_id, details=self._policy_details(self._policy, eval_id))])
        self._st.policy_id, self._st.policy_version = self._policy.policy_id, self._policy.version
        self._st.policy_sha256 = self._policy.sha256
        self._sync_search_attributes()

        # 6. Disposition: persist the assessment (and review task) first.
        decision = await self._disposition(evaluation, evaluation.review_required)
        if decision is None:
            return KycResult(application_id=self.app_id, status="REVIEW_EXPIRED",
                             detail="review expired without a decision")
        return await self._finalize(decision)

    # ------------------------------------------------------------ helpers

    @property
    def _gen(self) -> int:
        return self._st.recovery_generation

    def _event(self, event_type: str, discriminator: str, *, actor_type: str = "WORKFLOW",
               actor_id: str = "kyc-workflow", reason: str | None = None, request_id: str | None = None,
               details: dict | None = None) -> AuditEvent:
        """Keys and sequence come from workflow state, so Activity retries reuse them."""
        self._seq += 1
        return AuditEvent(
            event_key=f"{event_type}:{discriminator}", event_sequence=self._seq, event_type=event_type,
            occurred_at=workflow.now().isoformat(), actor_type=actor_type, actor_id=actor_id,
            reason=reason, request_id=request_id, details=details or {})

    @staticmethod
    def _policy_details(p: PolicySnapshot, evaluation_id: str) -> dict:
        return {"evaluation_id": evaluation_id, "policy_id": p.policy_id, "policy_version": p.version,
                "policy_sha256": p.sha256}

    async def _exec(self, name: str, arg, result_type, activity_id: str, *,
                    schedule_to_close: timedelta | None = None):
        start_to_close, attempts = ACTIVITY_SETTINGS[name]
        return await workflow.execute_activity(
            name, arg, result_type=result_type, activity_id=activity_id,
            start_to_close_timeout=timedelta(seconds=start_to_close),
            schedule_to_close_timeout=schedule_to_close,
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1), backoff_coefficient=2.0,
                maximum_interval=timedelta(seconds=30), maximum_attempts=attempts,
                non_retryable_error_types=NON_RETRYABLE_TYPES))

    async def _persist(self, name: str, arg, key: str, result_type=None):
        """Persistence is an audit boundary: block visibly and retry, never skip.

        Integrity conflicts (non-retryable) fail the workflow. Anything else
        keeps retrying with a fresh scheduling ID and a visible waiting reason.
        """
        round_ = 0
        try:
            while True:
                try:
                    return await self._exec(name, arg, result_type, f"{key}-g{self._gen}-r{round_}")
                except ActivityError as exc:
                    code, msg, fatal = _failure_info(exc)
                    if fatal:
                        raise
                    round_ += 1
                    self._st.waiting_reason = "AUDIT_PERSISTENCE_UNAVAILABLE"
                    self._st.last_error_code = code
                    self._sync_search_attributes()
                    await workflow.sleep(PERSISTENCE_RETRY_PAUSE)
        finally:
            if self._st.waiting_reason == "AUDIT_PERSISTENCE_UNAVAILABLE":
                self._st.waiting_reason = None
                self._sync_search_attributes()

    async def _record_event(self, events: list[AuditEvent], set_fields: dict | None = None,
                            milestone: ReviewMilestone | None = None) -> None:
        await self._persist(
            "app_record_event",
            RecordEventInput(self.app_id, self.wf_id, self.run_id, events,
                             Projection(set_fields or {}), milestone),
            f"record-{events[0].event_key}")

    def _sync_search_attributes(self) -> None:
        st = self._st
        updates = [SA_APPLICATION_ID.value_set(self.app_id), SA_STATUS.value_set(st.business_status),
                   SA_STAGE.value_set(st.current_stage)]
        updates.append(SA_WAITING.value_set(st.waiting_reason) if st.waiting_reason else SA_WAITING.value_unset())
        if st.policy_version is not None:
            updates.append(SA_POLICY_VERSION.value_set(st.policy_version))
        if st.review_deadline_at and st.business_status == "AWAITING_REVIEW":
            updates.append(SA_REVIEW_DEADLINE.value_set(_parse(st.review_deadline_at)))
        else:
            updates.append(SA_REVIEW_DEADLINE.value_unset())
        workflow.upsert_search_attributes(updates)

    async def _enter_stage(self, stage: str, *, status: str = "PROCESSING",
                           waiting_reason: str | None = None, extra: dict | None = None) -> None:
        """Persist STAGE_COMPLETED/STAGE_STARTED and the projection before work begins."""
        now = workflow.now().isoformat()
        events = []
        if self._stage_open and self._stage_open != stage:
            events.append(self._event("STAGE_COMPLETED", f"{self._stage_open}:g{self._gen}"))
        events.append(self._event("STAGE_STARTED", f"{stage}:g{self._gen}"))
        await self._record_event(events, {
            "status": status, "current_stage": stage, "waiting_reason": waiting_reason,
            "stage_started_at": now, "last_error_code": None, "last_error_message": None,
            **(extra or {})})
        st = self._st
        st.business_status, st.current_stage, st.stage_started_at = status, stage, now
        st.waiting_reason, st.failed_stage, st.last_error_code = waiting_reason, None, None
        self._stage_open = stage
        self._sync_search_attributes()

    async def _service_stage(self, stage: str, activity_name: str, make_input, result_type, *,
                             schedule_prefix: str):
        """Run a provider-facing Activity; on exhaustion wait for operator recovery."""
        while True:
            try:
                return await self._exec(activity_name, make_input(self._gen), result_type,
                                        f"{schedule_prefix}-g{self._gen}")
            except ActivityError as exc:
                code, msg, _ = _failure_info(exc)
                await self._enter_recovery(stage, code, msg)

    # --------------------------------------------------- recovery (section 16)

    def _error_partial(self, stage: str, code: str, msg: str) -> PersistAssessmentInput:
        loaded = dataclasses.replace(self._loaded, documents=[]) if self._loaded else None
        return PersistAssessmentInput(
            application_id=self.app_id, workflow_id=self.wf_id, run_id=self.run_id,
            target_status="PROCESSING_ERROR", current_stage=stage, waiting_reason="OPERATOR_RECOVERY",
            loaded=loaded, documents=self._loaded.documents if self._loaded else [],
            document_outcomes=list(self._outcomes.values()),
            screening=(self._screening or ScreeningResult(status="NOT_RUN")) if self._loaded else None,
            error_code=code, error_message=msg,
            events=[self._event("PROCESSING_ERROR", f"{stage}:r{self._gen}", reason=msg,
                                details={"stage": stage, "error_code": code, "recovery_generation": self._gen})])

    async def _enter_recovery(self, stage: str, code: str, msg: str) -> None:
        """Persist partial results, then wait durably for retry_failed_stage."""
        st = self._st
        activation = stage == "ACTIVATION"
        if not activation:
            await self._persist("app_persist_assessment", self._error_partial(stage, code, msg),
                                f"persist-error-{stage}", AssessmentPersisted)
        st.business_status = "ACTIVATION_ERROR" if activation else "PROCESSING_ERROR"
        st.current_stage, st.failed_stage = stage, stage
        st.stage_started_at = workflow.now().isoformat()
        st.waiting_reason, st.last_error_code = "OPERATOR_RECOVERY", code
        deadline = workflow.now() + timedelta(seconds=self.profile.recovery_expiry_s)
        if self._review and self._review.expires_at:
            deadline = min(deadline, _parse(self._review.expires_at))
        st.recovery_deadline_at = deadline.isoformat()
        self._recovery = {"stage": stage, "generation": st.recovery_generation, "deadline": deadline}
        self._retry_req = None
        self._sync_search_attributes()

        remaining = (deadline - workflow.now()).total_seconds()
        try:
            await workflow.wait_condition(lambda: self._retry_req is not None, timeout=max(remaining, 0.001))
        except asyncio.TimeoutError:
            pass
        if self._retry_req is None:
            self._recovery = None
            await self._record_event([self._event(
                "RECOVERY_WAIT_EXPIRED", f"{stage}:r{st.recovery_generation}",
                details={"stage": stage, "error_code": code})],
                {"waiting_reason": None})
            st.waiting_reason = None
            self._sync_search_attributes()
            raise RecoveryExpired("ACTIVATION_ERROR" if activation else "PROCESSING_ERROR",
                                  f"recovery wait for {stage} expired after {code}")
        # The Update handler already incremented recovery_generation and audited the request.
        self._recovery = None
        st.recovery_deadline_at = None
        st.waiting_reason = None

    # ------------------------------------------------- document fan-out/fan-in

    async def _parse_documents(self) -> None:
        refs = {r.document_id: r for r in self._loaded.documents}
        while True:
            pending = [refs[d] for d in sorted(refs)
                       if d not in self._outcomes or self._outcomes[d].status == "TECHNICAL_ERROR"]
            if not pending:
                break
            gen = self._gen
            semaphore = asyncio.Semaphore(self.profile.document_concurrency)

            async def parse_one(ref) -> DocumentOutcome:
                async with semaphore:
                    aid = f"parse-{ref.document_id}-g{gen}"
                    try:
                        return await self._exec(
                            "app_document_parse", ParseDocumentInput(ref, f"{self.run_id}:{aid}"),
                            DocumentOutcome, aid)
                    except ActivityError as exc:  # isolate: never cancel siblings
                        code, msg, _ = _failure_info(exc)
                        return DocumentOutcome(
                            document_id=ref.document_id, status="TECHNICAL_ERROR", error_code=code,
                            error_message=msg, attempts=ACTIVITY_SETTINGS["app_document_parse"][1])

            results = await asyncio.gather(*(parse_one(r) for r in pending))
            events = []
            for out in results:
                self._outcomes[out.document_id] = out
                kind = "DOCUMENT_PARSED" if out.status == "SUCCEEDED" else "DOCUMENT_ERROR"
                events.append(self._event(kind, f"{out.document_id}:g{gen}", details={
                    "document_id": out.document_id, "status": out.status, "error_code": out.error_code,
                    "attempts": out.attempts}))
            self._refresh_document_counts()
            await self._record_event(events)

            failed = [o for o in results if o.status == "TECHNICAL_ERROR"]
            if failed:
                await self._enter_recovery("DOCUMENT_PARSING", failed[0].error_code or "TECHNICAL_ERROR",
                                           f"{len(failed)} document(s) failed technically")

    def _refresh_document_counts(self) -> None:
        st = self._st
        counts = {"SUCCEEDED": 0, "DOCUMENT_ERROR": 0, "TECHNICAL_ERROR": 0}
        for o in self._outcomes.values():
            counts[o.status] = counts.get(o.status, 0) + 1
        st.documents_succeeded, st.documents_document_error = counts["SUCCEEDED"], counts["DOCUMENT_ERROR"]
        st.documents_technical_error = counts["TECHNICAL_ERROR"]
        st.documents_pending = st.documents_total - len(self._outcomes)
        st.failed_document_ids = sorted(o.document_id for o in self._outcomes.values()
                                        if o.status in ("DOCUMENT_ERROR", "TECHNICAL_ERROR"))

    # ------------------------------------------------------------- evaluation

    def _evaluation_documents(self) -> list[EvaluationDocument]:
        out = []
        for ref in self._loaded.documents:
            o = self._outcomes.get(ref.document_id)
            out.append(EvaluationDocument(ref.document_id, ref.document_type, o.status if o else "NOT_RUN",
                                          o.extracted_fields if o else None))
        return out

    def _evaluate_input(self, evaluation_id: str, sequence: int, mode: str = "current",
                        version: int | None = None, sha256: str | None = None) -> EvaluateApplicationInput:
        return EvaluateApplicationInput(
            evaluation_id=evaluation_id, evaluation_sequence=sequence,
            declared_applicant=self._loaded.declared_applicant, documents=self._evaluation_documents(),
            evaluated_at=workflow.now().isoformat(), policy_id=self.inp.policy_id, policy_mode=mode,
            policy_version=version, policy_sha256=sha256, screening=self._screening)

    async def _evaluate(self, evaluation_id: str, sequence: int) -> EvaluationResult:
        evaluation = await self._service_stage(
            "EVALUATION", "app_evaluate_application",
            lambda gen: self._evaluate_input(evaluation_id, sequence),
            EvaluationResult, schedule_prefix=f"evaluate-{evaluation_id[:8]}")
        self._st.evaluation_id = evaluation_id
        return evaluation

    # ----------------------------------------------------------- disposition

    def _assessment_input(self, evaluation: EvaluationResult, *, status: str, stage: str,
                          events: list[AuditEvent], review: ReviewTaskSpec | None,
                          supersede: str | None = None, include_loaded: bool = True) -> PersistAssessmentInput:
        return PersistAssessmentInput(
            application_id=self.app_id, workflow_id=self.wf_id, run_id=self.run_id,
            target_status=status, current_stage=stage,
            waiting_reason="AWAITING_REVIEW" if status == "AWAITING_REVIEW" else None,
            loaded=dataclasses.replace(self._loaded, documents=[]) if include_loaded else None,
            documents=self._loaded.documents if include_loaded else [],
            document_outcomes=list(self._outcomes.values()) if include_loaded else [],
            screening=self._screening, evaluation=evaluation, review=review,
            supersede_review_task_id=supersede,
            review_deadline_at=review.expires_at if review else None, events=events)

    def _new_review_spec(self, evaluation: EvaluationResult) -> ReviewTaskSpec:
        opened = workflow.now()
        p = self.profile
        return ReviewTaskSpec(
            review_task_id=str(workflow.uuid4()), evaluation_id=evaluation.evaluation_id,
            review_key=f"review-{evaluation.evaluation_sequence}",
            reason=kyc_logic.review_reason(evaluation.findings), opened_at=opened.isoformat(),
            reminder_due_at=(opened + timedelta(seconds=p.review_reminder_s)).isoformat(),
            escalation_due_at=(opened + timedelta(seconds=p.review_escalation_s)).isoformat(),
            expires_at=(opened + timedelta(seconds=p.review_expiry_s)).isoformat())

    async def _disposition(self, evaluation: EvaluationResult, review_required: bool) -> dict | None:
        review = self._new_review_spec(evaluation) if review_required else None
        eid = evaluation.evaluation_id
        events = [
            self._event("EVALUATION_COMPLETED", eid, details={
                **self._policy_details(evaluation.policy, eid), "outcome": evaluation.evaluation_outcome,
                "rule_ids": [r["rule_id"] for r in evaluation.rule_results]}),
            self._event("ASSESSMENT_PERSISTED", eid, details={
                "evaluation_id": eid, "review_required": review_required,
                "finding_count": len(evaluation.findings)}),
        ]
        if review:
            events.append(self._event("REVIEW_OPENED", review.review_task_id, details={
                "review_task_id": review.review_task_id, "evaluation_id": eid, "reason": review.reason,
                "expires_at": review.expires_at}))
        await self._persist(
            "app_persist_assessment",
            self._assessment_input(
                evaluation, events=events, review=review,
                status="AWAITING_REVIEW" if review else "PROCESSING",
                stage="HUMAN_REVIEW" if review else "FINALIZING"),
            "persist-assessment", AssessmentPersisted)
        st = self._st
        if not review:
            st.current_stage, st.business_status = "FINALIZING", "PROCESSING"
            self._sync_search_attributes()
            return {"decision": "APPROVED", "source": "AUTOMATIC", "evaluation_id": eid,
                    "review_task_id": None, "reason_code": "CLEAN_AUTOMATIC_APPROVAL",
                    "decided_by": "kyc-workflow", "decided_at": workflow.now().isoformat()}
        self._review = review
        st.business_status, st.current_stage = "AWAITING_REVIEW", "HUMAN_REVIEW"
        st.waiting_reason, st.stage_started_at = "AWAITING_REVIEW", workflow.now().isoformat()
        st.review_task_id, st.review_queue = review.review_task_id, "compliance"
        st.review_deadline_at = review.expires_at
        self._stage_open = "HUMAN_REVIEW"
        self._sync_search_attributes()
        return await self._review_loop()

    # ----------------------------------------------- review timers (section 14)

    def _review_open(self) -> bool:
        r = self._review
        return bool(r and self._decision is None and workflow.now() < _parse(r.expires_at)
                    and self._st.business_status == "AWAITING_REVIEW")

    async def _review_milestone(self, kind: str, *, historical: bool = False) -> None:
        r = self._review
        if kind == "REMINDER":
            at = r.reminder_due_at if historical else workflow.now().isoformat()
            event = self._event("REVIEW_REMINDER_DUE", r.review_task_id,
                                details={"review_task_id": r.review_task_id, "historical": historical})
            r.reminded_at = at
            self._st.review_reminded = True
        else:
            at = workflow.now().isoformat()
            event = self._event("REVIEW_ESCALATED", r.review_task_id, details={
                "review_task_id": r.review_task_id, "queue": SENIOR_QUEUE})
            r.escalated_at, r.status = at, "ESCALATED"
            self._st.review_escalated, self._st.review_queue = True, SENIOR_QUEUE
        await self._record_event([event], milestone=ReviewMilestone(
            r.review_task_id, kind, at, SENIOR_QUEUE if kind == "ESCALATION" else None, historical))

    async def _expire_review(self) -> None:
        r = self._review
        await self._record_event(
            [self._event("REVIEW_EXPIRED", r.review_task_id, details={
                "review_task_id": r.review_task_id, "expires_at": r.expires_at})],
            milestone=ReviewMilestone(r.review_task_id, "EXPIRY", r.expires_at))
        st = self._st
        st.business_status, st.current_stage, st.waiting_reason = "REVIEW_EXPIRED", "REVIEW_EXPIRED", None
        st.review_deadline_at = None
        self._sync_search_attributes()

    async def _review_loop(self) -> dict | None:
        """Timers and Updates are serialized through one lock; expiry is checked first."""
        r = self._review
        while True:
            async with self._lock:
                if self._decision is not None:
                    return self._decision
                now = workflow.now()
                expires, escalate, remind = (_parse(r.expires_at), _parse(r.escalation_due_at),
                                             _parse(r.reminder_due_at))
                if now >= expires:
                    await self._expire_review()
                    return None
                if now >= escalate and not r.escalated_at:
                    if not r.reminded_at:  # overdue while the worker was down: history, not a new notice
                        await self._review_milestone("REMINDER", historical=True)
                    await self._review_milestone("ESCALATION")
                elif now >= remind and not r.reminded_at and not r.escalated_at:
                    await self._review_milestone("REMINDER")
                due = [expires]
                if not r.escalated_at:
                    due.append(escalate)
                    if not r.reminded_at:
                        due.append(remind)
                wait_s = (min(due) - workflow.now()).total_seconds()
            try:
                await workflow.wait_condition(lambda: self._decision is not None, timeout=max(wait_s, 0.001))
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------- finalize and activation

    async def _finalize(self, d: dict) -> KycResult:
        approved = d["decision"] == "APPROVED"
        key = f"kyc-activate:{self.app_id}"
        events = [self._event(
            "KYC_APPROVED" if approved else "KYC_REJECTED", self.app_id,
            actor_type="REVIEWER" if d["source"] == "HUMAN" else "WORKFLOW", actor_id=d["decided_by"],
            request_id=d.get("request_id"), reason=d.get("notes"),
            details={"evaluation_id": d["evaluation_id"], "decision_source": d["source"]})]
        if approved:
            events.append(self._event("ACTIVATION_REQUESTED", self.app_id,
                                      details={"idempotency_key": key, "evaluation_id": d["evaluation_id"]}))
        await self._persist(
            "app_persist_final_outcome",
            FinalOutcomeInput(
                application_id=self.app_id, workflow_id=self.wf_id, run_id=self.run_id,
                evaluation_id=d["evaluation_id"], decision=d["decision"], decision_source=d["source"],
                reason_code=d["reason_code"], decided_by=d["decided_by"], decided_at=d["decided_at"],
                events=events, review_task_id=d["review_task_id"], reason_details=d.get("notes"),
                review_request_id=d.get("request_id"), resolution_notes=d.get("notes"),
                activation_idempotency_key=key if approved else None),
            "persist-final")
        st = self._st
        st.waiting_reason, st.review_deadline_at = None, None
        if not approved:
            st.business_status, st.current_stage = "REJECTED", "COMPLETE"
            self._sync_search_attributes()
            return KycResult(application_id=self.app_id, status="REJECTED", decision="REJECTED")
        st.business_status, st.current_stage = "APPROVED", "KYC_APPROVED"
        return await self._activate(d["evaluation_id"], key)

    async def _activate(self, evaluation_id: str, key: str) -> KycResult:
        st = self._st
        budget = timedelta(seconds=self.profile.activation_budget_s)
        while True:
            await self._enter_stage(
                "ACTIVATION", status="ACTIVATING",
                extra={"activation_deadline_at": (workflow.now() + budget).isoformat()})
            st.activation_status = "PENDING"
            try:
                # Schedule-To-Close bounds this scheduling generation's retry envelope.
                receipt = await self._exec(
                    "app_activate_account", ActivateAccountInput(self.app_id, key, evaluation_id, self._gen,
                                         (self._loaded.metadata_packet or {}).get("account")),
                    ActivationReceipt, f"activate-g{self._gen}", schedule_to_close=budget)
            except ActivityError as exc:
                code, msg, _ = _failure_info(exc)
                blocked = code == ACTIVATION_REJECTED
                await self._persist(
                    "app_persist_activation",
                    PersistActivationInput(
                        application_id=self.app_id, workflow_id=self.wf_id, run_id=self.run_id,
                        result="BLOCKED" if blocked else "TECHNICAL_ERROR", error_code=code, error_message=msg,
                        events=[self._event("ACTIVATION_ERROR", f"r{self._gen}", reason=msg, details={
                            "error_code": code, "recovery_generation": self._gen, "blocked": blocked})]),
                    "persist-activation-error")
                st.activation_status = "BLOCKED" if blocked else "TECHNICAL_ERROR"
                await self._enter_recovery("ACTIVATION", code, msg)
                continue
            # Provider succeeded: persist the recorded receipt (never re-create the account).
            await self._persist(
                "app_persist_activation",
                PersistActivationInput(
                    application_id=self.app_id, workflow_id=self.wf_id, run_id=self.run_id,
                    result="ACTIVE", receipt=receipt,
                    events=[self._event("ACTIVATION_CONFIRMED", self.app_id, details={
                        "account_id": receipt.account_id, "provider_reference": receipt.provider_reference,
                        "idempotency_key": key})]),
                "persist-activation")
            st.activation_status, st.account_id = "ACTIVE", receipt.account_id
            st.business_status, st.current_stage = "ACTIVE", "COMPLETE"
            st.waiting_reason = None
            self._sync_search_attributes()
            return KycResult(application_id=self.app_id, status="ACTIVE", decision="APPROVED",
                             account_id=receipt.account_id)

    # ------------------------------------------------------------------ Query

    @workflow.query(name="get_processing_state")
    def get_processing_state(self) -> ProcessingState:
        st = dataclasses.replace(self._st)
        actions = []
        if self._review_open():
            actions += ["submit_review_decision", "request_policy_reevaluation"]
        if self._recovery:
            actions.append("retry_failed_stage")
        st.available_actions = actions
        st.run_id = workflow.info().run_id
        return st

    # ---------------------------------------------------------------- Updates

    def _dedupe(self, req) -> UpdateOutcome | None:
        prior = self._requests.get(req.request_id)
        if prior is None:
            return None
        if prior[0] != _fingerprint(req):
            raise ApplicationError(f"request_id {req.request_id} was already used with a different body",
                                   type="RequestIdReused", non_retryable=True)
        return prior[1]

    def _remember(self, req, outcome: UpdateOutcome) -> UpdateOutcome:
        self._requests[req.request_id] = (_fingerprint(req), outcome)
        return outcome

    @staticmethod
    def _reject(message: str, kind: str = "UpdateRejected") -> ApplicationError:
        return ApplicationError(message, type=kind, non_retryable=True)

    def _check_review_request(self, req, roles: set[str], expected_eval: str,
                              task_id: str | None = None) -> None:
        if req.application_id != self.app_id:
            raise self._reject("application_id does not match this workflow")
        if req.actor_role not in roles or not req.actor_id:
            raise self._reject("actor is not authorised for this command", "Forbidden")
        r = self._review
        if r is None or self._decision is not None or self._st.business_status != "AWAITING_REVIEW":
            raise self._reject("application is not awaiting review", "StateMismatch")
        if workflow.now() >= _parse(r.expires_at):  # workflow time, never a client timestamp
            raise self._reject("the review has expired", "ReviewExpired")
        if expected_eval != r.evaluation_id or (task_id is not None and task_id != r.review_task_id):
            raise self._reject("request names a superseded or unknown evaluation/task", "StaleRequest")

    async def _reread_policy_before_decision(self, req: ReviewDecisionRequest) -> None:
        """Re-read the current policy JSON when a reviewer decides.

        If nothing new was published since the last current policy read, the decision proceeds. If it changed, the open
        review is superseded by a fresh evaluation under the new policy and this decision is
        rejected, so a reviewer's decision is never applied to a policy they did not see.
        """
        new_id, seq = str(workflow.uuid4()), self._eval.evaluation_sequence + 1
        try:
            evaluation = await self._exec(
                "app_evaluate_application", self._evaluate_input(new_id, seq, "current"),
                EvaluationResult, f"review-policy-check-{req.request_id[:8]}")
        except ActivityError as exc:
            code, _, _ = _failure_info(exc)
            raise self._reject(f"the current policy could not be re-read ({code}); retry the decision",
                               "PolicyCheckFailed")
        current, pinned = evaluation.policy, self._policy
        now_current = (current.version, current.sha256)
        if now_current == (pinned.version, pinned.sha256):
            self._seen_current = now_current
            return
        if now_current == self._seen_current:
            return  # nothing new published; the review may rest on an operator-pinned version
        self._seen_current = now_current
        await self._record_event([self._event(
            "POLICY_CHANGED_BEFORE_DECISION", req.request_id, actor_type="WORKFLOW", request_id=req.request_id,
            details={"from_policy_version": pinned.version, "to_policy_version": current.version,
                     "to_policy_sha256": current.sha256, "rejected_decision_by": req.actor_id})])
        review = await self._supersede_review(evaluation, req.request_id)
        raise self._reject(
            f"policy changed to v{current.version} before this decision; the review was re-evaluated"
            + (f" (new evaluation {new_id}, task {review.review_task_id}); decide again" if review
               else " and no review is needed"), "PolicyChanged")

    # -- submit_review_decision

    @workflow.update(name="submit_review_decision")
    async def submit_review_decision(self, req: ReviewDecisionRequest) -> UpdateOutcome:
        async with self._lock:
            if (dup := self._dedupe(req)) is not None:
                return dup
            self._check_review_request(req, DECISION_ROLES, req.evaluation_id, req.review_task_id)
            if req.decision not in ("APPROVE", "REJECT"):
                raise self._reject("decision must be APPROVE or REJECT")
            await self._reread_policy_before_decision(req)
            accepted_at = workflow.now().isoformat()
            await self._record_event([self._event(
                "REVIEW_DECISION_ACCEPTED", req.request_id, actor_type="REVIEWER", actor_id=req.actor_id,
                reason=req.reason, request_id=req.request_id,
                details={"review_task_id": req.review_task_id, "evaluation_id": req.evaluation_id,
                         "decision": req.decision, "role": req.actor_role})])
            self._decision = {
                "decision": "APPROVED" if req.decision == "APPROVE" else "REJECTED", "source": "HUMAN",
                "evaluation_id": req.evaluation_id, "review_task_id": req.review_task_id,
                "reason_code": f"HUMAN_{req.decision}", "decided_by": req.actor_id, "decided_at": accepted_at,
                "request_id": req.request_id, "notes": req.notes or req.reason}
            self._st.waiting_reason = None
            return self._remember(req, UpdateOutcome(req.request_id, "ACCEPTED", "DECISION_RECORDED",
                                                     {"decision": self._decision["decision"]}))

    @submit_review_decision.validator
    def _validate_decision(self, req: ReviewDecisionRequest) -> None:
        if self._dedupe(req) is None:
            self._check_review_request(req, DECISION_ROLES, req.evaluation_id, req.review_task_id)
            if req.decision not in ("APPROVE", "REJECT"):
                raise self._reject("decision must be APPROVE or REJECT")

    # -- request_policy_reevaluation

    async def _supersede_review(self, evaluation: EvaluationResult, request_id: str) -> ReviewTaskSpec | None:
        """Persist a new evaluation that replaces the open one; returns the new review task, if any.

        Shared by operator re-evaluation and the decision-time policy re-read. Deadlines and notice
        flags carry forward; this never resets the review budget.
        """
        old_eval, old_review = self._eval, self._review
        new_id, seq = evaluation.evaluation_id, evaluation.evaluation_sequence
        policy, review_required = evaluation.policy, evaluation.review_required
        # Deadlines and notice flags carry forward; re-evaluation never resets the budget.
        review = None
        if review_required:
            review = dataclasses.replace(
                old_review, review_task_id=str(workflow.uuid4()), evaluation_id=new_id,
                review_key=f"review-{seq}", reason=kyc_logic.review_reason(evaluation.findings))
        events = [
            self._event("EVALUATION_COMPLETED", new_id, request_id=request_id, details={
                **self._policy_details(policy, new_id), "outcome": evaluation.evaluation_outcome,
                "rule_ids": [r["rule_id"] for r in evaluation.rule_results],
                "supersedes_evaluation_id": old_eval.evaluation_id}),
            self._event("REVIEW_SUPERSEDED", old_review.review_task_id, request_id=request_id, details={
                "review_task_id": old_review.review_task_id, "superseded_by_evaluation_id": new_id}),
            self._event("ASSESSMENT_PERSISTED", new_id, request_id=request_id, details={
                "evaluation_id": new_id, "review_required": review_required,
                "finding_count": len(evaluation.findings)}),
        ]
        if review:
            events.append(self._event("REVIEW_OPENED", review.review_task_id, details={
                "review_task_id": review.review_task_id, "evaluation_id": new_id,
                "reason": review.reason, "expires_at": review.expires_at, "carried_forward": True}))
        await self._persist(
            "app_persist_assessment",
            self._assessment_input(
                evaluation, events=events, review=review, supersede=old_review.review_task_id,
                status="AWAITING_REVIEW" if review else "PROCESSING",
                stage="HUMAN_REVIEW" if review else "FINALIZING", include_loaded=False),
            f"persist-reeval-{new_id[:8]}", AssessmentPersisted)

        self._eval, st = evaluation, self._st
        self._policy, st.evaluation_id = policy, new_id
        st.policy_id, st.policy_version, st.policy_sha256 = policy.policy_id, policy.version, policy.sha256
        if review:
            self._review, st.review_task_id = review, review.review_task_id
        else:  # nothing left to review: resume disposition as an automatic approval
            self._decision = {"decision": "APPROVED", "source": "AUTOMATIC", "evaluation_id": new_id,
                              "review_task_id": None, "reason_code": "CLEAN_AFTER_REEVALUATION",
                              "decided_by": "kyc-workflow", "decided_at": workflow.now().isoformat()}
        self._sync_search_attributes()
        return review

    @workflow.update(name="request_policy_reevaluation")
    async def request_policy_reevaluation(self, req: ReevaluationRequest) -> UpdateOutcome:
        async with self._lock:
            if (dup := self._dedupe(req)) is not None:
                return dup
            self._check_review_request(req, OPERATOR_ROLES, req.expected_evaluation_id)
            old_eval = self._eval
            await self._record_event([self._event(
                "POLICY_REEVALUATION_REQUESTED", req.request_id, actor_type="OPERATOR", actor_id=req.actor_id,
                reason=req.reason, request_id=req.request_id,
                details={"campaign_id": req.campaign_id, "target_policy_id": req.policy_id,
                         "target_policy_version": req.policy_version, "target_policy_sha256": req.policy_sha256,
                         "expected_evaluation_id": req.expected_evaluation_id})])

            new_id, seq = str(workflow.uuid4()), old_eval.evaluation_sequence + 1
            try:
                # Explicit mode never follows a later current pointer. The Activity folds in the
                # existing OFAC/PEP findings, which stay binding review inputs.
                evaluation = await self._exec(
                    "app_evaluate_application",
                    self._evaluate_input(new_id, seq, "explicit", req.policy_version, req.policy_sha256),
                    EvaluationResult, f"reeval-evaluate-{new_id[:8]}")
            except ActivityError as exc:
                code, msg, _ = _failure_info(exc)
                raise self._reject(f"re-evaluation failed ({code}); the existing assessment is unchanged",
                                   "ReevaluationFailed")
            policy, review_required = evaluation.policy, evaluation.review_required
            review = await self._supersede_review(evaluation, req.request_id)
            return self._remember(req, UpdateOutcome(req.request_id, "ACCEPTED", "REEVALUATED", {
                "evaluation_id": new_id, "policy_version": policy.version, "review_required": review_required,
                "review_task_id": review.review_task_id if review else None}))

    @request_policy_reevaluation.validator
    def _validate_reevaluation(self, req: ReevaluationRequest) -> None:
        if self._dedupe(req) is None:
            self._check_review_request(req, OPERATOR_ROLES, req.expected_evaluation_id)
            if not kyc_logic.is_sha256(req.policy_sha256) or req.policy_version < 1:
                raise self._reject("an explicit policy version and sha256 are required")

    # -- retry_failed_stage

    def _check_retry(self, req: RetryRequest) -> None:
        if req.application_id != self.app_id:
            raise self._reject("application_id does not match this workflow")
        if req.actor_role not in OPERATOR_ROLES or not req.actor_id:
            raise self._reject("actor is not authorised for this command", "Forbidden")
        rec = self._recovery
        if rec is None or self._retry_req is not None:
            raise self._reject("no recovery wait is open", "StateMismatch")
        if req.failed_stage != rec["stage"] or req.recovery_generation != rec["generation"]:
            raise self._reject("stage or recovery generation mismatch", "StaleRequest")
        if workflow.now() >= rec["deadline"]:  # recovery can never extend a review deadline
            raise self._reject("the recovery deadline has passed", "RecoveryExpired")

    @workflow.update(name="retry_failed_stage")
    async def retry_failed_stage(self, req: RetryRequest) -> UpdateOutcome:
        async with self._lock:
            if (dup := self._dedupe(req)) is not None:
                return dup
            self._check_retry(req)
            new_gen = req.recovery_generation + 1
            await self._record_event([self._event(
                "OPERATOR_RETRY_REQUESTED", req.request_id, actor_type="OPERATOR", actor_id=req.actor_id,
                reason=req.reason, request_id=req.request_id, details={
                    "failed_stage": req.failed_stage, "new_recovery_generation": new_gen,
                    "note": "fresh scheduling generation: stub fail-first-two patterns may restart"})])
            self._st.recovery_generation = new_gen
            self._retry_req = req
            return self._remember(req, UpdateOutcome(req.request_id, "ACCEPTED", "RETRY_SCHEDULED",
                                                     {"recovery_generation": new_gen}))

    @retry_failed_stage.validator
    def _validate_retry(self, req: RetryRequest) -> None:
        if self._dedupe(req) is None:
            self._check_retry(req)
